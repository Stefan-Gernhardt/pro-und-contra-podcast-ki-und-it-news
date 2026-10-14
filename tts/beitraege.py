"""Ganze Sprechbeiträge für die mehrsprachigen Modelle (MOSS-TTS, Qwen3-TTS).

Das Sprechskript (06 Chatterbox Script/<datum>.jsonl) ist für Chatterbox gebaut: Beiträge in
250-Zeichen-Stücke zerlegt und englische Wörter in deutsche Lautschrift umgeschrieben
(„Smart Houm“, „Tuschee“). Mehrsprachige Modelle sprechen das Original besser. Hier werden die
Stücke wieder zu ganzen Beiträgen zusammengefasst und aus dem Feld "original" neu aufbereitet:
nur die Ersetzungen aus aussprache_mehrsprachig.json, Zahlen ausgeschrieben wie bei Chatterbox.
Jingles, Pausen und Teppich bleiben unverändert im Ablauf, damit render.mischen() alles
genauso mischt wie bei Chatterbox.
"""
import json
from pathlib import Path

import md_zu_chatterbox as md

TTS = Path(__file__).resolve().parent
AUSSPRACHE = TTS / "aussprache_mehrsprachig.json"


def lade(skript: Path) -> list[dict]:
    return [json.loads(z) for z in skript.read_text(encoding="utf-8").splitlines() if z.strip()]


def ablauf(eintraege: list[dict]) -> list[dict]:
    """Ersetzt die Chatterbox-Stücke eines Beitrags durch einen einzigen Sprach-Eintrag
    (ID des ersten Stücks, Pause danach vom letzten Stück, Text neu aus "original")."""
    md.AUSSPRACHE = AUSSPRACHE
    regeln = md.lade_aussprache()
    liste, letzter = [], None
    for e in eintraege:
        if e["typ"] != "sprache":
            liste.append(e)
            continue
        if e["original"]:
            letzter = {"typ": "sprache", "id": e["id"], "ids": [e["id"]], "block": e["block"],
                       "sprecher": e["sprecher"], "original": e["original"],
                       "text": md.sprechtext(e["original"], regeln, {}),
                       "gain_db": e.get("gain_db", 0.0), "regie": e["regie"],
                       "pause_davor": e["pause_davor"], "pause_danach": e["pause_danach"]}
            liste.append(letzter)
        elif letzter is not None:               # Fortsetzungs-Stück des vorigen Beitrags
            letzter["ids"].append(e["id"])
            letzter["pause_danach"] = e["pause_danach"]
    return liste


def beitraege(skript: Path) -> list[dict]:
    return [e for e in ablauf(lade(skript)) if e["typ"] == "sprache"]
