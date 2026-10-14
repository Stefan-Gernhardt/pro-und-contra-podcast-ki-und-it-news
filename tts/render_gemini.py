"""Ganze Folge mit Gemini 3.8 Flash TTS im Dialog-Modus (Stimmen und Anfragen wie probe_gemini.py).

  - Beiträge zwischen zwei Jingles/Blockgrenzen werden als Dialog gesprochen, je Anfrage höchstens
    MAX_ZEICHEN Zeichen (geteilt an der Beitragsgrenze)
  - Whisper hört jeden Abschnitt ab; fehlt Text oder kommt etwas doppelt, wird neu erzeugt (bis VERSUCHE,
    der beste Take zählt)
  - Whisper-Zeitstempel liefern, wo im Abschnitt welcher Beitrag beginnt: daraus Transkript je Sprecher
  - Mix, Jingles, Kapitel, Cover wie bei Chatterbox (render.mischen)

  tts\\.venv\\Scripts\\python.exe tts\\render_gemini.py              # ganze Folge (neuestes .jsonl)
  tts\\.venv\\Scripts\\python.exe tts\\render_gemini.py --block 2    # nur einzelne Blöcke

Ergebnis: tts/out/<datum>_gemini.wav/.mp3 (+ _kapitel, _transkript), ganze Folge zusätzlich in
Folgen/<datum>/gemini/. Veröffentlicht wird tts/out/<datum>.mp3:
dafür die Dateien umbenennen/kopieren (siehe Ausgabe am Ende).
"""
import argparse
from pathlib import Path

import numpy as np

import render
from beitraege import ablauf, lade
from probe_gemini import NAME, abschnitte, client, design_stimme, sprich

TTS = Path(__file__).resolve().parent
ROOT = TTS.parent
MAX_ZEICHEN = 2500      # ≈ 2,5–3 min Sprache pro Anfrage
VERSUCHE = 3
FEHLER_OK = 0.05        # Wortfehlerquote laut Whisper, bis zu der ein Take als sauber gilt


class Ohr:
    """Whisper mit Zeitstempeln (auch für Abschnitte über 30 s).

    Mit GPU: transformers-Pipeline. Ohne GPU (Cloud-Routine): faster-whisper int8, dort knapp viermal so
    schnell bei gleichem Text (Test 2026-10-08: Echtzeitfaktor 0,47 statt 1,75); torch braucht es dann nicht."""

    def __init__(self):
        try:
            import torch
            gpu = torch.cuda.is_available()
        except ImportError:
            gpu = False
        if gpu:
            from transformers import pipeline
            self.asr = pipeline("automatic-speech-recognition", model=render.Ohr.MODELL,
                                dtype=torch.float16, device="cuda:0")
            self.fw = None
        else:
            from faster_whisper import WhisperModel
            self.fw = WhisperModel(render.Ohr.MODELL.split("whisper-")[-1], device="cpu", compute_type="int8")

    def hoeren(self, y: np.ndarray, sr: int) -> tuple[str, list[tuple[str, float, float]]]:
        import librosa
        y16 = librosa.resample(y, orig_sr=sr, target_sr=16000).astype(np.float32)
        if self.fw:
            # Audio als Array übergeben: mit Dateipfad scheitert faster-whisper in der Cloud an einem Paketkonflikt
            segmente, _ = self.fw.transcribe(y16, language="de")
            stuecke = [(s.text, s.start, s.end) for s in segmente]
            return "".join(t for t, _, _ in stuecke).strip(), stuecke
        r = self.asr({"raw": y16, "sampling_rate": 16000},
                     return_timestamps=True, chunk_length_s=30, generate_kwargs={"language": "german"})
        stuecke = [(c["text"], c["timestamp"][0], c["timestamp"][1]) for c in r["chunks"]]
        return r["text"].strip(), stuecke


def zeiten(dialog: dict, stuecke: list[tuple[str, float, float]], dauer: float) -> list[tuple[float, float, dict]]:
    """Beginn/Ende jedes Beitrags im Abschnitt: Zeichenposition im Skript -> Zeit über die Whisper-Stücke
    (stückweise linear interpoliert)."""
    pos, punkte = 0, [(0, 0.0)]
    for text, a, b in stuecke:
        punkte.append((pos, a if a is not None else punkte[-1][1]))
        pos += len(render.vergleichstext(text))
        punkte.append((pos, b if b is not None else dauer))
    punkte.append((pos, dauer))
    xs, ts = zip(*punkte)
    laengen = [len(render.vergleichstext(e["text"])) for e in dialog["beitraege"]]
    skala = pos / max(sum(laengen), 1)
    grenzen = np.interp(np.cumsum([0] + laengen) * skala, xs, ts)
    grenzen[0], grenzen[-1] = 0.0, dauer
    # Whisper liefert an Stückgrenzen manchmal rückwärts laufende Zeitstempel: Grenzen nur vorwärts
    grenzen = np.minimum(np.maximum.accumulate(grenzen), dauer)
    return [(float(grenzen[i]), float(grenzen[i + 1]), e) for i, e in enumerate(dialog["beitraege"])]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--block", type=int, nargs="*")
    ap.add_argument("--sommer", default="design")
    ap.add_argument("--mondschein", default="design")
    args = ap.parse_args()

    c = client()
    wahl = {"SOMMER": args.sommer, "MONDSCHEIN": args.mondschein}
    stimme = {r: design_stimme(c, r) if w == "design" else w for r, w in wahl.items()}

    skript = max((ROOT / "06 Chatterbox Script").glob("*.jsonl"))
    plan = abschnitte(ablauf(render.auswaehlen(lade(skript), args.block, None)), MAX_ZEICHEN)
    dialoge = [e for e in plan if e["typ"] == "sprache"]
    ohr = Ohr()

    audio, sr, auffaellig, sekunden = {}, None, [], 0.0
    for nr, d in enumerate(dialoge, 1):
        soll = " ".join(e["text"] for e in d["beitraege"])
        bester = None
        for versuch in range(1, VERSUCHE + 1):
            y, sr = sprich(c, d, stimme, neu=False, versuch=versuch)
            ist, stuecke = ohr.hoeren(y, sr)
            fehler = render.fehlerquote(soll, ist)
            if bester is None or fehler < bester[0]:
                bester = (fehler, y, stuecke)
            if fehler <= FEHLER_OK:
                break
            print(f"  {d['id']} Take {versuch}: Fehlerquote {fehler:.3f}, neuer Versuch", flush=True)
        fehler, y, stuecke = bester
        audio[d["id"]] = y
        d["teile"] = zeiten(d, stuecke, len(y) / sr)
        sekunden += len(y) / sr
        if fehler > FEHLER_OK:
            auffaellig.append(d)
        sprecher = "/".join(sorted({NAME[e["sprecher"]] for e in d["beitraege"]}))
        print(f"[{nr}/{len(dialoge)}] {d['id']:<9} {len(d['beitraege']):3} Beiträge  {len(y) / sr:6.1f} s  "
              f"Fehler {fehler:.3f}  {sprecher}  „{d['beitraege'][0]['original'][:50]}…“", flush=True)

    name = skript.stem + ("_gemini" if args.block is None else "_gemini_bloecke_" + "_".join(map(str, args.block)))
    wav, mp3, dauer = render.mischen(plan, audio, sr, name)
    if args.block is None:
        print(f"Abgelegt in {render.ablegen(name, skript.stem, 'gemini')}")
    print(f"\nFertig: {wav}\n        {mp3}")
    print(f"Dauer {dauer / 60:.1f} min, Sprache {sekunden / 60:.1f} min ≈ {sekunden * 25 * 9e-6:.2f} $")
    if auffaellig:
        print("Bitte nachhören (Whisper-Fehlerquote hoch): " + ", ".join(
            f"{d['id']} („{d['beitraege'][0]['original'][:40]}…“)" for d in auffaellig))


if __name__ == "__main__":
    main()
