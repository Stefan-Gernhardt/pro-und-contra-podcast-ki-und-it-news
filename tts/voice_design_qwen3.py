"""Neue Stimmen per Beschreibung erfinden mit Qwen3-TTS VoiceDesign (keine echte Person als Vorlage).

Läuft im Qwen-venv:
  tts\\.venv-qwen\\Scripts\\python.exe tts\\voice_design_qwen3.py [name ...]

Jeder Entwurf spricht denselben deutschen Referenztext (fairer Vergleich) in zwei Takes, weil
VoiceDesign pro Seed eine etwas andere Stimme trifft. Ergebnis je Take in tts/voices/kandidaten/qwen_design/:
<name>_<take>.mp3 zum Anhören, .wav als Chatterbox-Vorlage, .txt (Wortlaut, für Qwen-Klonen).
Dazu hoerliste.mp3 mit allen Takes nacheinander (Reihenfolge wie ENTWUERFE, je 1,5 s Pause).
"""
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
import torch

TTS = Path(__file__).resolve().parent
ZIEL = TTS / "voices" / "kandidaten" / "qwen_design"   # noch unbewertet; Gewinner nach aktuell/, Rest nach verworfen/
MODELL = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
# RTX 20xx (Turing) kann kein bfloat16 und kein FlashAttention 2
DTYPE = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
SEEDS = (11, 23)
FFMPEG = r"c:\prg\FFmpeg\bin\ffmpeg.exe"

TEXT = ("Guten Abend und herzlich willkommen. Heute geht es um künstliche Intelligenz, um Rechenzentren "
        "und um die Frage, wer eigentlich haftet, wenn etwas schiefgeht. Ich habe mir die Zahlen genau angesehen. "
        "Und ehrlich gesagt: Die Sache ist komplizierter, als sie auf den ersten Blick wirkt.")

AKZENT = "He speaks clear standard German (Hochdeutsch) with native pronunciation and no foreign accent."
AKZENT_F = AKZENT.replace("He speaks", "She speaks")

# Beschreibung auf Englisch: das Modell folgt englischen Anweisungen am zuverlässigsten.
ENTWUERFE = {
    # Alternativen für Herrn Sommer (Pro): hell, zugewandt, begeistert
    "sommer_begeistert": "A man in his mid-thirties with a bright, warm baritone voice. Energetic and enthusiastic, "
                         "he sounds like he is smiling while he talks, speaks at a lively pace with lots of "
                         "melody. " + AKZENT,
    "sommer_moderator": "A man in his early forties with a clear, friendly mid-range voice, like an experienced "
                        "radio host. Relaxed, confident and optimistic, moderate pace, very articulate. " + AKZENT,
    # Alternativen für Herrn Mondschein (Contra): tief, skeptisch, trocken
    "mondschein_sonor": "A man around sixty with a deep, sonorous, slightly raspy voice. Calm, dry and ironic, "
                        "a sceptic who has seen it all, speaks slowly and deliberately with small pauses. " + AKZENT,
    "mondschein_scharf": "A man around fifty with a firm, slightly nasal lower-mid voice. Precise, critical and a "
                         "little impatient, like a sharp-tongued newspaper columnist, crisp consonants, "
                         "measured pace. " + AKZENT,
    # zweite Runde Mondschein: deutlich tiefer und älter als sommer_moderator (136 Hz), damit das Paar sich abhebt
    "mondschein_bass": "A man in his late sixties with a very deep, dark and resonant bass voice. Calm and grave, "
                       "dry understatement, speaks slowly with weight. " + AKZENT,
    "mondschein_brummig": "A man around seventy with a low, gravelly, slightly grumpy voice, like a grumbling retired "
                          "engineer. Sceptical and unhurried, a little gruff but not angry. " + AKZENT,
    "mondschein_bedaechtig": "A man in his late fifties with a low, serious bass-baritone voice. Thoughtful and "
                             "measured like an old judge, speaks slowly and calmly, every word weighed. " + AKZENT,
    # Frauenstimmen, z. B. für einen Gast oder eine dritte Rolle
    "frau_nachrichten": "A woman in her mid-thirties with a warm, clear mid-range voice. Composed, confident and "
                        "trustworthy like a news anchor, even pace, precise articulation. " + AKZENT_F,
    "frau_rauchig": "A woman around fifty with a low, slightly smoky voice. Witty and pointed, with a hint of "
                    "amusement, relaxed pace, expressive intonation. " + AKZENT_F,
}


def main():
    from qwen_tts import Qwen3TTSModel

    namen = sys.argv[1:] or list(ENTWUERFE)
    ZIEL.mkdir(parents=True, exist_ok=True)
    print(f"Lade {MODELL} ({DTYPE}) …", flush=True)
    model = Qwen3TTSModel.from_pretrained(MODELL, device_map="cuda:0", dtype=DTYPE, attn_implementation="sdpa")

    liste, sr = [], None
    for name in namen:
        for take, seed in enumerate(SEEDS, 1):
            torch.manual_seed(seed)
            wavs, sr = model.generate_voice_design(text=TEXT, instruct=ENTWUERFE[name], language="German")
            y = np.asarray(wavs[0], dtype=np.float32)
            y = y / max(np.abs(y).max(), 1e-6) * 0.891                        # Spitze auf -1 dBFS
            pfad = ZIEL / f"{name}_{take}.wav"
            sf.write(pfad, y, sr)
            subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(pfad), "-b:a", "160k",
                            str(pfad.with_suffix(".mp3"))], check=True)          # zum Anhören
            pfad.with_suffix(".txt").write_text(TEXT + "\n", encoding="utf-8")
            liste.append(y)
            print(f"{pfad.name:<26} {len(y) / sr:5.1f} s", flush=True)

    pause = np.zeros(int(1.5 * sr), dtype=np.float32)
    alles = np.concatenate([t for y in liste for t in (y, pause)])
    if len(namen) < len(ENTWUERFE):
        return                                    # Nachlieferung einzelner Entwürfe: alte Hörliste bleibt
    sf.write(ZIEL / "hoerliste.wav", alles, sr)
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(ZIEL / "hoerliste.wav"), "-b:a", "160k",
                    str(ZIEL / "hoerliste.mp3")], check=True)
    (ZIEL / "hoerliste.wav").unlink()
    print(f"\nFertig: {ZIEL / 'hoerliste.mp3'} ({len(alles) / sr / 60:.1f} min)")


if __name__ == "__main__":
    main()
