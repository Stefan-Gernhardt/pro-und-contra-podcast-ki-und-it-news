"""Wandelt ein Folgen-Skript (05 Script/<datum>.md) in ein Chatterbox-Sprechskript um.

Ergebnis in "06 Chatterbox Script/":
  <datum>.jsonl  – ein Eintrag pro Segment (Sprache, Pause, Jingle), Eingabe für render.py
  <datum>.md     – lesbare Vorschau zum Gegenlesen und Nachhören

Aufruf:  python tts/md_zu_chatterbox.py [05 Script/2026-09-27.md]
"""
import hashlib
import json
import re
import sys
from pathlib import Path

from num2words import num2words

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = ROOT / "05 Script"
OUT_DIR = ROOT / "06 Chatterbox Script"
AUSSPRACHE = Path(__file__).resolve().parent / "aussprache.json"

# --- Stimmen und Grundeinstellungen -------------------------------------------------
STIMME = {
    # Sommer: warm, schnell, begeisterungsfähig
    "SOMMER": {"exaggeration": 0.60, "cfg_weight": 0.50},
    # Mondschein: ruhig, trocken, langsam (niedriger cfg_weight = langsameres Tempo)
    "MONDSCHEIN": {"exaggeration": 0.40, "cfg_weight": 0.30},
}
TON_ERNST = {  # Block 2: "Ruhiger, ernster Ton"
    "SOMMER": {"exaggeration": 0.40, "cfg_weight": 0.40},
    "MONDSCHEIN": {"exaggeration": 0.30, "cfg_weight": 0.25},
}
MAX_ZEICHEN = 250          # Chatterbox bricht bei langen Texten ab (max. ~40 s pro Aufruf)
PAUSE_SPRECHERWECHSEL = 0.35
PAUSE_ZWISCHEN_CHUNKS = 0.15
PAUSE_UNTERBRECHUNG = 0.08  # Beitrag endet mit "…", der andere fällt ins Wort

MONATE = "Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember"
BUCHSTABE = "A-Za-zÄÖÜäöüßéè"


# --- Zahlen ---------------------------------------------------------------------------
def kardinal(n: int) -> str:
    w = num2words(n, lang="de")
    for alt, neu in (("einhundert", "hundert"), ("eintausend", "tausend")):
        if w.startswith(alt):
            w = neu + w[len(alt):]
    return w


def ordinal(n: int, endung: str) -> str:
    return num2words(n, lang="de", to="ordinal") + endung  # "achtzehnte" + "n"


def zahlen_normalisieren(t: str) -> str:
    # Datum/Ordinal: "am 18. September", "Der 26. September", "dem 10. und dem 15. September", "19. Jahrhundert"
    def ord_ersetzen(m):
        artikel = (m.group("art") or "").lower()
        endung = "" if artikel in ("der", "die", "das") else "n"
        return (m.group("art") + " " if m.group("art") else "") + ordinal(int(m.group("n")), endung)

    folge = rf"(?=\s+(?:{MONATE}|Jahrhundert|und\s+dem\s+\d{{1,2}}\.\s+(?:{MONATE})|bis\s+(?:zum\s+)?\d{{1,2}}\.\s+(?:{MONATE})))"
    t = re.sub(rf"(?:(?P<art>\b[Dd]er|\b[Dd]em|\b[Aa]m|\b[Vv]om|\b[Zz]um|\b[Ii]m)\s+)?(?P<n>\d{{1,2}})\.{folge}", ord_ersetzen, t)

    # Dezimalzahlen: "3,6" -> "drei Komma sechs"
    t = re.sub(r"\b(\d+),(\d+)\b",
               lambda m: kardinal(int(m.group(1))) + " Komma " + " ".join(kardinal(int(c)) for c in m.group(2)), t)
    # Tausenderpunkte: "600.000", "3.600"
    t = re.sub(r"\b\d{1,3}(?:\.\d{3})+\b", lambda m: kardinal(int(m.group(0).replace(".", ""))), t)
    # Jahreszahlen 1100–2099
    t = re.sub(r"\b(1[1-9]\d\d|20\d\d)\b", lambda m: num2words(int(m.group(1)), lang="de", to="year"), t)
    # Übrige ganze Zahlen
    t = re.sub(r"\b\d+\b", lambda m: kardinal(int(m.group(0))), t)
    return t


# --- Aussprache -----------------------------------------------------------------------
def lade_aussprache():
    daten = json.loads(AUSSPRACHE.read_text(encoding="utf-8"))
    paare = [(k, v) for k, v in daten.items() if not k.startswith("_")]
    paare.sort(key=lambda kv: len(kv[0]), reverse=True)
    return [(re.compile(rf"(?<![{BUCHSTABE}]){re.escape(k)}(?![{BUCHSTABE}])"), k, v) for k, v in paare]


def aussprache_anwenden(t: str, regeln, zaehler: dict) -> str:
    for rx, k, v in regeln:
        t, n = rx.subn(v, t)
        if n:
            zaehler[k] = zaehler.get(k, 0) + n
    return t


def sprechtext(t: str, regeln, zaehler) -> str:
    t = t.replace("„", "").replace("“", "").replace("”", "").replace('"', "")
    t = t.replace("*", "")
    t = aussprache_anwenden(t, regeln, zaehler)  # vor den Zahlen, wegen "Opus 4.6", "Pets.com"
    t = zahlen_normalisieren(t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"^…\s*", "", t)                    # Zögern am Anfang wird zur Pause
    return t


# --- Chunking -------------------------------------------------------------------------
def in_chunks(text: str) -> list[str]:
    saetze = re.split(r"(?<=[.!?…])\s+", text)
    teile = []
    for s in saetze:
        if len(s) <= MAX_ZEICHEN:
            teile.append(s)
            continue
        # zu langer Satz: an Kommas/Doppelpunkten teilen
        stueck = ""
        for p in re.split(r"(?<=[,:;])\s+", s):
            if stueck and len(stueck) + 1 + len(p) > MAX_ZEICHEN:
                teile.append(stueck)
                stueck = p
            else:
                stueck = f"{stueck} {p}".strip()
        if stueck:
            teile.append(stueck)
    chunks, akt = [], ""
    for s in teile:
        if akt and len(akt) + 1 + len(s) > MAX_ZEICHEN:
            chunks.append(akt)
            akt = s
        else:
            akt = f"{akt} {s}".strip()
    if akt:
        chunks.append(akt)
    return chunks


# --- Regieanweisungen -----------------------------------------------------------------
def regie_deuten(r: str) -> dict:
    """Übersetzt eine Regieanweisung in Aktionen."""
    s = r.lower()
    if "kein jingle" in s and "ernst" in s:
        return {"ton": "ernst", "pause": 1.0}
    if "längere pause" in s and "jingle" in s:
        return {"pause": 2.0, "jingle": ("uebergang_lang", 5.0)}
    if "musikteppich" in s:
        return {"jingle": ("uebergang_ruhig", 4.0), "teppich": True}
    if "übergangs-jingle" in s:
        return {"jingle": ("uebergang", 3.0)}
    if "intro-sound" in s:
        return {"jingle": ("intro_sound", 1.5)}
    if "intro-jingle" in s:
        return {"jingle": ("intro", 5.0)}
    if "outro-jingle" in s:
        return {"jingle": ("outro", 6.0)}
    if "kurze pause" in s:
        return {"pause": 0.6}
    if "atmet durch" in s:
        return {"pause": 0.8}
    if "pause" in s:
        return {"pause": 0.9}
    if "lacht kurz" in s:
        return {"exag": 0.15}
    if "lacht" in s:
        return {"exag": 0.25}
    if "leise" in s:
        return {"exag": -0.2, "gain": -3.0}
    raise ValueError(f"Unbekannte Regieanweisung: [{r}]")


# --- Hauptprogramm --------------------------------------------------------------------
def umwandeln(md_pfad: Path):
    regeln = lade_aussprache()
    zaehler: dict = {}
    zeilen = md_pfad.read_text(encoding="utf-8").splitlines()

    eintraege = []
    block, ton, titel = None, "normal", {}
    seg_nr = 0
    unbekannt = set()
    teppich_an = False

    for zeile in zeilen:
        z = zeile.strip()
        m = re.match(r"^## (\d+) · (.+)$", z)
        if m:
            block, ton = int(m.group(1)), "normal"
            titel[block] = m.group(2)
            eintraege.append({"typ": "block", "block": block, "titel": m.group(2)})
            continue
        if z.startswith("## "):          # "## Quellen …" – ab hier nichts mehr sprechen
            if block is not None:
                break
            continue
        if block is None or not z or z == "---":
            continue

        m = re.match(r"^\[(.+)\]$", z)
        if m:
            a = regie_deuten(m.group(1))
            if a.get("ton"):
                ton = a["ton"]
            if a.get("pause"):
                eintraege.append({"typ": "pause", "sekunden": a["pause"], "regie": m.group(1)})
            if a.get("jingle"):
                name, dauer = a["jingle"]
                if teppich_an:  # der Teppich läuft bis in den nächsten Jingle hinein aus
                    eintraege.append({"typ": "teppich", "aktion": "stop"})
                    teppich_an = False
                eintraege.append({"typ": "jingle", "name": name, "ersatz_stille": dauer, "regie": m.group(1)})
            if a.get("teppich"):
                eintraege.append({"typ": "teppich", "aktion": "start"})
                teppich_an = True
            continue

        m = re.match(r"^\*\*(SOMMER|MONDSCHEIN):\*\*\s*(.+)$", z)
        if not m:
            unbekannt.add(z[:60])
            continue
        sprecher, rest = m.group(1), m.group(2)
        basis = (TON_ERNST if ton == "ernst" else STIMME)[sprecher]

        # Beitrag an Regieanweisungen aufteilen; jede Anweisung gilt für das folgende Stück
        stuecke = re.split(r"\s*\[([^\]]+)\]\s*", rest)
        anweisung_davor = []
        beitrag_segmente = []
        for i, teil in enumerate(stuecke):
            if i % 2 == 1:
                anweisung_davor.append(teil)
                continue
            if not teil.strip():
                continue
            exag, cfg, gain, pause_davor = basis["exaggeration"], basis["cfg_weight"], 0.0, 0.0
            if teil.strip().startswith("…"):
                pause_davor += 0.5
            for r in anweisung_davor:
                a = regie_deuten(r)
                pause_davor += a.get("pause", 0.0)
                exag += a.get("exag", 0.0)
                gain += a.get("gain", 0.0)
            exag = round(min(max(exag, 0.25), 0.9), 2)
            text = sprechtext(teil, regeln, zaehler)
            for k, chunk in enumerate(in_chunks(text)):
                seg_nr += 1
                beitrag_segmente.append({
                    "typ": "sprache",
                    "id": f"b{block}-{seg_nr:03d}",
                    "block": block,
                    "sprecher": sprecher,
                    "text": chunk,
                    "exaggeration": exag,
                    "cfg_weight": cfg,
                    "gain_db": gain,
                    "pause_davor": round(pause_davor, 2) if k == 0 else 0.0,
                    "pause_danach": PAUSE_ZWISCHEN_CHUNKS,
                    "regie": list(anweisung_davor) if k == 0 else [],
                    "original": teil.strip() if k == 0 else "",
                })
            anweisung_davor = []
        if beitrag_segmente:
            beitrag_segmente[-1]["pause_danach"] = (
                PAUSE_UNTERBRECHUNG if rest.rstrip().endswith("…") else PAUSE_SPRECHERWECHSEL)
        eintraege.extend(beitrag_segmente)

    if teppich_an:
        eintraege.append({"typ": "teppich", "aktion": "stop"})
    if unbekannt:
        raise SystemExit("Nicht erkannte Zeilen:\n" + "\n".join(sorted(unbekannt)))

    for e in eintraege:
        if e["typ"] == "sprache":
            e["hash"] = hashlib.sha1(json.dumps(
                [e["sprecher"], e["text"], e["exaggeration"], e["cfg_weight"]], ensure_ascii=False
            ).encode()).hexdigest()[:12]
    return eintraege, zaehler


def schreiben(md_pfad: Path, eintraege, zaehler):
    OUT_DIR.mkdir(exist_ok=True)
    datum = md_pfad.stem
    jsonl = OUT_DIR / f"{datum}.jsonl"
    with jsonl.open("w", encoding="utf-8", newline="\n") as f:
        for e in eintraege:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    sprache = [e for e in eintraege if e["typ"] == "sprache"]
    zeichen = sum(len(e["text"]) for e in sprache)
    v = [f"# Chatterbox-Sprechskript – Folge vom {datum}",
         "",
         f"Erzeugt aus `05 Script/{md_pfad.name}` mit `tts/md_zu_chatterbox.py`. "
         f"Maschinenlesbare Fassung: `{jsonl.name}` (Eingabe für `tts/render.py`).",
         "",
         f"**Segmente:** {len(sprache)} Sprachsegmente (max. {MAX_ZEICHEN} Zeichen) · "
         f"{sum(e['typ'] == 'pause' for e in eintraege)} Pausen · "
         f"{sum(e['typ'] == 'jingle' for e in eintraege)} Jingles · "
         f"{zeichen:,}".replace(",", ".") + " Zeichen Sprechtext",
         "",
         "**Stimmen:** SOMMER exaggeration 0.60 / cfg 0.50 · MONDSCHEIN 0.40 / 0.30 · "
         "Block 2 (ernster Ton): SOMMER 0.40 / 0.40 · MONDSCHEIN 0.30 / 0.25",
         "",
         "**Regie:** [Pause] 0,9 s · [Kurze Pause] 0,6 s · [atmet durch] 0,8 s · [lacht] +0,25 exaggeration · "
         "[lacht kurz] +0,15 · [leise] −0,2 exaggeration und −3 dB · Jingles und Klangteppich laut "
         "`tts/sounds.json` aus `sounds/`, fehlt eine Datei, steht Stille als Platzhalter.",
         "",
         "Der Sprechtext ist absichtlich phonetisch geschrieben (z. B. „Ka-I“ statt „KI“). "
         "Änderungen an der Aussprache bitte in `tts/aussprache.json` machen und den Konverter neu laufen lassen, "
         "nicht hier.",
         "",
         "## Angewendete Aussprache-Ersetzungen",
         "",
         "| Skript | Chatterbox | Anzahl |",
         "|---|---|---:|"]
    regeln = {k: val for k, val in json.loads(AUSSPRACHE.read_text(encoding="utf-8")).items()}
    for k, n in sorted(zaehler.items(), key=lambda kv: (-kv[1], kv[0])):
        v.append(f"| {k} | {regeln[k]} | {n} |")
    for e in eintraege:
        if e["typ"] == "block":
            v += ["", f"## {e['block']} · {e['titel']}", ""]
        elif e["typ"] == "pause":
            v.append(f"> ⏸ Pause {e['sekunden']:.1f} s  _[{e['regie']}]_\n")
        elif e["typ"] == "teppich":
            v.append("> 🎶 Klangteppich " + ("setzt leise ein" if e["aktion"] == "start" else "blendet aus") + "\n")
        elif e["typ"] == "jingle":
            v.append(f"> 🎵 Jingle `{e['name']}`  _[{e['regie']}]_\n")
        else:
            info = f"ex {e['exaggeration']:.2f} · cfg {e['cfg_weight']:.2f}"
            if e["gain_db"]:
                info += f" · {e['gain_db']:+.0f} dB"
            if e["pause_davor"]:
                info += f" · Pause davor {e['pause_davor']:.1f} s"
            if e["regie"]:
                info += " · [" + "] [".join(e["regie"]) + "]"
            v.append(f"`{e['id']}` **{e['sprecher']}** <sub>{info}</sub>  ")
            v.append(e["text"] + "\n")
    (OUT_DIR / f"{datum}.md").write_text("\n".join(v) + "\n", encoding="utf-8", newline="\n")
    return jsonl, len(sprache), zeichen


if __name__ == "__main__":
    if len(sys.argv) > 1:
        pfad = Path(sys.argv[1])
    else:
        pfad = max(SCRIPT_DIR.glob("*.md"))
    eintraege, zaehler = umwandeln(pfad)
    jsonl, n, zeichen = schreiben(pfad, eintraege, zaehler)
    print(f"{jsonl}: {n} Sprachsegmente, {zeichen} Zeichen")
