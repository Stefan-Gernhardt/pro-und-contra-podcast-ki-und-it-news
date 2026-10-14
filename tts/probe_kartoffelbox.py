"""Hörprobe mit Kartoffelbox (deutsch nachtrainiertes Chatterbox, SebastianBodza/Kartoffelbox-v0.1).

Nimmt einige Segmente aus dem Sprechskript (Cold Open, zwei Stellen mit [lacht]) und
rendert sie mit Kartoffelbox. [lacht] wird hier zum Lach-Tag <haha>, [lacht kurz] zu <chuckle>.

Stimmen: tts/voices/aktuell/sommer.wav bzw. mondschein.wav, falls vorhanden.
Sonst: SOMMER = eingebaute Kartoffelbox-Stimme, MONDSCHEIN = Standardstimme von
Chatterbox Multilingual (aus den bereits erzeugten Segmenten im Cache als Referenz).

Voraussetzung (Modell ist "gated"): auf huggingface.co/SebastianBodza/Kartoffelbox-v0.1
den Bedingungen zustimmen und einmalig  tts\\.venv\\Scripts\\hf auth login  ausführen.

Aufruf:  tts\\.venv\\Scripts\\python.exe tts\\probe_kartoffelbox.py
"""
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pyloudnorm
import soundfile as sf
import torch

import render

TTS = Path(__file__).resolve().parent
ROOT = TTS.parent
IDS = [f"b0-{i:03d}" for i in range(1, 14)] + ["b1-063", "b1-064", "b1-065", "b4-209", "b4-210", "b4-211"]
LACH_TAG = {"lacht": "<haha>", "lacht kurz": "<chuckle>"}


def referenz_mondschein() -> Path:
    eigene = TTS / "voices" / "aktuell" / "mondschein.wav"
    if eigene.exists():
        return eigene
    ziel = TTS / "voices" / "_referenz_chatterbox_standard.wav"
    if not ziel.exists():
        cache = sorted((TTS / "cache").glob("*/b0-009_*.wav")) + sorted((TTS / "cache").glob("*/b0-003_*.wav"))
        if not cache:
            raise SystemExit("Keine Referenz für MONDSCHEIN: erst render.py --block 0 laufen lassen.")
        teile = []
        for f in cache[:2]:
            y, _ = sf.read(f, dtype="float32")
            teile += [y, np.zeros(int(0.3 * render.SR_TTS), dtype=np.float32)]
        ziel.parent.mkdir(exist_ok=True)
        sf.write(ziel, np.concatenate(teile), render.SR_TTS)
    return ziel


def main():
    from chatterbox.tts import ChatterboxTTS
    from huggingface_hub import snapshot_download

    # händisch heruntergeladene Dateien haben Vorrang
    lokal = [d for d in (TTS / "models" / "kartoffelbox", ROOT / "download" / "Kartoffelbox")
             if (d / "t3_cfg.safetensors").exists()]
    if lokal:
        pfad = lokal[0]
    else:
        pfad = snapshot_download("SebastianBodza/Kartoffelbox-v0.1", ignore_patterns=["*.mp4"])
    geraet = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Lade Kartoffelbox auf {geraet} …", flush=True)
    model = ChatterboxTTS.from_local(pfad, geraet)

    conds = {}
    eigene = TTS / "voices" / "aktuell" / "sommer.wav"
    if eigene.exists():
        model.prepare_conditionals(str(eigene))
    conds["SOMMER"] = model.conds
    model.prepare_conditionals(str(referenz_mondschein()))
    conds["MONDSCHEIN"] = model.conds

    skript = max((ROOT / "06 Chatterbox Script").glob("*.jsonl"))
    segmente = {e["id"]: e for e in map(json.loads, skript.read_text(encoding="utf-8").splitlines())
                if e.get("id") in IDS}

    teile = []
    for sid in IDS:
        e = segmente[sid]
        text = e["text"]
        for r in e["regie"]:
            if r in LACH_TAG:
                text = f"{LACH_TAG[r]} {text}"
        model.conds = conds[e["sprecher"]]
        for versuch in range(3):
            torch.manual_seed(int(e["hash"][:8], 16) + versuch)
            with torch.inference_mode():
                wav = model.generate(text, exaggeration=e["exaggeration"], cfg_weight=e["cfg_weight"],
                                     temperature=0.6)
            wav = render.trimmen(wav.squeeze(0).cpu().numpy().astype(np.float32))
            if render.plausibel(wav, text):
                break
        print(f"{sid} {e['sprecher']:<10} {len(wav) / render.SR_TTS:5.1f} s  {text[:70]}", flush=True)
        teile += [np.zeros(int(e["pause_davor"] * render.SR_TTS), dtype=np.float32), wav,
                  np.zeros(int(e["pause_danach"] * render.SR_TTS), dtype=np.float32)]

    mix = np.concatenate(teile)
    lufs = pyloudnorm.Meter(render.SR_TTS).integrated_loudness(mix)
    mix = pyloudnorm.normalize.loudness(mix, lufs, -16.0)
    out = TTS / "out"
    out.mkdir(exist_ok=True)
    roh = out / "kartoffelbox_probe_roh.wav"
    sf.write(roh, mix, render.SR_TTS, subtype="FLOAT")
    ffmpeg = shutil.which("ffmpeg") or r"c:\prg\FFmpeg\bin\ffmpeg.exe"
    ziel = out / "kartoffelbox_probe.mp3"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(roh), "-af",
                    f"alimiter=limit={render.MAX_PEAK}:attack=5:release=50:level=0",
                    "-ar", "44100", "-b:a", "128k", str(ziel)], check=True)
    roh.unlink()
    print(f"\nFertig: {ziel} ({len(mix) / render.SR_TTS:.0f} s)")


if __name__ == "__main__":
    main()
