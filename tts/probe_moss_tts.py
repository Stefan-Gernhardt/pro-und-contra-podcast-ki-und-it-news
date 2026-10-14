"""Hörprobe mit MOSS-TTS v1.5 (OpenMOSS, 8B, Stimmen klonen), zum Vergleich mit Chatterbox.

Läuft im eigenen venv (torch 2.9.1+cu128, transformers 5.0.0, Code aus tts/moss-tts-src):
  tts\\.venv-moss\\Scripts\\python.exe tts\\probe_moss_tts.py

Das Modell hat 8B Parameter (bf16: 17 GB). Auf der RTX 2070 SUPER (8 GB) wird es deshalb
4-bit quantisiert (bitsandbytes NF4) geladen, das kostet etwas Qualität. Auf der 5090 geht
es unquantisiert: --voll
MOSS klont nur aus dem Audio der Vorlage, ein Transkript braucht es nicht.
Ergebnis: tts/out/moss_tts_probe.mp3
"""
import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

# transformers 5.0 lädt sonst alle Gewichte parallel und unquantisiert auf die GPU (> 8 GB)
os.environ.setdefault("HF_DEACTIVATE_ASYNC_LOAD", "1")

import librosa
import numpy as np
import pyloudnorm
import soundfile as sf
import torch

TTS = Path(__file__).resolve().parent
ROOT = TTS.parent
IDS = [f"b0-{i:03d}" for i in range(1, 14)] + ["b1-063", "b1-064", "b1-065", "b4-209", "b4-210", "b4-211"]
MODELL = "OpenMOSS-Team/MOSS-TTS-v1.5"
# bf16 wie im Training; fp16 läuft über (NaN beim Sampling). Turing emuliert bf16 (langsamer).
DTYPE = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16

torch.backends.cuda.enable_cudnn_sdp(False)


def main():
    from huggingface_hub import snapshot_download
    from transformers import AutoModel, AutoProcessor, BitsAndBytesConfig

    ap = argparse.ArgumentParser()
    ap.add_argument("--voll", action="store_true", help="ohne 4-bit-Quantisierung laden (braucht ~18 GB VRAM)")
    args = ap.parse_args()

    # lokaler Ordner statt Repo-Name: der MOSS-Code baut den Namen sonst unter Windows mit \ zusammen
    pfad = snapshot_download(MODELL)
    processor = AutoProcessor.from_pretrained(pfad, trust_remote_code=True)
    # Der Audio-Tokenizer hat 7 GB (fp32) und passt neben dem 8B-Modell nicht auf 8 GB:
    # auf der 5090 (--voll) auf die GPU, sonst auf der CPU lassen (langsamer, aber es passt)
    if args.voll:
        processor.audio_tokenizer = processor.audio_tokenizer.to("cuda")
    sr = int(processor.model_config.sampling_rate)

    # Vorlagen selbst laden und vorab kodieren (umgeht torchaudio.load/torchcodec)
    refs = {}
    for n in ("SOMMER", "MONDSCHEIN"):
        y, _ = librosa.load(TTS / "voices" / "aktuell" / f"{n.lower()}.wav", sr=sr, mono=True)
        refs[n] = processor.encode_audios_from_wav([torch.from_numpy(y).unsqueeze(0)], sr,
                                                 int(processor.model_config.n_vq))[0]

    quant = None if args.voll else BitsAndBytesConfig(
        load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=DTYPE,
        bnb_4bit_use_double_quant=True, llm_int8_skip_modules=["lm_heads", "emb_ext"])
    print(f"Lade {MODELL} ({DTYPE}, {'voll' if args.voll else '4-bit NF4'}) …", flush=True)
    model = AutoModel.from_pretrained(pfad, trust_remote_code=True, attn_implementation="sdpa",
                                      dtype=DTYPE, quantization_config=quant, device_map="cuda:0")
    model.eval()
    print(f"  VRAM belegt: {torch.cuda.memory_allocated() / 2**30:.1f} GB", flush=True)

    skript = max((ROOT / "06 Chatterbox Script").glob("*.jsonl"))
    segmente = {e["id"]: e for e in map(json.loads, skript.read_text(encoding="utf-8").splitlines())
                if e.get("id") in IDS}

    teile, t0 = [], time.time()
    for sid in IDS:
        e = segmente[sid]
        torch.manual_seed(int(e["hash"][:8], 16))
        msg = processor.build_user_message(text=e["text"], reference=[refs[e["sprecher"]]], language="German")
        batch = processor([[msg]], mode="generation")
        with torch.no_grad():
            out = model.generate(input_ids=batch["input_ids"].to("cuda"),
                                 attention_mask=batch["attention_mask"].to("cuda"), max_new_tokens=2048)
        audio = processor.decode(out)[0].audio_codes_list[0]
        wav = audio.detach().float().cpu().numpy().reshape(-1)
        wav, _ = librosa.effects.trim(wav.astype(np.float32), top_db=40)
        print(f"{sid} {e['sprecher']:<10} {len(wav) / sr:5.1f} s  {e['text'][:70]}", flush=True)
        teile += [np.zeros(int(e["pause_davor"] * sr), np.float32), wav,
                  np.zeros(int(e["pause_danach"] * sr), np.float32)]

    mix = np.concatenate(teile)
    print(f"Rechenzeit {time.time() - t0:.0f} s für {len(mix) / sr:.0f} s Audio", flush=True)
    mix = pyloudnorm.normalize.loudness(mix, pyloudnorm.Meter(sr).integrated_loudness(mix), -16.0)
    out = TTS / "out"
    out.mkdir(exist_ok=True)
    roh = out / "moss_tts_probe_roh.wav"
    sf.write(roh, mix, sr, subtype="FLOAT")
    ffmpeg = shutil.which("ffmpeg") or r"c:\prg\FFmpeg\bin\ffmpeg.exe"
    ziel = out / "moss_tts_probe.mp3"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(roh), "-af",
                    "alimiter=limit=0.891:attack=5:release=50:level=0",
                    "-ar", "44100", "-b:a", "128k", str(ziel)], check=True)
    roh.unlink()
    print(f"\nFertig: {ziel} ({len(mix) / sr:.0f} s)")


if __name__ == "__main__":
    main()
