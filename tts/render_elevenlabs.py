"""Folge (oder einzelne Blöcke) mit ElevenLabs Eleven v4 über die Text-to-Dialogue-API, zum Vergleich mit Gemini.

  - Stimmen: Instant Voice Clone unserer festen Stimmen (voices/aktuell/sommer.wav, mondschein.wav),
    einmal angelegt, IDs in voices/kandidaten/elevenlabs/stimmen.json
  - Beiträge zwischen zwei Jingles/Blockgrenzen werden als Dialog gesprochen, je Anfrage höchstens
    MAX_ZEICHEN Zeichen (API-Grenze 2000); Abschnitte eines Kapitels werden über previous_request_ids
    verkettet, damit Tonfall und Tempo nahtlos weiterlaufen (an Kapitelgrenzen beginnt die Kette neu)
  - die API liefert, wo im Abschnitt welcher Beitrag liegt (voice_segments): daraus das Transkript je Sprecher
  - Whisper hört jeden Abschnitt ab und meldet Auffälliges; neue Takes nur mit --versuche > 1 (kostet Credits)
  - Mix, Jingles, Kapitel, Cover wie bei Chatterbox (render.mischen)

  tts\\.venv\\Scripts\\python.exe tts\\render_elevenlabs.py --kosten           # nur Zeichen zählen, keine API
  tts\\.venv\\Scripts\\python.exe tts\\render_elevenlabs.py --stimmen          # nur Stimmen anlegen (kostenlos)
  tts\\.venv\\Scripts\\python.exe tts\\render_elevenlabs.py --block 0          # Probe
  tts\\.venv\\Scripts\\python.exe tts\\render_elevenlabs.py                    # ganze Folge

API-Key: Umgebungsvariable ELEVENLABS_API_KEY oder Datei tts/elevenlabs_api_key.txt (nur der Key, eine Zeile).
Der Key wird nie ausgegeben. Fertige Abschnitte liegen in tts/cache/elevenlabs/ – ein zweiter Lauf kostet nichts.
Ergebnis: tts/out/<datum>_elevenlabs[_bloecke_N].wav/.mp3 (+ _kapitel, _transkript), ganze Folge zusätzlich
in Folgen/<datum>/elevenlabs/
"""
import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

import render
from beitraege import ablauf, lade
from probe_gemini import FFMPEG, NAME, abschnitte

TTS = Path(__file__).resolve().parent
ROOT = TTS.parent
MODELL = "eleven_v4"
FORMAT = "mp3_44100_192"          # bestes Format im Creator-Plan (PCM 44,1 kHz erst ab Pro)
SR = 44100
MAX_ZEICHEN = 1900                # API: höchstens 2000 Zeichen Text je Dialog-Anfrage
FEHLER_OK = 0.05
STIMMEN = TTS / "voices" / "kandidaten" / "elevenlabs" / "stimmen.json"
CACHE = TTS / "cache" / "elevenlabs"
REGIE = {"lacht": "[laughs]", "lacht kurz": "[chuckles]", "Pause": "[short pause]", "seufzt": "[sighs]",
         "atmet durch": "[exhales]", "leise": "[quietly]"}


def client():
    from elevenlabs import ElevenLabs
    key = os.environ.get("ELEVENLABS_API_KEY")
    datei = TTS / "elevenlabs_api_key.txt"
    if not key and datei.exists():
        key = datei.read_text(encoding="utf-8").strip()
    if not key:
        sys.exit("Kein API-Key: ELEVENLABS_API_KEY setzen oder tts/elevenlabs_api_key.txt anlegen "
                 "(Key aus https://elevenlabs.io/app/developers/api-keys).")
    return ElevenLabs(api_key=key, timeout=300)


def credits(c) -> tuple[int, int]:
    s = c.user.subscription.get()
    return s.character_count, s.character_limit


def stimmen_anlegen(c) -> dict[str, str]:
    """Instant Voice Clone aus voices/aktuell/<rolle>.wav (einmalig, kostet keine Credits)."""
    STIMMEN.parent.mkdir(parents=True, exist_ok=True)
    stimmen = json.loads(STIMMEN.read_text(encoding="utf-8")) if STIMMEN.exists() else {}
    for rolle in ("SOMMER", "MONDSCHEIN"):
        wav = TTS / "voices" / "aktuell" / f"{rolle.lower()}.wav"
        schluessel = hashlib.sha1(wav.read_bytes()).hexdigest()[:8]
        if stimmen.get(rolle, {}).get("vorlage") != schluessel:
            print(f"Lege Stimme {NAME[rolle]} an (Instant Voice Clone aus {wav.name}) …", flush=True)
            with wav.open("rb") as f:
                v = c.voices.ivc.create(name=f"Pro und Contra {NAME[rolle]}", files=[(wav.name, f, "audio/wav")],
                                        remove_background_noise=False,
                                        description="Synthetische Stimme (Qwen3-TTS VoiceDesign), Podcast "
                                                    "„Pro und Contra – KI- und IT-News“",
                                        labels={"language": "de", "gender": "male"})
            stimmen[rolle] = {"id": v.voice_id, "vorlage": schluessel}
            STIMMEN.write_text(json.dumps(stimmen, ensure_ascii=False, indent=2), encoding="utf-8")
    return {r: s["id"] for r, s in stimmen.items()}


def eingaben(dialog: dict) -> list[str]:
    return [" ".join([REGIE[r] for r in e["regie"] if r in REGIE] + [e["text"]]) for e in dialog["beitraege"]]


def sprich(c, dialog: dict, stimme: dict[str, str], versuch: int = 1, vorher: list[str] | None = None):
    """Ein Dialog-Abschnitt in einer Anfrage -> (mono float32 in SR, Teile je Beitrag, Request-ID).
    vorher: Request-IDs der vorigen Abschnitte im selben Kapitel (höchstens 3), damit v4 Tonfall und Tempo
    nahtlos fortsetzt. Ergebnis im Cache."""
    anfrage = {"model_id": MODELL, "language_code": "de", "output_format": FORMAT,
               "inputs": [{"text": t, "voice_id": stimme[e["sprecher"]]}
                          for t, e in zip(eingaben(dialog), dialog["beitraege"])]}
    if versuch > 1:
        anfrage["seed"] = versuch
    if vorher:
        anfrage["previous_request_ids"] = vorher[-3:]
    CACHE.mkdir(parents=True, exist_ok=True)
    pfad = CACHE / (hashlib.sha1(json.dumps(anfrage, sort_keys=True).encode()).hexdigest()[:12] + ".json")
    if not pfad.exists():
        r = c.text_to_dialogue.with_raw_response.convert_with_timestamps(**anfrage)
        pfad.with_suffix(".mp3").write_bytes(base64.b64decode(r.data.audio_base_64))
        pfad.write_text(json.dumps({"request_id": r.headers.get("request-id"),
                                    "segmente": [s.model_dump() for s in r.data.voice_segments]}, indent=1),
                        encoding="utf-8")
    info = json.loads(pfad.read_text(encoding="utf-8"))
    if isinstance(info, list):                    # Cache vor der Verkettung: nur Segmente, keine Request-ID
        info = {"request_id": None, "segmente": info}
    roh = subprocess.run([FFMPEG, "-v", "error", "-i", str(pfad.with_suffix(".mp3")), "-f", "f32le", "-ac", "1",
                          "-ar", str(SR), "-"], capture_output=True, check=True).stdout
    y = np.frombuffer(roh, dtype=np.float32).copy()
    dauer = len(y) / SR
    # Beginn je Beitrag aus voice_segments; Ende = Beginn des nächsten Beitrags (Pausen bleiben beim Sprecher)
    beginn = {}
    for s in info["segmente"]:
        i = s["dialogue_input_index"]
        beginn[i] = min(beginn.get(i, s["start_time_seconds"]), s["start_time_seconds"])
    starts = [beginn.get(i) for i in range(len(dialog["beitraege"]))]
    for i in range(len(starts)):                  # fehlende Segmente: vom Vorgänger übernehmen
        starts[i] = starts[i] if starts[i] is not None else (starts[i - 1] if i else 0.0)
    starts[0] = 0.0
    grenzen = np.minimum(np.maximum.accumulate(np.array(starts + [dauer])), dauer)
    teile = [(float(grenzen[i]), float(grenzen[i + 1]), e) for i, e in enumerate(dialog["beitraege"])]
    return y, teile, info["request_id"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--block", type=int, nargs="*")
    ap.add_argument("--kosten", action="store_true", help="nur Zeichen zählen, keine API-Anfrage")
    ap.add_argument("--stimmen", action="store_true", help="nur Stimmen anlegen")
    ap.add_argument("--versuche", type=int, default=1, help="Takes je Abschnitt bei Whisper-Auffälligkeit (Credits!)")
    args = ap.parse_args()

    skript = max((ROOT / "06 Chatterbox Script").glob("*.jsonl"))
    plan = abschnitte(ablauf(render.auswaehlen(lade(skript), args.block, None)), MAX_ZEICHEN)
    dialoge = [e for e in plan if e["typ"] == "sprache"]
    zeichen = sum(len(t) for d in dialoge for t in eingaben(d))
    print(f"{skript.name}: {len(dialoge)} Abschnitte, {zeichen} Zeichen (max. {max(len(''.join(eingaben(d))) for d in dialoge)} "
          f"je Abschnitt)", flush=True)
    if args.kosten:
        return

    c = client()
    stimme = stimmen_anlegen(c)
    if args.stimmen:
        print(json.dumps({NAME[r]: v for r, v in stimme.items()}, indent=1))
        return
    vorher, limit = credits(c)
    print(f"Credits: {vorher} von {limit} verbraucht", flush=True)

    from render_gemini import Ohr
    ohr = Ohr()
    audio, auffaellig, sekunden = {}, [], 0.0
    kette, kapitel = [], None                     # Request-IDs der vorigen Abschnitte im selben Kapitel
    for nr, d in enumerate(dialoge, 1):
        if d["beitraege"][0]["block"] != kapitel:
            kette, kapitel = [], d["beitraege"][0]["block"]
        soll = " ".join(e["text"] for e in d["beitraege"])
        bester = None
        for versuch in range(1, args.versuche + 1):
            y, teile, rid = sprich(c, d, stimme, versuch, kette)
            fehler = render.fehlerquote(soll, ohr.hoeren(y, SR)[0])
            if bester is None or fehler < bester[0]:
                bester = (fehler, y, teile, rid)
            if fehler <= FEHLER_OK:
                break
            if versuch < args.versuche:
                print(f"  {d['id']} Take {versuch}: Fehlerquote {fehler:.3f}, neuer Versuch", flush=True)
        fehler, y, d["teile"], rid = bester
        kette = kette + [rid] if rid else []      # ohne ID (alter Cache) beginnt die Kette neu
        audio[d["id"]] = y
        sekunden += len(y) / SR
        if fehler > FEHLER_OK:
            auffaellig.append(d)
        print(f"[{nr}/{len(dialoge)}] {d['id']:<9} {len(d['beitraege']):3} Beiträge  {len(y) / SR:6.1f} s  "
              f"Fehler {fehler:.3f}  „{d['beitraege'][0]['original'][:50]}…“", flush=True)

    name = skript.stem + ("_elevenlabs" if args.block is None else "_elevenlabs_bloecke_" + "_".join(map(str, args.block)))
    wav, mp3, dauer = render.mischen(plan, audio, SR, name)
    if args.block is None:
        print(f"Abgelegt in {render.ablegen(name, skript.stem, 'elevenlabs')}")
    nachher, _ = credits(c)
    print(f"\nFertig: {wav}\n        {mp3}")
    print(f"Dauer {dauer / 60:.1f} min, Sprache {sekunden / 60:.1f} min, Credits verbraucht: {nachher - vorher} "
          f"(jetzt {nachher} von {limit})")
    if auffaellig:
        print("Bitte nachhören (Whisper-Fehlerquote hoch): " + ", ".join(
            f"{d['id']} („{d['beitraege'][0]['original'][:40]}…“)" for d in auffaellig))


if __name__ == "__main__":
    main()
