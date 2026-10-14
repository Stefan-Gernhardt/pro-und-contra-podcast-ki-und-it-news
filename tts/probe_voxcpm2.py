"""Hörprobe mit VoxCPM2 (OpenBMB, 2B, Stimmen klonen, 48 kHz), zum Vergleich mit Chatterbox.

Läuft im eigenen venv:
  tts\\.venv-voxcpm\\Scripts\\python.exe tts\\probe_voxcpm2.py [--dtype float16|float32]

Stimmen: tts/voices/aktuell/sommer.wav und mondschein.wav samt Wortlaut (sommer.txt / mondschein.txt),
damit VoxCPM2 die Vorlage fortsetzen kann („ultimate cloning“).
Das Checkpoint ist bfloat16; die RTX 2070 SUPER (Turing) rechnet bf16 nur emuliert,
darum lässt sich der Dtype umstellen. torch.compile (Triton) gibt es unter Windows nicht.
Ergebnis: tts/out/voxcpm2_probe.mp3
"""
import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path

import librosa
import numpy as np
import pyloudnorm
import soundfile as sf
import torch

TTS = Path(__file__).resolve().parent
ROOT = TTS.parent
IDS = [f"b0-{i:03d}" for i in range(1, 14)] + ["b1-063", "b1-064", "b1-065", "b4-209", "b4-210", "b4-211"]
MODELL = "openbmb/VoxCPM2"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dtype", default="bfloat16", choices=["bfloat16", "float16", "float32"])
    args = ap.parse_args()

    import voxcpm.model.voxcpm2 as vx2
    from voxcpm import VoxCPM
    vx2.pick_runtime_dtype = lambda geraet, dtype: args.dtype

    print(f"Lade {MODELL} ({args.dtype}) …", flush=True)
    model = VoxCPM.from_pretrained(MODELL, load_denoiser=False, optimize=False, device="cuda")
    sr = model.tts_model.sample_rate

    stimmen = {n: TTS / "voices" / "aktuell" / f"{n.lower()}.wav" for n in ("SOMMER", "MONDSCHEIN")}
    texte = {n: p.with_suffix(".txt").read_text(encoding="utf-8").strip() for n, p in stimmen.items()}

    skript = max((ROOT / "06 Chatterbox Script").glob("*.jsonl"))
    segmente = {e["id"]: e for e in map(json.loads, skript.read_text(encoding="utf-8").splitlines())
                if e.get("id") in IDS}

    teile, t0 = [], time.time()
    for sid in IDS:
        e = segmente[sid]
        n = e["sprecher"]
        torch.manual_seed(int(e["hash"][:8], 16))
        wav = model.generate(text=e["text"], prompt_wav_path=str(stimmen[n]), prompt_text=texte[n],
                             reference_wav_path=str(stimmen[n]), cfg_value=2.0, inference_timesteps=10,
                             normalize=False, denoise=False, retry_badcase=True, retry_badcase_max_times=3,
                             retry_badcase_ratio_threshold=6.0)
        wav, _ = librosa.effects.trim(np.asarray(wav, dtype=np.float32).reshape(-1), top_db=40)
        print(f"{sid} {n:<10} {len(wav) / sr:5.1f} s  {e['text'][:70]}", flush=True)
        teile += [np.zeros(int(e["pause_davor"] * sr), np.float32), wav,
                  np.zeros(int(e["pause_danach"] * sr), np.float32)]

    mix = np.concatenate(teile)
    print(f"Rechenzeit {time.time() - t0:.0f} s für {len(mix) / sr:.0f} s Audio", flush=True)
    mix = pyloudnorm.normalize.loudness(mix, pyloudnorm.Meter(sr).integrated_loudness(mix), -16.0)
    out = TTS / "out"
    out.mkdir(exist_ok=True)
    roh = out / "voxcpm2_probe_roh.wav"
    sf.write(roh, mix, sr, subtype="FLOAT")
    ffmpeg = shutil.which("ffmpeg") or r"c:\prg\FFmpeg\bin\ffmpeg.exe"
    ziel = out / "voxcpm2_probe.mp3"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(roh), "-af",
                    "alimiter=limit=0.891:attack=5:release=50:level=0",
                    "-ar", "44100", "-b:a", "128k", str(ziel)], check=True)
    roh.unlink()
    print(f"\nFertig: {ziel} ({len(mix) / sr:.0f} s)")


if __name__ == "__main__":
    main()
