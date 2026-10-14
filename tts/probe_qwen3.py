"""Hörprobe mit Qwen3-TTS 1.7B Base (Stimmen klonen), zum Vergleich mit Chatterbox und Kartoffelbox.

Läuft im eigenen venv, weil qwen-tts eine andere transformers-Version braucht:
  tts\\.venv-qwen\\Scripts\\python.exe tts\\probe_qwen3.py

Stimmen: tts/voices/aktuell/sommer.wav und mondschein.wav wie bei Chatterbox. Qwen klont am besten mit
dem Wortlaut der Vorlage (sommer.txt / mondschein.txt); fehlt eine .txt, wird sie mit Whisper erzeugt.
[lacht] lässt sich beim Klonen nicht steuern und entfällt hier.
Text: ganze Beiträge im Original (beitraege.py), nicht die Chatterbox-Lautschrift – sonst spricht
z. B. Mondschein „Smart Houm“ falsch.
Probe = Block 0 mit Jingles, gemischt wie bei Chatterbox (render.mischen).
Ergebnis: tts/out/<datum>_qwen3_probe.wav/.mp3
"""
import hashlib
from pathlib import Path

import librosa
import numpy as np
import torch

import render
from beitraege import ablauf, lade

TTS = Path(__file__).resolve().parent
ROOT = TTS.parent
MODELL = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
# RTX 20xx (Turing) kann kein bfloat16 und kein FlashAttention 2
DTYPE = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
ATTN = "sdpa"


def transkript(wav: Path) -> str:
    txt = wav.with_suffix(".txt")
    if not txt.exists():
        from transformers import pipeline
        print(f"Schreibe {wav.name} mit Whisper ab …", flush=True)
        asr = pipeline("automatic-speech-recognition", model="openai/whisper-large-v3-turbo",
                       torch_dtype=torch.float16, device="cuda:0")
        y, sr = librosa.load(wav, sr=16000)
        text = asr({"raw": y, "sampling_rate": sr}, generate_kwargs={"language": "german"})["text"].strip()
        txt.write_text(text + "\n", encoding="utf-8")
        del asr
        torch.cuda.empty_cache()
    return txt.read_text(encoding="utf-8").strip()


def main():
    from qwen_tts import Qwen3TTSModel

    stimmen = {n: TTS / "voices" / "aktuell" / f"{n.lower()}.wav" for n in ("SOMMER", "MONDSCHEIN")}
    texte = {n: transkript(p) for n, p in stimmen.items()}
    for n, t in texte.items():
        print(f"  Vorlage {n}: {t[:80]}…")

    print(f"Lade {MODELL} ({DTYPE}, {ATTN}) …", flush=True)
    model = Qwen3TTSModel.from_pretrained(MODELL, device_map="cuda:0", dtype=DTYPE, attn_implementation=ATTN)
    prompts = {n: model.create_voice_clone_prompt(ref_audio=str(stimmen[n]), ref_text=texte[n],
                                                  x_vector_only_mode=False) for n in stimmen}

    skript = max((ROOT / "06 Chatterbox Script").glob("*.jsonl"))
    plan = ablauf(render.auswaehlen(lade(skript), [0], None))       # Block 0 mit Jingles

    audio, sr = {}, None
    for e in (e for e in plan if e["typ"] == "sprache"):
        torch.manual_seed(int(hashlib.sha1(e["text"].encode()).hexdigest()[:8], 16))
        wavs, sr = model.generate_voice_clone(text=e["text"], language="German",
                                              voice_clone_prompt=prompts[e["sprecher"]])
        audio[e["id"]], _ = librosa.effects.trim(np.asarray(wavs[0], dtype=np.float32), top_db=40)
        print(f"{e['id']} {e['sprecher']:<10} {len(audio[e['id']]) / sr:5.1f} s  {e['text'][:70]}", flush=True)

    # gleicher Mix wie bei Chatterbox: Jingles, Teppich, Lautheit je Stimme, Limiter
    wav_pfad, mp3_pfad, dauer = render.mischen(plan, audio, sr, f"{skript.stem}_qwen3_probe")
    print(f"\nFertig: {mp3_pfad} ({dauer / 60:.1f} min)")


if __name__ == "__main__":
    main()
