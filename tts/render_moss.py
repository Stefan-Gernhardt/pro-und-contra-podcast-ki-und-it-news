"""Rendert Sprechbeiträge mit MOSS-TTS v1.5 (OpenMOSS, 8B, Stimmen klonen).

Läuft im eigenen venv (torch 2.9.1+cu128, transformers 5.0.0, Code aus tts/moss-tts-src):
  tts\\.venv-moss\\Scripts\\python.exe tts\\render_moss.py                 Hörprobe: Block 0 mit Jingles
  tts\\.venv-moss\\Scripts\\python.exe tts\\render_moss.py --block 1       andere Blöcke
  tts\\.venv-moss\\Scripts\\python.exe tts\\render_moss.py --ids b0-009 --neu   Beitrag neu würfeln
  tts\\.venv-moss\\Scripts\\python.exe tts\\render_moss.py --ganz          ganze Folge (nur auf Ansage!)

Unterschiede zum Chatterbox-Weg:
- MOSS spricht jeden Beitrag am Stück (Feld "original" aus dem Sprechskript) statt in
  250-Zeichen-Stücken; das ergibt eine durchgehende Satzmelodie.
- Eigene, schlanke Ausspracheliste tts/aussprache_mehrsprachig.json (nur Abkürzungen); englische
  Wörter bleiben im Original, Zahlen werden wie bei Chatterbox ausgeschrieben.
- Jede Aufnahme wird mit Whisper gegengehört. Weicht sie zu stark vom Text ab (Gebrabbel,
  verschluckte Wörter) oder ist sie unplausibel lang/kurz, gibt es bis zu drei Versuche;
  der beste gewinnt. Fertige Beiträge landen im Cache (tts/cache/moss/<datum>/).

Auf der RTX 2070 SUPER (8 GB) wird das Modell 4-bit (NF4) geladen, der 7-GB-Audio-Tokenizer
und Whisper laufen auf der CPU. Auf einer großen Karte (5090): --voll (unquantisiert, alles auf GPU).
Gemischt wird mit render.mischen(), also genau wie bei Chatterbox (Jingles, Teppich, -16 LUFS).
Ergebnis: tts/out/<datum>_moss_probe.wav/.mp3 (bzw. _moss_auswahl, _moss)
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import time
from pathlib import Path

# transformers 5.0 lädt sonst alle Gewichte parallel und unquantisiert auf die GPU (> 8 GB)
os.environ.setdefault("HF_DEACTIVATE_ASYNC_LOAD", "1")

import librosa
import numpy as np
import soundfile as sf
import torch

import md_zu_chatterbox as md
import render
from beitraege import ablauf, lade

TTS = Path(__file__).resolve().parent
ROOT = TTS.parent
MODELL = "OpenMOSS-Team/MOSS-TTS-v1.5"
WHISPER = "openai/whisper-large-v3-turbo"
# bf16 wie im Training; in fp16 läuft das Modell über (NaN beim Sampling). Turing emuliert bf16.
DTYPE = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
# Empfohlene Werte laut Model Card (MossTTSDelay-8B v1.5); dritter Versuch etwas ruhiger.
# Ab dem zweiten Versuch wird die Länge vorgegeben, falls ein Versuch unplausibel lang/kurz war.
VERSUCHE = [
    dict(audio_temperature=1.7, audio_top_p=0.8, audio_top_k=25),
    dict(audio_temperature=1.7, audio_top_p=0.8, audio_top_k=25),
    dict(audio_temperature=1.4, audio_top_p=0.8, audio_top_k=25),
]
FEHLER_OK = 0.10       # Zeichenfehlerquote gegenüber Whisper, ab der ein Versuch sofort genommen wird
TOKENS_PRO_S = 12.5
VERSION = "moss1"      # hochzählen, wenn sich Parameter ändern, die der Cache nicht sieht

torch.backends.cuda.enable_cudnn_sdp(False)


def vergleichstext(t: str) -> str:
    t = md.zahlen_normalisieren(t).lower()
    return re.sub(r"[^a-zäöüß]", "", t)


def fehlerquote(soll: str, ist: str) -> float:
    a, b = vergleichstext(soll), vergleichstext(ist)
    return 1.0 - difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() if a else 0.0


def plausibel(sekunden: float, text: str) -> bool:
    n = len(text)
    return 0.035 * n - 0.3 <= sekunden <= 0.13 * n + 1.5


def dauer_schaetzen(text: str) -> float:
    """Solldauer für die Längensteuerung; MOSS spricht im Mittel ~17 Zeichen pro Sekunde."""
    return 0.35 + len(text) / 17


def datei_hash(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()[:8]


class Moss:
    def __init__(self, voll: bool):
        from huggingface_hub import snapshot_download
        from transformers import AutoModel, AutoProcessor, BitsAndBytesConfig, WhisperForConditionalGeneration

        # lokaler Ordner statt Repo-Name: der MOSS-Code baut den Namen sonst unter Windows mit \ zusammen
        pfad = snapshot_download(MODELL)
        self.processor = AutoProcessor.from_pretrained(pfad, trust_remote_code=True)
        if voll:
            self.processor.audio_tokenizer = self.processor.audio_tokenizer.to("cuda")
        self.sr = int(self.processor.model_config.sampling_rate)
        self.refs = {}

        quant = None if voll else BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=DTYPE,
            bnb_4bit_use_double_quant=True, llm_int8_skip_modules=["lm_heads", "emb_ext"])
        print(f"Lade {MODELL} ({DTYPE}, {'voll' if voll else '4-bit NF4'}) …", flush=True)
        self.model = AutoModel.from_pretrained(pfad, trust_remote_code=True, attn_implementation="sdpa",
                                               dtype=DTYPE, quantization_config=quant, device_map="cuda:0")
        self.model.eval()
        print(f"  VRAM belegt: {torch.cuda.memory_allocated() / 2**30:.1f} GB", flush=True)
        # Whisper direkt statt über pipeline() (die ist in transformers 5.0 für Whisper kaputt)
        self.asr_geraet = "cuda:0" if voll else "cpu"
        self.asr_proc = AutoProcessor.from_pretrained(WHISPER)
        self.asr = WhisperForConditionalGeneration.from_pretrained(
            WHISPER, dtype=torch.float16 if voll else torch.float32).to(self.asr_geraet).eval()

    def referenz(self, wav: Path):
        if wav not in self.refs:
            y, _ = librosa.load(wav, sr=self.sr, mono=True)
            self.refs[wav] = self.processor.encode_audios_from_wav(
                [torch.from_numpy(y).unsqueeze(0)], self.sr, int(self.processor.model_config.n_vq))[0]
        return self.refs[wav]

    def sprechen(self, text: str, stimme: Path, seed: int, params: dict, dauer: float | None = None) -> np.ndarray:
        """dauer: Solllänge in Sekunden (MOSS-Längensteuerung), sonst frei."""
        torch.manual_seed(seed)
        tokens = int(round(dauer * TOKENS_PRO_S)) if dauer else None
        msg = self.processor.build_user_message(text=text, reference=[self.referenz(stimme)], language="German",
                                                tokens=tokens)
        batch = self.processor([[msg]], mode="generation")
        # Obergrenze aus der Textlänge: bremst Gebrabbel, lässt normalem Sprechen viel Luft
        grenze = int((0.13 * len(text) + 3) * TOKENS_PRO_S) + 64
        with torch.no_grad():
            out = self.model.generate(input_ids=batch["input_ids"].to("cuda"),
                                      attention_mask=batch["attention_mask"].to("cuda"),
                                      max_new_tokens=grenze, **params)
        nachricht = self.processor.decode(out)[0]
        if nachricht is None or not nachricht.audio_codes_list:
            return np.zeros(0, np.float32)
        wav = nachricht.audio_codes_list[0].detach().float().cpu().numpy().reshape(-1)
        wav, _ = librosa.effects.trim(wav.astype(np.float32), top_db=40)
        return wav

    def hoeren(self, wav: np.ndarray) -> str:
        if len(wav) < self.sr * 0.2:
            return ""
        y = librosa.resample(wav, orig_sr=self.sr, target_sr=16000)
        lang = len(y) > 30 * 16000                      # über 30 s: Long-Form mit Zeitstempeln
        f = self.asr_proc(y, sampling_rate=16000, return_tensors="pt", return_attention_mask=True,
                          **({"truncation": False, "padding": "longest"} if lang else {}))
        with torch.no_grad():
            ids = self.asr.generate(f.input_features.to(self.asr_geraet, self.asr.dtype),
                                    attention_mask=f.attention_mask.to(self.asr_geraet),
                                    language="german", task="transcribe", return_timestamps=lang)
        return self.asr_proc.batch_decode(ids, skip_special_tokens=True)[0].strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--block", type=int, action="append",
                    help="Blöcke samt Jingles (mehrfach möglich); Standard: Block 0 = Hörprobe, die ersten ~2 Minuten")
    ap.add_argument("--ids", help="nur diese Beiträge, kommagetrennt (ohne Jingles)")
    ap.add_argument("--ganz", action="store_true", help="ganze Folge")
    ap.add_argument("--neu", action="store_true", help="Cache ignorieren")
    ap.add_argument("--voll", action="store_true", help="unquantisiert, alles auf der GPU (~24 GB VRAM)")
    args = ap.parse_args()

    skript = max((ROOT / "06 Chatterbox Script").glob("*.jsonl"))
    bloecke = None if args.ganz or (args.ids and not args.block) else (args.block or [0])
    eintraege = render.auswaehlen(lade(skript), bloecke, None)
    plan = ablauf(eintraege)
    if args.ids:
        wahl = set(args.ids.split(","))
        plan = [e for e in plan if e["typ"] == "sprache" and wahl & set(e["ids"])]
    auswahl = [e for e in plan if e["typ"] == "sprache"]
    if not auswahl:
        raise SystemExit("Keine passenden Beiträge gefunden.")
    name = f"{skript.stem}_moss" + ("" if args.ganz else "_probe" if bloecke == [0] and not args.ids else "_auswahl")

    stimmen = {n: TTS / "voices" / "aktuell" / f"{n.lower()}.wav" for n in ("SOMMER", "MONDSCHEIN")}
    cache = TTS / "cache" / "moss" / skript.stem
    cache.mkdir(parents=True, exist_ok=True)
    moss, audio, fehler, sr, t0, neu = None, {}, {}, 24000, time.time(), 0
    for n, b in enumerate(auswahl, 1):
        stimme = stimmen[b["sprecher"]]
        schluessel = hashlib.sha1(f"{VERSION}|{b['sprecher']}|{b['text']}|{datei_hash(stimme)}"
                                  .encode()).hexdigest()[:12]
        datei = cache / f"{b['id']}_{schluessel}.wav"
        info = datei.with_suffix(".json")
        if datei.exists() and not args.neu:
            wav, sr = sf.read(datei, dtype="float32")
            fq = json.loads(info.read_text(encoding="utf-8"))["fehlerquote"] if info.exists() else float("nan")
            print(f"[{n}/{len(auswahl)}] {b['id']} {b['sprecher']:<10} {len(wav) / sr:5.1f} s  "
                  f"Fehler {fq:4.0%}  (Cache)", flush=True)
            audio[b["id"]], fehler[b["id"]] = wav, fq
            continue
        if moss is None:
            moss = Moss(args.voll)
        sr = moss.sr
        bester, laenge_daneben = None, False
        for nr, params in enumerate(VERSUCHE):
            seed = int(schluessel[:8], 16) + nr
            # war ein Versuch zu lang/kurz (typisch: Ein-Wort-Sätze werden wiederholt), die Länge vorgeben
            dauer = dauer_schaetzen(b["text"]) if laenge_daneben else None
            wav = moss.sprechen(b["text"], stimme, seed, params, dauer)
            gehoert = moss.hoeren(wav)
            fq = fehlerquote(b["original"], gehoert)
            ok = plausibel(len(wav) / sr, b["text"])
            laenge_daneben |= not ok
            wertung = fq + (0 if ok else 0.5)
            print(f"   Versuch {nr + 1}{f' (Länge {dauer:.1f} s vorgegeben)' if dauer else ''}: {len(wav) / sr:5.1f} s, "
                  f"Fehler {fq:4.0%}{'' if ok else ', Länge unplausibel'}  „{gehoert[:60]}“", flush=True)
            if bester is None or wertung < bester[0]:
                bester = (wertung, wav, fq, gehoert, nr + 1)
            if ok and fq <= FEHLER_OK:
                break
        _, wav, fq, gehoert, nr = bester
        for alt in cache.glob(f"{b['id']}_*"):
            alt.unlink()
        sf.write(datei, wav, sr)
        info.write_text(json.dumps({"text": b["text"], "gehoert": gehoert, "fehlerquote": round(fq, 3),
                                    "versuch": nr}, ensure_ascii=False, indent=1), encoding="utf-8")
        audio[b["id"]], fehler[b["id"]] = wav, fq
        neu += 1
        rest = (time.time() - t0) / neu * (len(auswahl) - n)
        print(f"[{n}/{len(auswahl)}] {b['id']} {b['sprecher']:<10} {len(wav) / sr:5.1f} s  Fehler {fq:4.0%}  "
              f"{b['original'][:50]}  (Rest ~{rest / 60:.0f} min)", flush=True)

    # gleicher Mix wie bei Chatterbox: Jingles, Teppich, Lautheit je Stimme, Limiter
    wav_pfad, mp3_pfad, dauer = render.mischen(plan, audio, sr, name)
    auffaellig = [i for i, fq in fehler.items() if fq > 0.2]
    print(f"\nFertig: {wav_pfad}\n        {mp3_pfad}\nDauer {dauer / 60:.1f} min, {neu} Beiträge neu erzeugt, "
          f"{len(auswahl) - neu} aus dem Cache, Rechenzeit {(time.time() - t0) / 60:.0f} min")
    if auffaellig:
        print("Bitte nachhören (Whisper-Abweichung > 20 %): " + ", ".join(auffaellig))


if __name__ == "__main__":
    main()
