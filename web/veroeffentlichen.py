"""Veröffentlicht den Podcast auf Cloudflare Pages (pro-und-contra-ki-und-it-news.pages.dev).

Ablauf für eine neue Folge:
  1. tts/render.py erzeugt tts/out/<datum>.mp3 und tts/out/<datum>_kapitel.json
     (veröffentlichte Folgen liest das Skript aus 09 Folgen/<datum>/<engine>/, siehe quellen())
  2. web/folgen/<datum>.json anlegen (Titel, Kurztext, Text und Links je Kapitel).
     Fehlt die Datei, legt dieses Skript ein Gerüst an und bricht ab, damit man es ausfüllt.
  3. python web/veroeffentlichen.py
     → legt die MP3s ab, baut public/ (Seite, Feed, Cover, Kapitel, Transkript) und deployt das Pages-Projekt.
     functions/audio/[[pfad]].js (Logik in src/audio.js) beantwortet dort die Range-Anfragen fürs Vorspulen.

Die MP3s liegen je nach podcast.json → "audio" als statische Datei in public/audio/ ("assets", höchstens
25 MiB, notfalls mit kleinerer Bitrate neu kodiert) oder im R2-Bucket ("r2", dann in wrangler.jsonc die
r2_buckets-Bindung einkommentieren).

Optionen: --nur-bauen (nichts hochladen, nicht deployen), --ohne-upload (nur Seite/Feed deployen),
--spotify <datum> (Titel und Beschreibung der Folge für Spotify for Creators ausgeben und als
09 Folgen/<datum>/<datum>_spotify.txt ablegen; Spotify nimmt den Feed von pages.dev nicht an, der Nutzer lädt
jede Folge selbst im Browser hoch, mit der normalen 160-kbit-MP3 aus 09 Folgen/<datum>/elevenlabs/),
--facebook <datum> (Text für den Facebook-Beitrag zur Folge ausgeben und als 09 Folgen/<datum>/<datum>_facebook.txt
ablegen; das Video steht in web/folgen/<datum>.json im Feld „youtube“ als Video-ID, sonst nimmt er die Playlist).
"""
import argparse
import html
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

WEB = Path(__file__).resolve().parent
ROOT = WEB.parent
OUT = ROOT / "tts" / "out"
ARCHIV = ROOT / "09 Folgen"
PUBLIC = WEB / "public"
FOLGEN = WEB / "folgen"
KONFIG = WEB / "podcast.json"
BUCKET = "pro-und-contra-audio"
PROJEKT = "pro-und-contra-ki-und-it-news"  # Name des Pages-Projekts, ergibt <PROJEKT>.pages.dev

FFMPEG = shutil.which("ffmpeg") or r"c:\prg\FFmpeg\bin\ffmpeg.exe"
FFPROBE = shutil.which("ffprobe") or str(Path(FFMPEG).with_name("ffprobe.exe"))


def lesen(pfad):
    return json.loads(pfad.read_text(encoding="utf-8"))


def schreiben(pfad, daten):
    pfad.write_text(json.dumps(daten, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def wrangler(*args, ausgabe=False):
    npx = shutil.which("npx") or "npx"
    cmd = [npx, "wrangler", *args]
    print("›", "wrangler", *args)
    if ausgabe:
        r = subprocess.run(cmd, cwd=WEB, capture_output=True, text=True, encoding="utf-8", errors="replace")
        print(r.stdout + r.stderr)
        return r
    return subprocess.run(cmd, cwd=WEB, check=True)


def dauer(mp3):
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp3)],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def zeitmarke(s):
    s = int(s)  # wie render.zeitmarke: abschneiden, nicht runden
    h, m, sek = s // 3600, s // 60 % 60, s % 60
    return f"{h}:{m:02d}:{sek:02d}" if h else f"{m:02d}:{sek:02d}"


def datum_de(datum):
    j, m, t = datum.split("-")
    return f"{t}.{m}.{j}"


def geruest(datum, kapitel):
    """Legt folgen/<datum>.json mit den Kapiteltiteln an; Texte und Links füllt man von Hand."""
    pfad = FOLGEN / f"{datum}.json"
    nummer = len(list(FOLGEN.glob("*.json"))) + 1
    schreiben(pfad, {"nummer": nummer, "titel": "", "kurz": "",
                     "kapitel": [{"text": "", "links": []} for _ in kapitel]})
    return pfad


def quellen():
    """Datum -> Basispfad ohne Endung (<basis>.mp3, <basis>_kapitel.json, <basis>_transkript.json/.vtt).
    Veröffentlichte Folgen (mit web/folgen/<datum>.json) liegen in 09 Folgen/<datum>/<engine>/<datum>_<engine>.*
    (Engine aus der json, sonst ElevenLabs); eine frisch gerenderte tts/out/<datum>.mp3 hat Vorrang. In der Cloud fehlt
    09 Folgen (gitignored): Routine und Workflow holen die alten Folgen vorher von der Live-Seite nach tts/out.
    Fehlt eine Folge hier, verschwindet sie beim nächsten Deploy von der Seite und aus dem Feed."""
    gefunden = {}
    for ordner in sorted(ARCHIV.glob("????-??-??")):
        datum = ordner.name
        meta_pfad = FOLGEN / f"{datum}.json"
        if not meta_pfad.exists():   # noch nicht veröffentlicht (z. B. Testlauf im Workflow): nicht mitnehmen
            continue
        engine = lesen(meta_pfad).get("engine") or "elevenlabs"
        basis = ordner / engine / f"{datum}_{engine}"
        if basis.with_suffix(".mp3").exists():
            gefunden[datum] = basis
    for mp3 in OUT.glob("????-??-??.mp3"):
        gefunden[mp3.stem] = mp3.with_suffix("")
    for meta_pfad in FOLGEN.glob("????-??-??.json"):
        if meta_pfad.stem not in gefunden:
            sys.exit(f"Folge {meta_pfad.stem} ist veröffentlicht, aber ihre MP3 fehlt in 09 Folgen und tts/out.")
    return gefunden


def folgen_sammeln():
    folgen = []
    for datum, basis in sorted(quellen().items()):
        mp3 = basis.with_suffix(".mp3")
        kap_pfad = basis.parent / f"{basis.name}_kapitel.json"
        if not kap_pfad.exists():
            sys.exit(f"Kapitel fehlen: {kap_pfad}")
        kapitel = lesen(kap_pfad)["chapters"]
        meta_pfad = FOLGEN / f"{datum}.json"
        if not meta_pfad.exists():
            sys.exit(f"Neue Folge {datum}: Gerüst {geruest(datum, kapitel)} angelegt. Bitte Titel, Kurztext und Kapiteltexte ausfüllen.")
        meta = lesen(meta_pfad)
        if not meta.get("titel") or not meta.get("kurz"):
            sys.exit(f"{meta_pfad}: Titel oder Kurztext fehlt.")
        if len(meta["kapitel"]) != len(kapitel):
            sys.exit(f"{meta_pfad}: {len(meta['kapitel'])} Kapiteltexte, aber {len(kapitel)} Kapitel in der MP3.")
        tr_pfad = basis.parent / f"{basis.name}_transkript.json"
        folgen.append({"datum": datum, "basis": basis, "mp3": mp3, "meta_pfad": meta_pfad, "meta": meta,
                       "kapitel": [{**k, **m} for k, m in zip(kapitel, meta["kapitel"])],
                       "kapitel_json": kap_pfad, "groesse": mp3.stat().st_size, "dauer": dauer(mp3),
                       "transkript": lesen(tr_pfad)["segments"] if tr_pfad.exists() else None})
    return sorted(folgen, key=lambda f: f["datum"], reverse=True)


GRENZE = 25 * 1024 * 1024 - 64 * 1024  # statische Dateien dürfen höchstens 25 MiB groß sein


def als_asset(folge):
    """Legt die MP3 nach public/audio/. Ist sie zu groß, wird sie mit kleinerer Bitrate neu kodiert
    (Kapitel, Tags und Cover bleiben erhalten)."""
    ziel = PUBLIC / "audio" / f"{folge['datum']}.mp3"
    ziel.parent.mkdir(exist_ok=True)
    if not ziel.exists() or ziel.stat().st_mtime < folge["mp3"].stat().st_mtime:
        if folge["groesse"] <= GRENZE:
            shutil.copyfile(folge["mp3"], ziel)
        else:
            for rate in (128, 112, 96, 80, 64, 48):
                if rate * 1000 / 8 * folge["dauer"] > GRENZE:
                    continue
                print(f"› {folge['mp3'].name}: {folge['groesse'] / 2**20:.0f} MiB zu groß, kodiere mit {rate} kbit/s neu")
                subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(folge["mp3"]), "-map", "0", "-c:v", "copy",
                                "-map_metadata", "0", "-map_chapters", "0", "-id3v2_version", "3",
                                "-b:a", f"{rate}k", str(ziel)], check=True)
                if ziel.stat().st_size <= GRENZE:
                    break
            else:
                sys.exit(f"{folge['mp3'].name} passt auch mit 48 kbit/s nicht unter 25 MiB.")
    folge["groesse"] = ziel.stat().st_size


def hochladen(folge):
    meta = folge["meta"]
    if meta.get("hochgeladen") != folge["groesse"]:
        wrangler("r2", "object", "put", f"{BUCKET}/audio/{folge['datum']}.mp3", "--file", str(folge["mp3"]),
                 "--content-type", "audio/mpeg", "--remote")
        meta["hochgeladen"] = folge["groesse"]
        schreiben(folge["meta_pfad"], meta)


def veroeffentlicht_setzen(folge):
    meta = folge["meta"]
    if not meta.get("veroeffentlicht"):
        meta["veroeffentlicht"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        schreiben(folge["meta_pfad"], meta)


# ---------------------------------------------------------------- Seite

def kapitel_html(f):
    zeilen = []
    for k in f["kapitel"]:
        links = "".join(f'<li><a href="{html.escape(u)}" rel="noopener">{html.escape(t)}</a></li>' for t, u in k.get("links", []))
        zeilen.append(
            f'<li class="kapitel" data-start="{k["startTime"]:.2f}">'
            f'<button type="button" class="sprung" aria-label="Zu {zeitmarke(k["startTime"])} springen">'
            f'<span class="zeit">{zeitmarke(k["startTime"])}</span><span class="ktitel">{html.escape(k["title"])}</span></button>'
            + (f'<p>{html.escape(k["text"])}</p>' if k.get("text") else "")
            + (f'<details><summary>Quellen</summary><ul class="quellen">{links}</ul></details>' if links else "")
            + "</li>")
    return "\n".join(zeilen)


def transkript_html(f):
    """Absätze je Sprecherwechsel, jeder Satz anklickbar; Kapitelüberschriften an ihren Zeitmarken."""
    kapitel = list(f["kapitel"])
    teile, absatz, sprecher = [], [], None

    def absatz_schliessen():
        if absatz:
            klasse = "sommer" if "Sommer" in sprecher else "mondschein"
            start = absatz[0]["startTime"]
            teile.append(f'<p class="beitrag"><button type="button" class="satz-sprung" data-start="{start:.2f}">'
                         f'<span class="wer {klasse}">{html.escape(sprecher)}</span> <span class="zeit">{zeitmarke(start)}</span></button> '
                         + " ".join(f'<span class="satz" data-start="{s["startTime"]:.2f}">{html.escape(s["body"])}</span>' for s in absatz)
                         + "</p>")
            absatz.clear()

    for s in f["transkript"]:
        while kapitel and kapitel[0]["startTime"] <= s["startTime"] + 0.01:
            absatz_schliessen()
            teile.append(f'<h4>{html.escape(kapitel.pop(0)["title"])}</h4>')
        if s["speaker"] != sprecher:
            absatz_schliessen()
            sprecher = s["speaker"]
        absatz.append(s)
    absatz_schliessen()
    return "\n".join(teile)


def folge_html(f):
    m, d = f["meta"], f["datum"]
    kapitel = f'<ol class="kapitelliste">\n{kapitel_html(f)}\n  </ol>'
    if f["transkript"]:
        inhalt = f"""<div class="reiter" role="tablist">
    <button type="button" role="tab" id="r-kap-{d}" aria-controls="p-kap-{d}" aria-selected="true">Kapitel</button>
    <button type="button" role="tab" id="r-tr-{d}" aria-controls="p-tr-{d}" aria-selected="false" tabindex="-1">Transkript</button>
  </div>
  <div class="panel" role="tabpanel" id="p-kap-{d}" aria-labelledby="r-kap-{d}">
  {kapitel}
  </div>
  <div class="panel transkript" role="tabpanel" id="p-tr-{d}" aria-labelledby="r-tr-{d}" hidden>
  <p class="hinweis-tr">Aus dem Skript der Folge; ein Klick auf einen Satz springt dorthin.
  <a href="transkript/{d}.vtt" download>Als WebVTT</a></p>
{transkript_html(f)}
  </div>"""
    else:
        inhalt = f"<h3>Kapitel</h3>\n  {kapitel}"
    return f"""
<article class="folge" id="folge-{d}">
  <p class="dachzeile">Folge {m['nummer']} · {datum_de(d)} · {round(f['dauer'] / 60)} Min.</p>
  <h2>{html.escape(m['titel'])}</h2>
  <p class="kurz">{html.escape(m['kurz'])}</p>
  <audio controls preload="metadata" src="audio/{d}.mp3"></audio>
  <p class="download"><a href="audio/{d}.mp3" download>MP3 herunterladen</a> ({f['groesse'] / 1e6:.0f} MB)</p>
  {inhalt}
</article>"""


SEITE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITEL__</title>
<meta name="description" content="__BESCHREIBUNG__">
<meta property="og:type" content="website">
<meta property="og:title" content="__TITEL__">
<meta property="og:description" content="__BESCHREIBUNG__">
<meta property="og:image" content="__BASIS__/cover.jpg">
<meta property="og:url" content="__BASIS__/">
<meta name="twitter:card" content="summary_large_image">
<link rel="alternate" type="application/rss+xml" title="__TITEL__" href="__BASIS__/feed.xml">
<link rel="icon" href="cover-klein.jpg">
<link rel="apple-touch-icon" href="cover-klein.jpg">
<style>
:root {
  --bg: #f7f3ec; --flaeche: #ffffff; --text: #1b2233; --leise: #5b6475; --linie: #e3dccf;
  --sonne: #c86a12; --mond: #22407a; --akzent: #c86a12; --akzent-text: #ffffff;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0c1426; --flaeche: #131e36; --text: #e8ecf4; --leise: #9aa6bd; --linie: #26334f;
    --sonne: #f2a33a; --mond: #a9c1f0; --akzent: #f2a33a; --akzent-text: #1b1206;
  }
}
:root[data-theme="dark"] {
  --bg: #0c1426; --flaeche: #131e36; --text: #e8ecf4; --leise: #9aa6bd; --linie: #26334f;
  --sonne: #f2a33a; --mond: #a9c1f0; --akzent: #f2a33a; --akzent-text: #1b1206;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 17px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
.wrap { max-width: 760px; margin: 0 auto; padding: 24px 16px 64px; }
header.kopf { display: grid; grid-template-columns: 180px 1fr; gap: 24px; align-items: center; margin-bottom: 32px; }
header.kopf img { width: 100%; aspect-ratio: 1; border-radius: 14px; box-shadow: 0 8px 30px rgb(0 0 0 / .25); }
h1 { font-size: 1.9rem; line-height: 1.15; margin: 0 0 8px; }
h1 .pro { color: var(--sonne); } h1 .contra { color: var(--mond); }
.untertitel { margin: 0 0 16px; color: var(--leise); }
.abo { display: flex; flex-wrap: wrap; gap: 8px; }
.abo a, .abo button { font: inherit; font-size: .9rem; padding: 7px 12px; border-radius: 999px; cursor: pointer;
  border: 1px solid var(--linie); background: var(--flaeche); color: var(--text); text-decoration: none; }
.abo .haupt { background: var(--akzent); color: var(--akzent-text); border-color: var(--akzent); font-weight: 600; }
.folge { background: var(--flaeche); border: 1px solid var(--linie); border-radius: 16px; padding: 20px; margin-bottom: 24px; }
.dachzeile { margin: 0; color: var(--leise); font-size: .9rem; }
.folge h2 { font-size: 1.35rem; line-height: 1.25; margin: 4px 0 8px; }
.kurz { margin: 0 0 16px; }
audio { width: 100%; }
.download { font-size: .9rem; color: var(--leise); margin: 6px 0 0; }
a { color: var(--akzent); }
h3 { font-size: 1rem; margin: 20px 0 8px; }
.kapitelliste { list-style: none; margin: 0; padding: 0; }
.kapitel { border-top: 1px solid var(--linie); padding: 10px 0; }
.kapitel p { margin: 4px 0 0 64px; color: var(--leise); font-size: .95rem; }
.sprung { all: unset; cursor: pointer; display: flex; gap: 12px; align-items: baseline; width: 100%; }
.sprung:focus-visible { outline: 2px solid var(--akzent); outline-offset: 3px; border-radius: 4px; }
.zeit { font-variant-numeric: tabular-nums; color: var(--akzent); min-width: 52px; font-size: .9rem; }
.ktitel { font-weight: 600; }
.kapitel.aktiv .ktitel::after { content: " ◀"; color: var(--akzent); font-size: .8em; }
details { margin: 6px 0 0 64px; font-size: .9rem; }
summary { cursor: pointer; color: var(--leise); }
.quellen { padding-left: 18px; margin: 6px 0; }
.quellen li { margin: 3px 0; overflow-wrap: anywhere; }
footer { color: var(--leise); font-size: .85rem; margin-top: 40px; }
.hinweis { font-size: .85rem; color: var(--leise); min-height: 1.2em; margin: 8px 0 0; }
.reiter { display: flex; gap: 4px; margin: 20px 0 0; border-bottom: 1px solid var(--linie); }
.reiter button { font: inherit; font-weight: 600; background: none; border: 0; color: var(--leise); cursor: pointer;
  padding: 8px 14px; border-bottom: 3px solid transparent; margin-bottom: -1px; }
.reiter button[aria-selected="true"] { color: var(--text); border-bottom-color: var(--akzent); }
.reiter button:focus-visible { outline: 2px solid var(--akzent); outline-offset: -2px; }
.panel { padding-top: 4px; }
.transkript { max-height: 70vh; overflow-y: auto; overscroll-behavior: contain; padding-right: 6px; }
.transkript h4 { font-size: .95rem; margin: 22px 0 6px; color: var(--leise); text-transform: uppercase; letter-spacing: .04em; }
.hinweis-tr { font-size: .85rem; color: var(--leise); margin: 10px 0; }
.beitrag { margin: 0 0 12px; }
.satz-sprung { all: unset; cursor: pointer; margin-right: 4px; }
.satz-sprung:focus-visible { outline: 2px solid var(--akzent); outline-offset: 2px; border-radius: 4px; }
.wer { font-weight: 700; }
.wer.sommer { color: var(--sonne); } .wer.mondschein { color: var(--mond); }
.beitrag .zeit { min-width: 0; font-size: .8rem; margin-right: 2px; }
.satz { cursor: pointer; border-radius: 3px; transition: background .2s; }
.satz:hover { background: color-mix(in srgb, var(--akzent) 12%, transparent); }
.satz.aktiv { background: color-mix(in srgb, var(--akzent) 28%, transparent); }
@media (max-width: 560px) {
  header.kopf { grid-template-columns: 1fr; text-align: center; }
  header.kopf img { width: 70%; margin: 0 auto; }
  .abo { justify-content: center; }
  .kapitel p, details { margin-left: 0; }
  .transkript { max-height: 65vh; }
}
</style>
</head>
<body>
<div class="wrap">
<header class="kopf">
  <img src="cover-klein.jpg" alt="Cover: Herr Sommer auf der Sonnenseite, Herr Mondschein auf der Nachtseite">
  <div>
    <h1><span class="pro">Pro</span> und <span class="contra">Contra</span><br>KI- und IT-News</h1>
    <p class="untertitel">__UNTERTITEL__</p>
    <div class="abo">
__APPLE__
__SPOTIFY__
__YOUTUBE__
      <a href="podcastaddict://__HOST__/feed.xml">Podcast Addict</a>
      <a href="https://antennapod.org/deeplink/subscribe?url=__FEED_ENC__">AntennaPod</a>
      <a href="pktc://subscribe/__HOST__/feed.xml">Pocket Casts</a>
      <a href="overcast://x-callback-url/add?url=__FEED_ENC__">Overcast</a>
      <button type="button" id="kopieren">Feed-Adresse kopieren</button>
      <button type="button" id="teilen" hidden>Teilen</button>
    </div>
    <p class="hinweis" id="hinweis" aria-live="polite"></p>
  </div>
</header>
<main>
__FOLGEN__
</main>
<footer>
  <p>__BESCHREIBUNG__</p>
  <p>Feed: <a href="feed.xml">__BASIS__/feed.xml</a></p>
__RECHTLICHES__
</footer>
</div>
<script>
const FEED = "__BASIS__/feed.xml";
const hinweis = document.getElementById("hinweis");
document.getElementById("kopieren").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(FEED); hinweis.textContent = "Kopiert. In der Podcast-App „Per URL hinzufügen“ wählen und einfügen."; }
  catch (e) { hinweis.textContent = FEED; }
});
const teilen = document.getElementById("teilen");
if (navigator.share) {
  teilen.hidden = false;
  teilen.addEventListener("click", () => navigator.share({ title: document.title, url: location.href }).catch(() => {}));
}
// Zählung ohne Cookies: je Tag und Herkunft nur Summen (Seitenaufruf, Klick auf eine App, Start im Player), siehe src/zaehler.js
const q = new URLSearchParams(location.search);
const quelle = (q.get("utm_source") || "").toLowerCase();
let verweis = "";
try { verweis = new URL(document.referrer).hostname; } catch (e) {}
const HERKUNFT = quelle === "tiktok" || q.has("ttclid") || verweis.includes("tiktok") ? "tiktok"
  : /^(meta|facebook|instagram|fb|ig)$/.test(quelle) || q.has("fbclid") || /facebook|instagram/.test(verweis) ? "meta"
  : /^(google|youtube)$/.test(quelle) || q.has("gclid") || q.has("gbraid") || q.has("wbraid") || /google|youtube/.test(verweis) ? "google"
  : verweis && verweis !== location.hostname ? "sonst" : "direkt";
const zaehle = (ereignis) => { try { navigator.sendBeacon("/z", ereignis + "|" + HERKUNFT); } catch (e) {} };
const sichtbar = () => { if (document.visibilityState === "visible") { zaehle("seite"); document.removeEventListener("visibilitychange", sichtbar); } };
document.addEventListener("visibilitychange", sichtbar);  // vorab im Hintergrund geladene Seiten zählen erst, wenn sie jemand sieht
sichtbar();
document.querySelectorAll(".abo a").forEach(a => a.addEventListener("click", () =>
  zaehle(a.href.includes("podcasts.apple.com") ? "apple" : a.href.includes("spotify.com") ? "spotify" : a.href.includes("youtube.com") ? "youtube" : "app")));
document.getElementById("kopieren").addEventListener("click", () => zaehle("app"));

document.querySelectorAll(".folge").forEach(folge => {
  const audio = folge.querySelector("audio");
  audio.addEventListener("play", () => zaehle("play"), { once: true });
  folge.querySelector(".download a").addEventListener("click", () => zaehle("mp3"));
  const kapitel = [...folge.querySelectorAll(".kapitel")];
  kapitel.forEach(k => k.querySelector(".sprung").addEventListener("click", () => {
    document.querySelectorAll("audio").forEach(a => { if (a !== audio) a.pause(); });
    audio.currentTime = parseFloat(k.dataset.start);
    audio.play();
  }));
  const springe = (t) => {
    document.querySelectorAll("audio").forEach(a => { if (a !== audio) a.pause(); });
    audio.currentTime = t;
    audio.play();
  };
  folge.querySelectorAll(".satz, .satz-sprung").forEach(s =>
    s.addEventListener("click", () => springe(parseFloat(s.dataset.start))));

  // Reiter Kapitel | Transkript
  const reiter = [...folge.querySelectorAll('[role="tab"]')];
  const zeige = (r) => reiter.forEach(x => {
    const an = x === r;
    x.setAttribute("aria-selected", an);
    x.tabIndex = an ? 0 : -1;
    document.getElementById(x.getAttribute("aria-controls")).hidden = !an;
  });
  reiter.forEach((r, i) => {
    r.addEventListener("click", () => zeige(r));
    r.addEventListener("keydown", (ev) => {
      if (ev.key !== "ArrowRight" && ev.key !== "ArrowLeft") return;
      const n = reiter[(i + (ev.key === "ArrowRight" ? 1 : reiter.length - 1)) % reiter.length];
      zeige(n); n.focus();
    });
  });

  // Mitlesen: aktueller Satz wird markiert und bleibt im sichtbaren Bereich des Transkripts
  const saetze = [...folge.querySelectorAll(".satz")];
  const kasten = folge.querySelector(".transkript");
  let letzter = null;
  audio.addEventListener("timeupdate", () => {
    let aktiv = null;
    kapitel.forEach(k => { if (parseFloat(k.dataset.start) <= audio.currentTime + 0.3) aktiv = k; });
    kapitel.forEach(k => k.classList.toggle("aktiv", k === aktiv && !audio.paused));
    let satz = null;
    for (const s of saetze) { if (parseFloat(s.dataset.start) <= audio.currentTime + 0.1) satz = s; else break; }
    if (satz !== letzter) {
      if (letzter) letzter.classList.remove("aktiv");
      if (satz) {
        satz.classList.add("aktiv");
        if (kasten && !kasten.hidden && !audio.paused) {
          const k = kasten.getBoundingClientRect(), r = satz.getBoundingClientRect();
          if (r.top < k.top || r.bottom > k.bottom) kasten.scrollTop += r.top - k.top - k.height / 3;
        }
      }
      letzter = satz;
    }
  });
});
</script>
</body>
</html>
"""


def seite_bauen(konfig, folgen):
    from urllib.parse import quote
    basis = konfig["basis_url"]
    feed = f"{basis}/feed.xml"
    text = (SEITE
            .replace("__FOLGEN__", "\n".join(folge_html(f) for f in folgen) or "<p>Die erste Folge kommt bald.</p>")
            .replace("__TITEL__", html.escape(konfig["titel"]))
            .replace("__UNTERTITEL__", html.escape(konfig["untertitel"]))
            .replace("__BESCHREIBUNG__", html.escape(konfig["beschreibung"]))
            .replace("__APPLE__", f'      <a class="haupt" href="{html.escape(konfig["apple_url"])}">Auf Apple Podcasts anhören</a>'
                     if konfig.get("apple_url") else '      <a class="haupt" href="podcast://__HOST__/feed.xml">Apple Podcasts</a>')
            .replace("__SPOTIFY__", f'      <a class="haupt" href="{html.escape(konfig["spotify_url"])}">Auf Spotify anhören</a>'
                     if konfig.get("spotify_url") else "")
            .replace("__YOUTUBE__", f'      <a class="haupt" href="{html.escape(konfig["youtube_url"])}">Auf YouTube anhören</a>'
                     if konfig.get("youtube_url") else "")
            .replace("__HOST__", basis.split("://", 1)[1])
            .replace("__FEED_ENC__", quote(feed, safe=""))
            # Impressum und Datenschutz liegen zentral auf einer eigenen Seite für Podcast und DJ Anubis
            .replace("__RECHTLICHES__", f'  <p><a href="{html.escape(konfig["impressum_url"])}">Impressum</a> · '
                     f'<a href="{html.escape(konfig["datenschutz_url"])}">Datenschutz</a></p>'
                     if konfig.get("impressum_url") else "")
            .replace("__BASIS__", basis))
    (PUBLIC / "index.html").write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- Feed

def shownotes(f, basis):
    """Kapitelliste zuerst: YouTube kürzt die Beschreibung aus dem Feed auf 5.000 Zeichen und erkennt
    Kapitel nur an den Zeitmarken, die davor stehen. Text und Quellen folgen danach."""
    teile = [f"<p>{html.escape(f['meta']['kurz'])}</p>", "<p><strong>Kapitel</strong></p>", "<ul>"]
    teile += [f"<li>{zeitmarke(k['startTime'])} {html.escape(k['title'])}</li>" for k in f["kapitel"]]
    teile.append("</ul>")
    url = f"{basis}/#folge-{f['datum']}"
    teile.append(f'<p>Quellen und Transkript: <a href="{html.escape(url)}">{html.escape(url)}</a></p>')
    teile += ["<p><strong>Worum es geht</strong></p>", "<ul>"]
    for k in f["kapitel"]:
        teile.append(f"<li>{html.escape(k['title'])}"
                     + (f"<br>{html.escape(k['text'])}" if k.get("text") else "")
                     + "".join(f'<br><a href="{html.escape(u)}">{html.escape(t)}</a>' for t, u in k.get("links", []))
                     + "</li>")
    teile.append("</ul>")
    return "".join(teile)


def spotify_text(konfig, f):
    """Titel und Beschreibung für Spotify for Creators. Aus den Zeilen „(MM:SS) Titel“ macht Spotify Kapitel."""
    zeilen = [f["meta"]["titel"], "", f["meta"]["kurz"], "", "Kapitel"]
    zeilen += [f"({zeitmarke(k['startTime'])}) {k['title']}" for k in f["kapitel"]]
    zeilen += ["", f"Quellen und Transkript: {konfig['basis_url']}/#folge-{f['datum']}"]
    return "\n".join(zeilen)


def facebook_text(konfig, f):
    """Text für den Facebook-Beitrag zur Folge; dazu kommt als Bild das Vorschaubild 09 Folgen/<datum>/<datum>_youtube.jpg."""
    m = f["meta"]
    youtube = f"https://www.youtube.com/watch?v={m['youtube']}" if m.get("youtube") else konfig["youtube_url"]
    return "\n".join([f"Folge {m['nummer']}: {m['titel']}", "", m["kurz"], "",
                      f"Spotify: {konfig['spotify_url']}", f"Apple Podcasts: {konfig['apple_url']}",
                      f"YouTube: {youtube}", f"Quellen und Transkript: {konfig['basis_url']}/#folge-{f['datum']}",
                      "", "Beide Stimmen sind KI-generiert."])


def transkript_tags(basis, f):
    if not f["transkript"]:
        return ""
    return "".join(f'\n      <podcast:transcript url="{basis}/transkript/{f["datum"]}.{endung}" type="{typ}" language="de"{rel}/>'
                   for endung, typ, rel in (("vtt", "text/vtt", ' rel="captions"'), ("json", "application/json", "")))


def besitzer(konfig):
    """Spotify & Apple schicken beim Einreichen einen Bestätigungscode an diese Adresse; sie steht dann
    öffentlich im Feed. Deshalb nur, wenn in podcast.json „besitzer_email“ gesetzt ist."""
    if not konfig.get("besitzer_email"):
        return ""
    return (f"\n    <itunes:owner><itunes:name>{escape(konfig.get('besitzer_name', konfig['autor']))}</itunes:name>"
            f"<itunes:email>{escape(konfig['besitzer_email'])}</itunes:email></itunes:owner>")


def feed_bauen(konfig, folgen):
    basis = konfig["basis_url"]
    jetzt = format_datetime(datetime.now(timezone.utc))
    items = []
    for f in folgen:
        m = f["meta"]
        pub = format_datetime(datetime.fromisoformat(m.get("veroeffentlicht") or datetime.now(timezone.utc).isoformat()))
        notes = shownotes(f, basis).replace("]]>", "]]]]><![CDATA[>")
        items.append(f"""    <item>
      <title>{escape(m['titel'])}</title>
      <description><![CDATA[{notes}]]></description>
      <content:encoded><![CDATA[{notes}]]></content:encoded>
      <itunes:summary>{escape(m['kurz'])}</itunes:summary>
      <link>{basis}/#folge-{f['datum']}</link>
      <guid isPermaLink="false">pro-und-contra-{f['datum']}</guid>
      <pubDate>{pub}</pubDate>
      <enclosure url="{basis}/audio/{f['datum']}.mp3" length="{f['groesse']}" type="audio/mpeg"/>
      <itunes:duration>{int(round(f['dauer']))}</itunes:duration>
      <itunes:episode>{m['nummer']}</itunes:episode>
      <itunes:episodeType>full</itunes:episodeType>
      <itunes:explicit>false</itunes:explicit>
      <itunes:image href="{basis}/cover.jpg"/>
      <podcast:chapters url="{basis}/kapitel/{f['datum']}.json" type="application/json+chapters"/>{transkript_tags(basis, f)}
    </item>""")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:podcast="https://podcastindex.org/namespace/1.0">
  <channel>
    <title>{escape(konfig['titel'])}</title>
    <link>{basis}/</link>
    <atom:link href="{basis}/feed.xml" rel="self" type="application/rss+xml"/>
    <description>{escape(konfig['beschreibung'])}</description>
    <language>de</language>
    <lastBuildDate>{jetzt}</lastBuildDate>
    <image><url>{basis}/cover.jpg</url><title>{escape(konfig['titel'])}</title><link>{basis}/</link></image>
    <itunes:image href="{basis}/cover.jpg"/>
    <itunes:author>{escape(konfig['autor'])}</itunes:author>{besitzer(konfig)}
    <itunes:summary>{escape(konfig['beschreibung'])}</itunes:summary>
    <itunes:type>episodic</itunes:type>
    <itunes:explicit>false</itunes:explicit>
    <itunes:category text="Technology"/>
    <itunes:category text="News"><itunes:category text="Tech News"/></itunes:category>
{chr(10).join(items)}
  </channel>
</rss>
"""
    (PUBLIC / "feed.xml").write_text(xml, encoding="utf-8")


# ---------------------------------------------------------------- Ablauf

def bilder_bauen():
    cover = ROOT / "cover" / "cover.jpg"
    for name, groesse in (("cover.jpg", 1400), ("cover-klein.jpg", 400)):
        ziel = PUBLIC / name
        if ziel.exists() and ziel.stat().st_mtime >= cover.stat().st_mtime:
            continue
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(cover), "-vf", f"scale={groesse}:{groesse}:flags=lanczos",
                        "-q:v", "3", str(ziel)], check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nur-bauen", action="store_true", help="nur public/ erzeugen, nichts hochladen oder deployen")
    ap.add_argument("--ohne-upload", action="store_true", help="MP3s nicht hochladen, nur Seite/Feed deployen")
    ap.add_argument("--spotify", metavar="DATUM", help="Titel und Beschreibung für Spotify for Creators ausgeben und ablegen")
    ap.add_argument("--facebook", metavar="DATUM", help="Text für den Facebook-Beitrag zur Folge ausgeben und ablegen")
    args = ap.parse_args()

    konfig = lesen(KONFIG)
    PUBLIC.mkdir(exist_ok=True)
    (PUBLIC / "kapitel").mkdir(exist_ok=True)
    folgen = folgen_sammeln()

    for datum, bauen, art in ((args.spotify, spotify_text, "spotify"), (args.facebook, facebook_text, "facebook")):
        if not datum:
            continue
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Umlaute auch in der Windows-Konsole
        f = next((f for f in folgen if f["datum"] == datum), None) or sys.exit(f"Keine Folge {datum}.")
        text = bauen(konfig, f)
        ziel = ROOT / "09 Folgen" / datum / f"{datum}_{art}.txt"
        ziel.parent.mkdir(parents=True, exist_ok=True)
        ziel.write_text(text + "\n", encoding="utf-8")
        print(text + f"\n\nAbgelegt: {ziel}")
        return

    r2 = konfig.get("audio", "assets") == "r2"
    if r2 and not args.nur_bauen and not konfig.get("bucket_angelegt"):
        r = wrangler("r2", "bucket", "create", BUCKET, ausgabe=True)
        if r.returncode != 0 and "already exists" not in (r.stdout + r.stderr):
            sys.exit("Bucket konnte nicht angelegt werden.")
        konfig["bucket_angelegt"] = True
        schreiben(KONFIG, konfig)

    for f in folgen:
        if not r2:
            als_asset(f)
        elif not args.nur_bauen and not args.ohne_upload:
            hochladen(f)
        if not args.nur_bauen:
            veroeffentlicht_setzen(f)

    if not konfig.get("basis_url"):
        if args.nur_bauen:
            sys.exit("basis_url fehlt in podcast.json – erst einmal ohne --nur-bauen laufen lassen.")
        # Pages-Projekt anlegen, um die pages.dev-Adresse zu erfahren (ist der Name vergeben, hängt Cloudflare etwas an)
        r = wrangler("pages", "project", "create", PROJEKT, "--production-branch", "main", ausgabe=True)
        if r.returncode != 0 and "already exists" not in (r.stdout + r.stderr):
            sys.exit("Pages-Projekt konnte nicht angelegt werden.")
        r = wrangler("pages", "project", "list", ausgabe=True)
        treffer = re.search(r"\b" + PROJEKT + r"(?:-[a-z0-9]+)?\.pages\.dev\b", r.stdout)
        if not treffer:
            sys.exit("Keine pages.dev-Adresse für das Projekt gefunden.")
        konfig["basis_url"] = "https://" + treffer.group(0)
        schreiben(KONFIG, konfig)

    groessen = {str(d.relative_to(PUBLIC).as_posix()): d.stat().st_size for d in (PUBLIC / "audio").glob("*.mp3")}         if (PUBLIC / "audio").exists() else {}
    schreiben(WEB / "src" / "audio_groessen.json", groessen)
    bilder_bauen()
    for f in folgen:
        shutil.copyfile(f["kapitel_json"], PUBLIC / "kapitel" / f"{f['datum']}.json")
        if f["transkript"]:
            (PUBLIC / "transkript").mkdir(exist_ok=True)
            for endung in ("json", "vtt"):
                shutil.copyfile(f["basis"].parent / f"{f['basis'].name}_transkript.{endung}", PUBLIC / "transkript" / f"{f['datum']}.{endung}")
    seite_bauen(konfig, folgen)
    feed_bauen(konfig, folgen)
    print(f"public/ gebaut: {len(folgen)} Folge(n).")

    if not args.nur_bauen:
        wrangler("pages", "deploy", "--project-name", PROJEKT, "--branch", "main", "--commit-dirty=true")
        print(f"\nSeite: {konfig['basis_url']}/\nFeed:  {konfig['basis_url']}/feed.xml")


if __name__ == "__main__":
    main()
