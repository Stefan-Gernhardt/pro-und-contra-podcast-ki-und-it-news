"""Google Trends für das Ranking (Schritt 2 der Sonntags-Routine).

  python routine/trends.py "Robco" "Homeoffice" "Google Playground" ...
      Suchinteresse in Deutschland in den letzten 7 Tagen, je Begriff Schnitt und Spitze,
      umgerechnet auf den Ankerbegriff „Windows 11“ = 100. Google normiert jede Abfrage auf ihren
      eigenen Höchstwert; erst der gemeinsame Anker macht Begriffe aus verschiedenen Abfragen vergleichbar.
      0 heißt: zu wenig Suchen für Google Trends, nicht „unwichtig“.

  python routine/trends.py --tagestrends
      Die aktuellen Tagestrends für Deutschland (RSS), mit ungefährer Zahl der Suchen.

Ohne Fremdpakete. Bei Fehlern (Netz gesperrt, Google antwortet mit 429) Meldung und Exit-Code 1;
die Routine vermerkt das im Bericht und rankt ohne Trends weiter.
"""
import http.cookiejar
import json
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request

ANKER = "Windows 11"
GEO = "DE"
ZEIT = "now 7-d"
JE_ABFRAGE = 4                                  # Google erlaubt 5 Begriffe pro Abfrage, einer ist der Anker

_cj = http.cookiejar.CookieJar()
_op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cj))
_op.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"), ("Accept-Language", "de-DE,de")]


def _hole(url: str) -> str:
    for versuch in range(4):
        try:
            return _op.open(url, timeout=30).read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code != 429 or versuch == 3:
                raise
            time.sleep(10 * (versuch + 1))
    raise RuntimeError("unerreichbar")


def _json(url: str):
    return json.loads(_hole(url).split("\n", 1)[1])     # Google stellt ")]}'" voran


def vergleiche(begriffe: list[str]) -> dict[str, tuple[float, float]]:
    """{Begriff: (Schnitt, Spitze)} relativ zu ANKER = 100 (Schnitt des Ankers)."""
    _hole("https://trends.google.com/trends/?geo=" + GEO)  # Cookie holen
    ergebnis = {}
    for i in range(0, len(begriffe), JE_ABFRAGE):
        teil = begriffe[i:i + JE_ABFRAGE]
        alle = [ANKER] + teil
        req = {"comparisonItem": [{"keyword": b, "geo": GEO, "time": ZEIT} for b in alle], "category": 0, "property": ""}
        ex = _json("https://trends.google.com/trends/api/explore?hl=de&tz=-120&req=" + urllib.parse.quote(json.dumps(req)))
        w = next(w for w in ex["widgets"] if w["id"] == "TIMESERIES")
        daten = _json("https://trends.google.com/trends/api/widgetdata/multiline?hl=de&tz=-120&req="
                      + urllib.parse.quote(json.dumps(w["request"])) + "&token=" + w["token"])
        punkte = daten["default"]["timelineData"]
        anker = statistics.mean(p["value"][0] for p in punkte) or 1
        for j, b in enumerate(teil, start=1):
            werte = [p["value"][j] for p in punkte]
            ergebnis[b] = (100 * statistics.mean(werte) / anker, 100 * max(werte) / anker)
        time.sleep(2)
    return ergebnis


def tagestrends() -> list[tuple[str, str]]:
    xml = _hole(f"https://trends.google.com/trending/rss?geo={GEO}")
    titel = re.findall(r"<item>\s*<title>([^<]*)</title>", xml)
    menge = re.findall(r"<ht:approx_traffic>([^<]*)</ht:approx_traffic>", xml)
    return list(zip(titel, menge))


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(2)
    try:
        if args == ["--tagestrends"]:
            for t, m in tagestrends():
                print(f"{m:>8}  {t}")
            return
        print(f"Google Trends, {GEO}, letzte 7 Tage, {ANKER} = 100")
        for b, (schnitt, spitze) in vergleiche(args).items():
            print(f"{schnitt:7.1f} Schnitt  {spitze:7.1f} Spitze  {b}")
    except Exception as e:  # noqa: BLE001 – Routine soll ohne Trends weiterlaufen
        print(f"Google Trends nicht erreichbar: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
