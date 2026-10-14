"""Rendert ein Chatterbox-Sprechskript (06 Chatterbox Script/<datum>.jsonl) zu einer Podcast-Datei.

  - jedes Sprachsegment wird einzeln mit Chatterbox (Deutsch) auf der GPU erzeugt und
    in tts/cache/ zwischengespeichert; ein neuer Lauf erzeugt nur geänderte Segmente neu
  - Whisper hört jeden Take ab; wiederholt Chatterbox etwas oder brabbelt nach, wird neu
    gewürfelt (bis zu 5 Takes, der beste zählt)
  - Jingles und Klangteppich kommen laut tts/sounds.json aus sounds/ (fehlt eine Datei: Stille)
  - jede Stimme wird auf -16 LUFS gebracht, Jingles/Teppich auf ihren Pegel aus sounds.json
  - Ausgabe: 48 kHz Stereo als WAV und MP3 in tts/out/

Stimmen: tts/voices/aktuell/sommer.wav und tts/voices/aktuell/mondschein.wav (10–20 s saubere Sprache).
Fehlt eine Datei, spricht diese Rolle mit der Standardstimme von Chatterbox.

Beispiele (im venv):
  python tts/render.py                         # ganze Folge (neuestes .jsonl)
  python tts/render.py --block 0               # nur Block 0 als Probe
  python tts/render.py --ids b1-025,b1-026     # einzelne Segmente (zum Nachhören)
  python tts/render.py --neu --ids b1-025      # Segment neu würfeln (Cache ignorieren)
  python tts/render.py --block 0 --sommer tts/voices/kandidaten/x.wav   # andere Stimme probehören
"""
import argparse
import difflib
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import librosa
import numpy as np
import pyloudnorm
import soundfile as sf

TTS = Path(__file__).resolve().parent
ROOT = TTS.parent
SR_TTS = 24000   # Chatterbox
VERSUCHE = 5      # je Segment höchstens so viele Takes
MANGEL_OK = 0.05  # Take gilt als sauber bis zu dieser Wertung (siehe mangel())
SR = 48000       # Mischung und Ausgabe
ZIEL_LUFS = -16.0
MAX_PEAK = 0.891  # -1 dBFS


def stimm_schluessel(pfad: Path) -> str:
    if not pfad.exists():
        return "standard"
    return hashlib.sha1(pfad.read_bytes()).hexdigest()[:8]


class Sprecher:
    """Lädt Chatterbox erst, wenn wirklich etwas erzeugt werden muss."""

    def __init__(self, vorlagen: dict[str, Path]):
        self.model = None
        self.conds = {}
        self.vorlagen = vorlagen

    def laden(self):
        if self.model is not None:
            return
        import torch
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS
        geraet = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"Lade Chatterbox Multilingual auf {geraet} …", flush=True)
        self.model = ChatterboxMultilingualTTS.from_pretrained(device=geraet)
        standard = self.model.conds
        for name, ref in self.vorlagen.items():
            if ref.exists():
                self.model.prepare_conditionals(str(ref))
                self.conds[name] = self.model.conds
                print(f"  Stimme {name}: {ref.name}")
            else:
                self.conds[name] = standard
                print(f"  Stimme {name}: Standardstimme (keine {ref})")

    def sprich(self, seg: dict, versuch: int, variante: int = 0) -> np.ndarray:
        import torch
        self.laden()
        self.model.conds = self.conds[seg["sprecher"]]
        torch.manual_seed(int(seg["hash"][:8], 16) + versuch + 1000 * variante)
        wav = self.model.generate(
            seg["text"], language_id="de",
            exaggeration=seg["exaggeration"], cfg_weight=seg["cfg_weight"],
        )
        return wav.squeeze(0).numpy().astype(np.float32)


class Ohr:
    """Whisper hört jedes neue Segment ab – Chatterbox wiederholt gern das Satzende
    („Sicher? Sicher?“) oder brabbelt ein paar Silben hinterher."""
    MODELL = "openai/whisper-large-v3-turbo"
    VERSION = 2   # 2: mit Stille drumherum; ältere Cache-Befunde werden neu abgehört

    def __init__(self):
        self.model = None

    def hoeren(self, wav: np.ndarray) -> str:
        import torch
        if self.model is None:
            from transformers import WhisperForConditionalGeneration, WhisperProcessor
            self.geraet = "cuda" if torch.cuda.is_available() else "cpu"
            dtype = torch.float16 if self.geraet == "cuda" else torch.float32
            self.proc = WhisperProcessor.from_pretrained(self.MODELL)
            self.model = WhisperForConditionalGeneration.from_pretrained(self.MODELL, dtype=dtype).to(self.geraet).eval()
        if len(wav) < SR_TTS * 0.2:
            return ""
        y = librosa.resample(wav, orig_sr=SR_TTS, target_sr=16000)
        # je 1 s Stille davor/danach: sonst überhört Whisper bei kurzen Schnipseln angehängte Silben
        # („Und?“ + „die“ wurde als „und“ erkannt)
        stille = np.zeros(16000, dtype=np.float32)
        y = np.concatenate([stille, y, stille])
        lang = len(y) > 30 * 16000                      # über 30 s: Long-Form mit Zeitstempeln
        f = self.proc(y, sampling_rate=16000, return_tensors="pt", return_attention_mask=True,
                      **({"truncation": False, "padding": "longest"} if lang else {}))
        with torch.no_grad():
            ids = self.model.generate(f.input_features.to(self.geraet, self.model.dtype),
                                      attention_mask=f.attention_mask.to(self.geraet),
                                      language="german", task="transcribe", return_timestamps=lang)
        return self.proc.batch_decode(ids, skip_special_tokens=True)[0].strip()


def vergleichstext(t: str) -> str:
    import md_zu_chatterbox as md
    # ß = ss: Whisper schreibt teils alte Rechtschreibung („wußte, daß“)
    return re.sub(r"[^a-zäöü]", "", md.zahlen_normalisieren(t).lower().replace("ß", "ss"))


def fehlerquote(soll: str, ist: str) -> float:
    """Zeichenfehlerquote zwischen Soll-Text und dem, was Whisper gehört hat (0 = gleich)."""
    a, b = vergleichstext(soll), vergleichstext(ist)
    return 1.0 - difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() if a else 0.0


def mangel(wav: np.ndarray, text: str, soll: str, gehoert: str) -> float:
    """0 = sauber. Zeichenfehlerquote + Überhang (Gehörtes länger als 1,05 × Soll: Wiederholung
    oder Nachbrabbeln, das die Fehlerquote allein kaum anhebt) + Strafe für unplausible Länge."""
    a, b = vergleichstext(soll), vergleichstext(gehoert)
    ueberhang = max(0.0, len(b) / len(a) - 1.05) if a else 0.0
    return fehlerquote(soll, gehoert) + ueberhang + (0 if plausibel(wav, text) else 0.5)


def soll_texte(eintraege: list[dict]) -> dict[str, str]:
    """Womit das Gehörte verglichen wird: bei Beiträgen aus einem Stück das Original (Whisper
    schreibt „Chatbot“, nicht „Tschättbott“), bei zerlegten Beiträgen der Stücktext."""
    sprach = [e for e in eintraege if e["typ"] == "sprache"]
    soll = {}
    for e, danach in zip(sprach, sprach[1:] + [None]):
        ganz = e["original"] and (danach is None or danach["original"])
        soll[e["id"]] = e["original"] if ganz else e["text"]
    return soll


def plausibel(wav: np.ndarray, text: str) -> bool:
    """Grober Test gegen Aussetzer (zu kurz) und Gebrabbel/Endlosschleifen (zu lang)."""
    dauer = len(wav) / SR_TTS
    return 0.035 * len(text) - 0.3 <= dauer <= 0.13 * len(text) + 1.5


def fades(y: np.ndarray, sr: int, fade_in: float, fade_out: float) -> np.ndarray:
    n_in, n_out = min(int(fade_in * sr), len(y)), min(int(fade_out * sr), len(y))
    form = (1,) * (y.ndim - 1)
    if n_in:
        y[:n_in] *= np.linspace(0, 1, n_in, dtype=np.float32).reshape(-1, *form)
    if n_out:
        y[-n_out:] *= np.linspace(1, 0, n_out, dtype=np.float32).reshape(-1, *form)
    return y


def trimmen(wav: np.ndarray) -> np.ndarray:
    wav, _ = librosa.effects.trim(wav, top_db=40)
    return fades(wav.copy(), SR_TTS, 0.01, 0.01)


def lautheit(y: np.ndarray, sr: int) -> float | None:
    if len(y) < sr * 0.5:
        return None
    wert = pyloudnorm.Meter(sr).integrated_loudness(y)
    return wert if np.isfinite(wert) else None


class Sounds:
    def __init__(self):
        cfg_pfad = TTS / "sounds.json"
        self.cfg = json.loads(cfg_pfad.read_text(encoding="utf-8")) if cfg_pfad.exists() else {}
        self.cache = {}

    def laden(self, name: str, mindestlaenge: int = 0) -> np.ndarray | None:
        """Stereo (n, 2) bei 48 kHz, auf Ziel-LUFS gebracht; None wenn nicht vorhanden."""
        c = self.cfg.get(name)
        if not c or not (ROOT / c["datei"]).exists():
            return None
        if name not in self.cache:
            y, _ = librosa.load(ROOT / c["datei"], sr=SR, mono=False)
            y = np.stack([y, y]) if y.ndim == 1 else y[:2]
            y = np.ascontiguousarray(y.T, dtype=np.float32)
            a = int(c.get("start", 0) * SR)
            y = y[a:a + int(c["laenge"] * SR)] if "laenge" in c else y[a:]
            l = lautheit(y, SR)
            if l is not None:
                y *= 10 ** ((c.get("lufs", -18) - l) / 20)
            if np.abs(y).max() > MAX_PEAK:
                y *= MAX_PEAK / np.abs(y).max()
            self.cache[name] = y
        y = self.cache[name].copy()
        while len(y) < mindestlaenge:  # Teppich zu kurz: mit 2 s Überblendung wiederholen
            x = int(2 * SR)
            rampe = np.linspace(0, 1, x, dtype=np.float32)[:, None]
            y = np.concatenate([y[:-x], y[-x:] * (1 - rampe) + self.cache[name][:x] * rampe,
                                self.cache[name][x:]])
        if mindestlaenge:
            y = y[:mindestlaenge]
        return fades(y, SR, c.get("fade_in", 0.0), c.get("fade_out", 0.0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl", nargs="?", help="Sprechskript; Standard: neuestes in '06 Chatterbox Script'")
    ap.add_argument("--block", type=int, action="append", help="nur diese(n) Block/Blöcke")
    ap.add_argument("--ids", help="nur diese Segment-IDs, kommagetrennt")
    ap.add_argument("--neu", action="store_true", help="Cache für die gewählten Segmente ignorieren")
    ap.add_argument("--variante", type=int, default=0,
                    help="andere Seeds = neue Takes (eigener Cache, Ausgabe mit _v<N>)")
    ap.add_argument("--ohne-whisper", action="store_true",
                    help="Takes nicht mit Whisper abhören (nur Längenprüfung wie früher)")
    ap.add_argument("--sommer", type=Path, help="andere Vorlage für Sommer (Ausgabe bekommt ihren Namen)")
    ap.add_argument("--mondschein", type=Path, help="andere Vorlage für Mondschein (Ausgabe bekommt ihren Namen)")
    args = ap.parse_args()

    pfad = Path(args.jsonl) if args.jsonl else max((ROOT / "06 Chatterbox Script").glob("*.jsonl"))
    datum = pfad.stem
    eintraege = [json.loads(z) for z in pfad.read_text(encoding="utf-8").splitlines() if z.strip()]

    soll = soll_texte(eintraege)
    eintraege = auswaehlen(eintraege, args.block, args.ids)
    teilauswahl = args.block is not None or args.ids is not None

    # 1. Sprache erzeugen bzw. aus dem Cache laden --------------------------------------
    cache = TTS / "cache" / datum
    cache.mkdir(parents=True, exist_ok=True)
    anders = {"SOMMER": args.sommer, "MONDSCHEIN": args.mondschein}
    vorlagen = {n: p or TTS / "voices" / "aktuell" / f"{n.lower()}.wav" for n, p in anders.items()}
    stimmen = {n: stimm_schluessel(p) for n, p in vorlagen.items()}
    sprecher, ohr = Sprecher(vorlagen), (None if args.ohne_whisper else Ohr())
    sprach = [e for e in eintraege if e["typ"] == "sprache"]
    audio, warnungen, neu_erzeugt, start = {}, [], 0, time.time()
    v = f"_v{args.variante}" if args.variante else ""
    for n, e in enumerate(sprach, 1):
        datei = cache / f"{e['id']}_{e['hash']}_{stimmen[e['sprecher']]}{v}.wav"
        info = datei.with_suffix(".json")               # was Whisper gehört hat
        if datei.exists() and not args.neu:
            wav, _ = sf.read(datei, dtype="float32")
            if ohr and (not info.exists() or
                        json.loads(info.read_text(encoding="utf-8")).get("ohr") != Ohr.VERSION):
                info.write_text(json.dumps({"gehoert": ohr.hoeren(wav), "ohr": Ohr.VERSION}, ensure_ascii=False),
                                encoding="utf-8")                   # (noch) nicht so abgehört: nachholen
            gemerkt = json.loads(info.read_text(encoding="utf-8")) if ohr else {}
            m = mangel(wav, e["text"], soll[e["id"]], gemerkt["gehoert"]) if ohr else 0.0
            if m <= MANGEL_OK or gemerkt.get("ausgereizt"):
                # ausgereizt: schon das Beste aus allen Takes; neu würfeln brächte dieselben Seeds
                if m > MANGEL_OK:
                    warnungen.append(e["id"])
                audio[e["id"]] = wav
                continue
            print(f"  ! {e['id']}: Take im Cache nicht sauber (Mangel {m:.2f}), wird neu erzeugt", flush=True)

        bester = None
        for versuch in range(VERSUCHE if ohr else 3):
            wav = trimmen(sprecher.sprich(e, versuch, args.variante))
            gehoert = ohr.hoeren(wav) if ohr else ""
            m = mangel(wav, e["text"], soll[e["id"]], gehoert) if ohr else (0 if plausibel(wav, e["text"]) else 0.5)
            if bester is None or m < bester[0]:
                bester = (m, wav, gehoert)
            if m <= MANGEL_OK:
                break
            print(f"  ! {e['id']} Take {versuch + 1}: {len(wav) / SR_TTS:.1f} s, Mangel {m:.2f}  „{gehoert[:70]}“",
                  flush=True)
        m, wav, gehoert = bester
        if m > MANGEL_OK:
            warnungen.append(e["id"])
        for alt in cache.glob(f"{e['id']}_*"):
            # nur Takes derselben Stimme und Variante ersetzen (<id>_<hash>_<stimme>[_v<N>]);
            # Takes anderer Stimmen bleiben, damit ein Stimmvergleich den Cache der echten Stimmen nicht leert
            teile = alt.stem.split("_")
            if len(teile) == (4 if v else 3) and alt.stem.endswith(v) and teile[2] == stimmen[e["sprecher"]]:
                alt.unlink()
        sf.write(datei, wav, SR_TTS)
        if ohr:
            info.write_text(json.dumps({"gehoert": gehoert, "ohr": Ohr.VERSION, "ausgereizt": m > MANGEL_OK},
                                       ensure_ascii=False), encoding="utf-8")
        audio[e["id"]] = wav
        neu_erzeugt += 1
        rest = (time.time() - start) / neu_erzeugt * (len(sprach) - n)
        print(f"[{n}/{len(sprach)}] {e['id']} {e['sprecher']:<10} {len(wav) / SR_TTS:5.1f} s  "
              f"Mangel {m:.2f}  (Rest höchstens {rest / 60:.0f} min)", flush=True)

    name = datum + ("_probe" if teilauswahl else "") + v + "".join(f"_{p.stem}" for p in anders.values() if p)
    wav_pfad, mp3_pfad, dauer = mischen(eintraege, audio, SR_TTS, name)
    if name == datum:                                    # ganze Folge mit den festen Stimmen
        print(f"Abgelegt in {ablegen(name, datum, 'chatterbox')}")

    print(f"\nFertig: {wav_pfad}\n        {mp3_pfad}\nDauer {dauer / 60:.1f} min, "
          f"{neu_erzeugt} Segmente neu erzeugt, {len(sprach) - neu_erzeugt} aus dem Cache")
    if warnungen:
        print("Bitte nachhören (Länge oder Whisper auffällig): " + ", ".join(warnungen))


def auswaehlen(eintraege: list[dict], bloecke: list[int] | None, ids: str | None) -> list[dict]:
    """Blöcke (samt Jingles, Pausen, Teppich) und/oder einzelne Segment-IDs auswählen."""
    if bloecke is not None:
        auswahl, akt = [], None
        for e in eintraege:
            akt = e["block"] if e["typ"] == "block" else akt
            if akt in bloecke:
                auswahl.append(e)
        eintraege = auswahl
    if ids:
        eintraege = [e for e in eintraege if e.get("id") in set(ids.split(","))]
    return eintraege


def ablegen(name: str, datum: str, engine: str) -> Path:
    """Kopiert eine fertige ganze Folge (tts/out/<name>.mp3 samt Kapiteln und Transkript) nach
    09 Folgen/<datum>/<engine>/<datum>_<engine>.*: dort liegt jede Fassung einer Folge zum Vergleichen.
    Die WAV bleibt in tts/out (Größe)."""
    ziel = ROOT / "09 Folgen" / datum / engine
    ziel.mkdir(parents=True, exist_ok=True)
    out = TTS / "out"
    for endung in (".mp3", "_kapitel.json", "_kapitel.txt", "_transkript.json", "_transkript.vtt"):
        if (out / f"{name}{endung}").exists():
            shutil.copyfile(out / f"{name}{endung}", ziel / f"{datum}_{engine}{endung}")
    return ziel


def mischen(eintraege: list[dict], audio: dict, sr_tts: int, name: str) -> tuple[Path, Path, float]:
    """Mischt Sprache (audio: Segment-ID -> Mono-Audio bei sr_tts), Jingles und Teppich zu
    tts/out/<name>.wav und .mp3. Sprachsegmente ohne Audio werden übersprungen: andere Engines
    sprechen ganze Beiträge und lassen die Fortsetzungs-Stücke weg (siehe beitraege.py)."""
    sprach = [e for e in eintraege if e["typ"] == "sprache" and e["id"] in audio]

    # 2. Lautheit je Stimme auf -16 LUFS -----------------------------------------------
    verstaerkung = {}
    for sprecher in {"SOMMER", "MONDSCHEIN"} | {e["sprecher"] for e in sprach}:   # Gemini: "DIALOG"
        teile = [audio[e["id"]] for e in sprach if e["sprecher"] == sprecher]
        l = lautheit(np.concatenate(teile), sr_tts) if teile else None
        # Mono-Stimme liegt auf beiden Kanälen: nach BS.1770 zählt das 3 dB lauter
        verstaerkung[sprecher] = 10 ** ((ZIEL_LUFS - 3.01 - l) / 20) if l is not None else 1.0

    # 3. Ablauf planen (Positionen in Samples bei 48 kHz) --------------------------------
    sounds = Sounds()
    plan, pos, teppich, kapitel, gesprochen = [], 0, [], [], []
    for e in eintraege:
        if e["typ"] == "block":                        # Kapitel beginnt vor dem Jingle des Blocks
            kapitel.append((pos, e["titel"]))
        elif e["typ"] == "pause":
            pos += int(e["sekunden"] * SR)
        elif e["typ"] == "jingle":
            j = sounds.laden(e["name"])
            if j is None:
                print(f"  Jingle '{e['name']}' fehlt, {e['ersatz_stille']} s Stille", flush=True)
                pos += int(e["ersatz_stille"] * SR)
            else:
                pos += int(0.3 * SR)
                plan.append((pos, j))
                pos += len(j) + int(0.3 * SR)
        elif e["typ"] == "teppich":
            if e["aktion"] == "start":
                teppich.append([pos, None])
            elif teppich and teppich[-1][1] is None:
                teppich[-1][1] = pos + int(2.5 * SR)  # läuft unter den nächsten Jingle hinein aus
        elif e["typ"] == "sprache" and e["id"] in audio:
            pos += int(e["pause_davor"] * SR)
            wav = librosa.resample(audio[e["id"]], orig_sr=sr_tts, target_sr=SR)
            wav *= verstaerkung[e["sprecher"]] * 10 ** (e["gain_db"] / 20)
            plan.append((pos, wav))
            if "teile" in e:      # Dialog-Abschnitt (Gemini): Transkript je Beitrag, Zeiten in s ab Abschnittsbeginn
                gesprochen += [(pos + int(a * SR), pos + int(b * SR), t) for a, b, t in e["teile"]]
            else:
                gesprochen.append((pos, pos + len(wav), e))
            pos += len(wav) + int(e["pause_danach"] * SR)
    if not plan and not pos:
        sys.exit("Keine Einträge ausgewählt.")

    # 4. Mischen ------------------------------------------------------------------------
    gesamt = max([pos] + [p + len(y) for p, y in plan] + [t[1] or 0 for t in teppich])
    mix = np.zeros((gesamt, 2), dtype=np.float32)
    for p, y in plan:
        mix[p:p + len(y)] += y if y.ndim == 2 else y[:, None]
    for a, b in teppich:
        b = b or gesamt
        t = sounds.laden("teppich", mindestlaenge=b - a)
        if t is not None:
            mix[a:a + len(t)] += t
    out = TTS / "out"
    out.mkdir(exist_ok=True)
    roh_pfad = out / f"{name}_roh.wav"
    sf.write(roh_pfad, mix, SR, subtype="FLOAT")
    del mix

    # Chatterbox liefert einzelne Spitzen bis 0 dBFS; ein Limiter fängt nur diese ab,
    # statt die ganze Folge leiser zu machen
    ffmpeg = shutil.which("ffmpeg") or r"c:\prg\FFmpeg\bin\ffmpeg.exe"
    limiter = f"alimiter=limit={MAX_PEAK}:attack=5:release=50:level=0"
    wav_pfad, mp3_pfad = out / f"{name}.wav", out / f"{name}.mp3"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(roh_pfad), "-af", limiter,
                    "-c:a", "pcm_s16le", str(wav_pfad)], check=True)
    meta_pfad = kapitel_schreiben(kapitel, gesamt, name)
    transkript_schreiben(gesprochen, name)
    cover = ROOT / "cover" / "cover.jpg"                # quadratisch, wird als Titelbild eingebettet
    bild = (["-i", str(cover), "-map", "2:v", "-c:v", "copy", "-disposition:v", "attached_pic",
             "-metadata:s:v", "title=Cover", "-metadata:s:v", "comment=Cover (front)"]
            if cover.exists() else [])
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(wav_pfad), "-i", str(meta_pfad), *bild,
                    "-map", "0:a", "-map_metadata", "1", "-map_chapters", "1", "-id3v2_version", "3",
                    "-ar", "44100", "-b:a", "160k", str(mp3_pfad)], check=True)
    meta_pfad.unlink()
    roh_pfad.unlink()
    return wav_pfad, mp3_pfad, gesamt / SR


def zeitmarke(sekunden: float) -> str:
    m, s = divmod(int(sekunden), 60)
    return f"{m // 60}:{m % 60:02d}:{s:02d}" if m >= 60 else f"{m:02d}:{s:02d}"


def kapitel_schreiben(kapitel: list[tuple[int, str]], gesamt: int, name: str) -> Path:
    """Kapitel aus den Blocktiteln (Positionen in Samples bei 48 kHz):
      - <name>_kapitel.json  Podcasting-2.0-Kapitel für den Hoster
      - <name>_kapitel.txt   Zeitmarken für die Shownotes (Spotify/YouTube lesen nur diese)
    Rückgabe: FFMETADATA-Datei mit Folgen-Tags und Kapiteln, die ffmpeg als ID3-CHAP in die MP3 schreibt."""
    datum = name.split("_")[0]
    tag, monat, jahr = datum[8:10], datum[5:7], datum[:4]
    tags = {"title": f"Pro und Contra – KI- und IT-News vom {tag}.{monat}.{jahr}",
            "album": "Pro und Contra – KI- und IT-News", "artist": "Herr Sommer & Herr Mondschein",
            "genre": "Podcast", "date": datum}
    esc = lambda t: "".join("\\" + z if z in "=;#\\\n" else z for z in t)
    zeilen = [";FFMETADATA1"] + [f"{k}={esc(v)}" for k, v in tags.items()]
    ms = [round(p / SR * 1000) for p, _ in kapitel] + [round(gesamt / SR * 1000)]
    for (_, titel), a, b in zip(kapitel, ms, ms[1:]):
        zeilen += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={a}", f"END={b}", f"title={esc(titel)}"]
    out = TTS / "out"
    if kapitel:
        (out / f"{name}_kapitel.json").write_text(json.dumps(
            {"version": "1.2.0", "chapters": [{"startTime": round(a / 1000, 1), "title": t}
                                              for (_, t), a in zip(kapitel, ms)]},
            ensure_ascii=False, indent=2), encoding="utf-8")
        (out / f"{name}_kapitel.txt").write_text(
            "".join(f"{zeitmarke(a / 1000)} {t}\n" for (_, t), a in zip(kapitel, ms)), encoding="utf-8")
    meta = out / f"{name}_meta.txt"
    meta.write_text("\n".join(zeilen) + "\n", encoding="utf-8")
    return meta


SPRECHERNAME = {"SOMMER": "Herr Sommer", "MONDSCHEIN": "Herr Mondschein"}


def transkript_schreiben(gesprochen: list[tuple[int, int, dict]], name: str) -> None:
    """Transkript aus dem Skripttext (Original, nicht die Lautschrift) mit den Positionen im Mix:
      - <name>_transkript.json  Podcasting-2.0-Transkript, ein Eintrag je Satz
      - <name>_transkript.vtt   WebVTT mit Sprecher-Tags (Apple Podcasts & Co.)
    Zerlegte Beiträge (Fortsetzungsstücke haben kein „original“) werden wieder zusammengesetzt;
    innerhalb eines Beitrags verteilt sich die Zeit nach Zeichenzahl auf die Sätze."""
    beitraege = []
    for a, b, e in gesprochen:
        if e["original"] or not beitraege:
            beitraege.append([a, b, e["sprecher"], e["original"] or e["text"]])
        else:
            beitraege[-1][1] = b
    saetze = []
    for a, b, sprecher, text in beitraege:
        teile = [t for t in re.split(r"(?<=[.!?…])\s+", text.strip()) if t]
        gewicht = [len(t) + 5 for t in teile]
        t0 = a
        for teil, g in zip(teile, gewicht):
            t1 = t0 + (b - a) * g / sum(gewicht)
            saetze.append({"speaker": SPRECHERNAME.get(sprecher, sprecher), "startTime": round(t0 / SR, 2),
                           "endTime": round(t1 / SR, 2), "body": teil})
            t0 = t1
    out = TTS / "out"
    (out / f"{name}_transkript.json").write_text(json.dumps(
        {"version": "1.0.0", "segments": saetze}, ensure_ascii=False, indent=2), encoding="utf-8")

    def vtt_zeit(s: float) -> str:
        ms = round(s * 1000)
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"
    zeilen = ["WEBVTT", ""]
    for s in saetze:
        zeilen += [f"{vtt_zeit(s['startTime'])} --> {vtt_zeit(s['endTime'])}", f"<v {s['speaker']}>{s['body']}", ""]
    (out / f"{name}_transkript.vtt").write_text("\n".join(zeilen), encoding="utf-8")


if __name__ == "__main__":
    main()
