"""Hörprobe mit Gemini 3.8 Flash TTS (Cloud, Google) im Dialog-Modus, zum Vergleich mit Chatterbox.

Anders als die lokalen Engines spricht Gemini nicht Satz für Satz, sondern einen ganzen Dialog-Abschnitt
in einem Durchgang (höchstens 2 Sprecher pro Anfrage): Timing und Sprecherwechsel macht das Modell selbst.
Ein Abschnitt = alle Beiträge zwischen zwei Jingles. Jingles, Teppich und Lautheit mischt render.mischen()
wie bei Chatterbox.

Stimmen (--sommer / --mondschein):
  design   (Standard) Gemini erfindet die Stimme aus derselben Beschreibung wie unsere Qwen-Stimmen
           (voice_design_qwen3.py: sommer_moderator, mondschein_bass). Die Stimme wird einmal angelegt
           und ihre ID in tts/voices/kandidaten/gemini/stimmen.json gemerkt; Hörprobe von Google daneben.
  <Name>   eine fertige Gemini-Stimme, z. B. Puck, Achird, Charon, Algenib, Gacrux
           (Liste: https://ai.google.dev/gemini-api/docs/speech-generation)

API-Key: Umgebungsvariable GEMINI_API_KEY oder Datei tts/gemini_api_key.txt (nur der Key, eine Zeile).
Der Key wird nie ausgegeben.

  tts\\.venv\\Scripts\\python.exe tts\\probe_gemini.py
  tts\\.venv\\Scripts\\python.exe tts\\probe_gemini.py --sommer Achird --mondschein Algenib

Text: ganze Beiträge im Original (beitraege.py), nicht die Chatterbox-Lautschrift.
Fertige Abschnitte liegen in tts/cache/gemini/ – ein zweiter Lauf kostet nichts, --neu würfelt neu.
Ergebnis: tts/out/<datum>_gemini_probe_<sommer>_<mondschein>.wav/.mp3
"""
import argparse
import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

import render
from beitraege import ablauf, lade

TTS = Path(__file__).resolve().parent
ROOT = TTS.parent
MODELL = "gemini-3.8-flash-tts"
KANDIDATEN = TTS / "voices" / "kandidaten" / "gemini"
CACHE = TTS / "cache" / "gemini"
FFMPEG = shutil.which("ffmpeg") or r"c:\prg\FFmpeg\bin\ffmpeg.exe"   # Linux (Cloud): aus dem PATH

# Beschreibungen wie die gewählten Qwen-Entwürfe (voice_design_qwen3.py), gekürzt auf 1–2 Sätze (Google-Empfehlung)
BESCHREIBUNG = {
    "SOMMER": "A man in his early forties with a clear, friendly mid-range voice, like an experienced radio host, "
              "relaxed, confident and optimistic; native German speaker with clear standard pronunciation.",
    "MONDSCHEIN": "A man in his late sixties with a very deep, dark and resonant bass voice, calm and grave with dry "
                  "understatement, speaking slowly with weight; native German speaker with clear standard pronunciation.",
}
# Vortrag je Rolle, gilt für jeden Beitrag (speech_metadata.style)
STIL = {
    "SOMMER": "enthusiastic, warm and lively, a podcast host who loves new technology",
    "MONDSCHEIN": "dry, sceptical and calm, with quiet irony",
}
NAME = {"SOMMER": "Sommer", "MONDSCHEIN": "Mondschein"}
REGIE = {"lacht": "<laugh>", "lacht kurz": "<laugh>", "Pause": "<short pause>", "seufzt": "<sigh>",
         "atmet durch": "<breath>"}
REGIE_STIL = {"leise": "speaking quietly"}             # Regie, die den Vortrag des ganzen Beitrags ändert


def client():
    from google import genai
    key = os.environ.get("GEMINI_API_KEY")
    datei = TTS / "gemini_api_key.txt"
    if not key and datei.exists():
        key = datei.read_text(encoding="utf-8").strip()
    if not key:
        sys.exit("Kein API-Key: GEMINI_API_KEY setzen oder tts/gemini_api_key.txt anlegen "
                 "(Key aus https://aistudio.google.com/apikey).")
    # eigene Wiederholung in sprich(): das SDK würde sonst bei 429 still die Retry-After-Zeit abwarten (Stunden)
    # (Statuscode 599 gibt es nicht: damit wiederholt das SDK gar nichts selbst)
    return genai.Client(api_key=key, http_options={"timeout": 300_000,
                                                   "retry_options": {"attempts": 1, "http_status_codes": [599]}})


def als_mp3(wav: Path):
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(wav), "-b:a", "160k", str(wav.with_suffix(".mp3"))],
                   check=True)


def design_stimme(c, rolle: str) -> str:
    """Legt die Stimme per Beschreibung an (einmalig) und gibt ihre ID zurück."""
    KANDIDATEN.mkdir(parents=True, exist_ok=True)
    liste = KANDIDATEN / "stimmen.json"
    stimmen = json.loads(liste.read_text(encoding="utf-8")) if liste.exists() else {}
    schluessel = hashlib.sha1(BESCHREIBUNG[rolle].encode()).hexdigest()[:8]
    name = f"{rolle.lower()}_{schluessel}"
    if name not in stimmen:
        print(f"Gemini entwirft Stimme {name} …", flush=True)
        v = c.voices.create(store=True, voice={
            "model": MODELL, "type": "prompted", "display_name": f"Pro und Contra {NAME[rolle]}",
            "gender": "male", "language_code": "de-DE", "prompted": {"input": BESCHREIBUNG[rolle]}})
        stimmen[name] = {"id": v.id, "rolle": rolle, "beschreibung": BESCHREIBUNG[rolle]}
        liste.write_text(json.dumps(stimmen, ensure_ascii=False, indent=2), encoding="utf-8")
        if getattr(v, "sample_audio", None) is not None:              # Googles eigene Hörprobe der Stimme
            vorschau = KANDIDATEN / f"{name}_vorschau.wav"
            vorschau.write_bytes(base64.b64decode(v.sample_audio.data))
            als_mp3(vorschau)
    return stimmen[name]["id"]


def abschnitte(plan: list[dict], max_zeichen: int | None = None) -> list[dict]:
    """Fasst aufeinanderfolgende Beiträge zu einem Dialog-Eintrag zusammen (Grenze: Block, Jingle, Pause,
    Teppich; mit max_zeichen zusätzlich an der Beitragsgrenze, bevor der Abschnitt zu lang wird)."""
    neu, lauf = [], []

    def abschliessen():
        if lauf:
            neu.append({"typ": "sprache", "id": f"dialog{sum(e['typ'] == 'sprache' for e in neu) + 1}",
                        "sprecher": "DIALOG", "beitraege": list(lauf), "gain_db": 0.0, "pause_davor": 0.0,
                        "pause_danach": lauf[-1]["pause_danach"], "text": "",
                        "original": " ".join(e["original"] for e in lauf)})
            lauf.clear()
    for e in plan:
        if e["typ"] == "sprache":
            if max_zeichen and lauf and sum(len(x["text"]) for x in lauf) + len(e["text"]) > max_zeichen:
                abschliessen()
            lauf.append(e)
        else:
            abschliessen()
            neu.append(e)
    abschliessen()
    return neu


def sprich(c, dialog: dict, stimme: dict[str, str], neu: bool, versuch: int = 1) -> tuple[np.ndarray, int]:
    """Ein Dialog-Abschnitt in einer Anfrage. Ergebnis im Cache; versuch > 1 = weiterer Take derselben Anfrage."""
    inhalt = []
    for e in dialog["beitraege"]:
        tags = " ".join(REGIE[r] for r in e["regie"] if r in REGIE)
        stil = ", ".join([STIL[e["sprecher"]]] + [REGIE_STIL[r] for r in e["regie"] if r in REGIE_STIL])
        inhalt.append({"type": "text", "text": f"{tags} {e['text']}".strip(),
                       "annotations": [{"type": "speech_metadata", "speaker": NAME[e["sprecher"]],
                                        "style": stil}]})
    anfrage = {"model": MODELL, "input": [{"type": "user_input", "content": inhalt}],
               "response_format": {"type": "audio"},
               "generation_config": {"speech_config": {"speakers": [
                   {"speaker": NAME[r], "voice": v} for r, v in stimme.items()]}}}
    CACHE.mkdir(parents=True, exist_ok=True)
    pfad = CACHE / (hashlib.sha1(json.dumps(anfrage, sort_keys=True).encode()).hexdigest()[:12]
                    + (f"_t{versuch}" if versuch > 1 else "") + ".wav")
    if neu or not pfad.exists():
        for warten in (30, 60, 120, 240, None):           # Ratenlimit (429) der API: warten und erneut
            try:
                antwort = c.interactions.create(**anfrage)
                break
            except Exception as fehler:
                if "per day" in str(fehler):
                    sys.exit(f"Tageslimit der Gemini-API erreicht: {str(fehler)[:300]}\n"
                             "Fertige Abschnitte liegen im Cache; später einfach erneut starten.")
                vorueber = ("429", "RESOURCE_EXHAUSTED", "500", "503", "UNAVAILABLE", "imeout", "isconnect")
                if warten is None or not any(s in f"{type(fehler).__name__} {fehler}" for s in vorueber):
                    raise
                print(f"  API ausgelastet ({str(fehler)[:80]}), warte {warten} s …", flush=True)
                time.sleep(warten)
        pfad.write_bytes(base64.b64decode(antwort.output_audio.data))
    y, sr = sf.read(io.BytesIO(pfad.read_bytes()), dtype="float32")
    return (y.mean(axis=1) if y.ndim == 2 else y), sr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sommer", default="design")
    ap.add_argument("--mondschein", default="design")
    ap.add_argument("--block", type=int, default=0)
    ap.add_argument("--neu", action="store_true", help="Cache ignorieren, neu erzeugen (kostet erneut)")
    args = ap.parse_args()

    c = client()
    wahl = {"SOMMER": args.sommer, "MONDSCHEIN": args.mondschein}
    stimme = {r: design_stimme(c, r) if w == "design" else w for r, w in wahl.items()}

    skript = max((ROOT / "06 Chatterbox Script").glob("*.jsonl"))
    plan = abschnitte(ablauf(render.auswaehlen(lade(skript), [args.block], None)))

    audio, sr, sekunden = {}, None, 0.0
    for d in (e for e in plan if e["typ"] == "sprache"):
        audio[d["id"]], sr = sprich(c, d, stimme, args.neu)
        sekunden += len(audio[d["id"]]) / sr
        print(f"{d['id']}: {len(d['beitraege'])} Beiträge, {len(audio[d['id']]) / sr:5.1f} s", flush=True)

    name = f"{skript.stem}_gemini_probe_{args.sommer.lower()}_{args.mondschein.lower()}"
    _, mp3, dauer = render.mischen(plan, audio, sr, name)
    # 25 Audio-Token pro Sekunde, 9 $ pro Mio. (Preis bis Ende 2026, danach 18 $)
    print(f"\nFertig: {mp3} ({dauer / 60:.1f} min), Sprache {sekunden:.0f} s ≈ {sekunden * 25 * 9e-6:.3f} $")


if __name__ == "__main__":
    main()
