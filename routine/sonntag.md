# Sonntags-Routine: neue Folge „Pro und Contra – KI- und IT-News“

Diese Datei ist die Anweisung für die wöchentliche Cloud-Routine (Sonntag 03:00 UTC, also 05:00 Uhr Berlin im
Sommer, 04:00 Uhr im Winter). Die Routine selbst
bekommt nur den Satz „Lies `routine/sonntag.md` und führe sie aus“. Was hier steht, gilt ab dem nächsten Lauf.

**Veröffentlichen auf pages.dev: ja**

(Veröffentlicht wird nicht aus dieser Sitzung, sondern vom GitHub-Workflow `.github/workflows/folge-release.yml`, sobald
du die Folge in Schritt 8 pushst; die Sitzung selbst darf weder Releases anlegen noch auf Cloudflare Pages hochladen.
Steht oben „nein“, legst du in Schritt 8 zusätzlich die Datei `09 Folgen/$DATUM/nicht-veroeffentlichen` an: Dann
entsteht nur das Release, und der Nutzer gibt die Folge selbst frei.)

Du arbeitest allein und ohne Rückfragen; niemand schaut zu. Halte dich an die Reihenfolge. Wenn ein Schritt scheitert
und du ihn nicht sicher beheben kannst, brich ab, schreib den Bericht (Schritt 10) und pushe ihn.

## Was du nie tust

- API-Schlüssel ausgeben, in Dateien schreiben oder committen. Die Schlüssel hängt der Proxy der Umgebung an; in der
  Sitzung gibt es nur Platzhalter.
- Mehr als eine ganze Folge bei ElevenLabs rendern (Credits). Proben einzelner Blöcke mit `--block` sind unnötig.
- `--versuche` über 1 setzen.
- Etwas bei Spotify, YouTube, Apple, Meta, TikTok oder Facebook ändern. Spotify lädt der Nutzer selbst hoch.
- Die Folge 1 (2026-09-27) oder Folge 2 (2026-10-04) verändern, außer den Feldern, die `veroeffentlichen.py` selbst setzt.
- Selbst etwas auf pages.dev deployen (`wrangler pages deploy` oder `veroeffentlichen.py` ohne `--nur-bauen`). Das
  scheitert in der Sitzung ohnehin am Proxy; deployt wird nur über den Workflow.

## 0. Vorbereitung

```bash
export DATUM=$(TZ=Europe/Berlin date +%F)        # Datum der Folge = heutiger Tag in Berlin (die Routine läuft So 03:00 UTC)
export ELEVENLABS_API_KEY=platzhalter              # echter Schlüssel kommt vom Proxy
python3 -m pip install --use-pep517 -r tts/requirements-cloud.txt > /tmp/pip.log 2>&1 &   # läuft im Hintergrund weiter
```

- Immer `python3 -m pip`, nie `pip` (gehört zu einem anderen Python). `--use-pep517` ist nötig, sonst scheitert docopt.
- **Abgebrochene Folge fortsetzen** (Wunsch des Nutzers): Ein früherer Lauf kann abgebrochen sein, z. B. am
  Nutzungslimit. Suche auf `main` das jüngste Datum `D` mit `01 Collect Topics/D.md`, für das es weder
  `web/folgen/D.json` noch den Tag `folge-D` gibt. Ist der letzte Commit mit Dateien dieser Folge höchstens 2 Tage alt
  (`git log -1 --format=%ct -- "01 Collect Topics/D.md" "02 Ranked Topics/D.md" "03 Most Relevant Topics/D.md" "04 Recherche/D.md" "05 Script/D.md" "06 Chatterbox Script/D.jsonl"`
  mit der aktuellen Zeit vergleichen), dann `export DATUM=D` und dort weitermachen: Schritte, deren Ergebnis schon auf
  `main` liegt, überspringst du und liest das Ergebnis stattdessen; du beginnst beim ersten fehlenden (`01`, `02`,
  `03`, `04 Recherche`, `05 Script` mit `06`, dann Rendern). Ist der Abbruch älter als 2 Tage, ignorierst du ihn und
  beginnst mit dem heutigen Datum von vorn. Im Bericht vermerken, ob und ab welchem Schritt fortgesetzt wurde.
- **Doppelte Folge verhindern:** Gibt es auf `main` schon `web/folgen/$DATUM.json`, liefert
  `git ls-remote --tags origin "folge-$DATUM"` etwas oder gibt es den Zweig `folge/$DATUM` schon (dann ist der Workflow
  gescheitert und wird im Actions-Tab neu gestartet), dann brich sofort ab (Bericht schreiben, sonst nichts).
- **Zwischenstände sichern:** Nach jedem der Schritte 1 bis 4 das Ergebnis auf `main` committen und pushen
  (`git pull --rebase` vorher; Nachricht „Folge $DATUM: … (Schritt N)“), damit ein späterer Lauf dort weitermachen kann.
- Lies `Charaktere.md`, `proUndContraKIundITnews.txt` (die sieben Schritte des Nutzers) und die Skripte der
  bisherigen Folgen in `05 Script/`.
- Befehle im Vordergrund brechen nach 10 Minuten ab. Alles, was länger dauert, mit `nohup … &` starten und per PID
  überwachen (siehe Schritt 6).

## 1. Meldungen sammeln → `01 Collect Topics/$DATUM.md`

Alle Meldungen der letzten 7 Tage (heute eingeschlossen) aus den Feeds
https://www.heise.de/rss/heise-top-atom.xml und https://www.heise.de/thema/Kuenstliche-Intelligenz.xml
(per `curl` holen und mit Python auswerten). Werbung („heise-Angebot“, Rabatte, Webinare) entfernen, Doppelte
zusammenführen. Der KI-Feed enthält nur rund 100 Einträge; reicht er nicht 7 Tage zurück, das im Bericht vermerken.
Format wie `01 Collect Topics/2026-10-04.md`.

## 2. Ranking → `02 Ranked Topics/$DATUM.md`

Für jede Meldung die Resonanz in Medien und sozialen Medien schätzen. Regeln des Nutzers:
- Spielethemen etwas höher, Jubiläen (z. B. „10 Jahre TikTok“) etwas höher, Themen mit langfristiger Bedeutung höher.
- Was in den bisherigen Folgen schon dran war (`03 Most Relevant Topics/*.md`), niedriger; lieber neue Themen.
- Meldungen ohne IT- oder KI-Kern (z. B. Nobelpreise für Physik/Medizin) überspringen.
- Bewertungen von Meldungen, die schon in der Vorwoche in `02 Ranked Topics/` standen, übernehmen.

Arbeite mit **4 Agenten parallel** (je ein Viertel der Meldungen). Die WebSearch kennt die letzten Tage kaum. Gib den
Agenten deshalb diese Quellen mit:
- Bing News als RSS: `https://www.bing.com/news/search?q=<Suchbegriff>&format=rss&setlang=de` (und `&setlang=en`)
- Hacker News: `https://hn.algolia.com/api/v1/search?query=<Begriff>&tags=story&numericFilters=created_at_i%3E<Unix-Zeit vor 8 Tagen>`
  (Punkte und Kommentare)
- die heise-Artikelseite selbst (Zahl der Forenbeiträge)
- zeit.de und faz.net sind für WebFetch gesperrt; nicht versuchen.

**Google Trends** (Suchinteresse in Deutschland) zusätzlich, und zwar von dir selbst, nicht von den Agenten (parallele
Abfragen sperrt Google mit 429):
- Wähle für jede Meldung, die nach Medien und Social mindestens 5 Punkte hat, **einen** kurzen Suchbegriff, nach dem
  Leute tatsächlich suchen würden (Firma, Produkt oder Schlagwort, z. B. „Robco“, „Homeoffice“, „Mistral“, nicht die
  Überschrift). Alle Begriffe in **einem** Aufruf: `python routine/trends.py "Begriff 1" "Begriff 2" …`
  (Werte relativ zu „Windows 11“ = 100, letzte 7 Tage).
- Dazu einmal `python routine/trends.py --tagestrends`; steht dort ein Begriff zu einer Meldung, im Hinweis vermerken.
- Eigene Spalte **Trends** in der Rangliste (Schnitt/Spitze und Begriff). Der Wert fließt in **Social** ein:
  Schnitt ab 20 oder Spitze ab 100 zählt wie „Trending“ (Social mindestens 4), Schnitt ab 5 mindestens 3. Er senkt
  Social nie, denn 0 heißt nur: zu wenig Suchen für Google Trends. Bei Gleichstand nach Medien und Social entscheidet
  der Trends-Schnitt.
- Scheitert das Skript (Netz gesperrt, 429), ohne Trends weitermachen und es im Bericht vermerken.

Format wie `02 Ranked Topics/2026-10-04.md`.

**Zeit sparen:** Die Recherche-Agenten für die Wunschthemen (Schritt 3) und das Intro kannst du schon während des
Rankings starten.

## 3. Auswahl → `03 Most Relevant Topics/$DATUM.md`

- Zuerst die Themenwünsche aus `02b Wished Topics/$DATUM.md` (falls vorhanden; alle Wünsche nehmen).
- Mit den bestplatzierten Meldungen aus dem Ranking auf **genau 5 Themen** auffüllen.
- Dazu eine Idee für den **Cold Open (Intro)**: eine Meldung der Woche mit Pointe, Witz oder Twist, wie die
  Türklingel-Kamera, die den Postboten als Familienmitglied erkennt (Folge 1), oder der Podcast, der sich umbenennen
  muss (Folge 2). Keine Pointe wiederholen.
- Reihenfolge mit einem roten Faden begründen. Format wie `03 Most Relevant Topics/2026-10-04.md`.

## 4. Tiefe Recherche → `04 Recherche/$DATUM.md`

Je Thema und für das Intro **ein Agent, alle parallel** (6 Agenten). Gesucht sind: Fakten zur Meldung,
Hintergründe, ähnliche Meldungen, historische Parallelen („Das gab es schon einmal …“), Zahlen, Gegenstimmen.
Jede Aussage mit Quelle (Titel und URL). Gleiche Quellen-Hinweise wie in Schritt 2. Format wie
`04 Recherche/2026-10-04.md`, am Ende eine Quellenliste.

## 5. Skript → `05 Script/$DATUM.md`, dann Sprechskript

- Schreibe das Skript aus `04 Recherche/$DATUM.md` und `Charaktere.md`. **Aufbau und Formatierung exakt wie
  `05 Script/2026-10-04.md`** (Kopf, Ablauf-Tabelle, Blöcke `## N · Titel`, Sprecherzeilen, Regieanweisungen in
  eckigen Klammern, Jingles, Quellenteil am Ende), denn `tts/md_zu_chatterbox.py` liest genau dieses Format.
- Das Skript beginnt immer mit dem Cold Open mit Pointe, dann Begrüßung und Themenüberblick. Am Ende Sonnenseite,
  Nachtgedanke und Verabschiedung wie in den bisherigen Folgen.
- Herr Sommer ist immer Pro, Herr Mondschein immer Contra. Beide sollen witzig sein. Keine Sprüche, Begrüßungen oder
  Running Gags aus den bisherigen Folgen wiederholen; im Kopf unter „Sprüche und Running Gags“ festhalten, was neu ist.
- **Länge: höchstens 4.300 gesprochene Wörter** (ergibt bei 126 Wörtern pro Minute inklusive Jingles rund 34 Minuten).
  Zielfolge 30–35 Minuten, bis 40 Minuten ist erlaubt.
- Dann: `python3 tts/md_zu_chatterbox.py "05 Script/$DATUM.md"` → `06 Chatterbox Script/$DATUM.jsonl` und `.md`.
  Warnungen des Konverters beheben.
- Kosten prüfen: `python3 tts/render_elevenlabs.py --kosten`. **Grenze 40.000 Zeichen**; liegt die Zahl über
  31.000, ist das Skript meist zu lang → kürzen und neu umwandeln.
- Eigennamen oder Abkürzungen, die ElevenLabs vermutlich falsch spricht, darfst du in `tts/aussprache_mehrsprachig.json`
  ergänzen (gleiches Format wie die vorhandenen Einträge). **Abkürzungen, die man deutsch buchstabiert (BMW, SAP,
  BSI …), nicht eintragen:** ElevenLabs spricht sie im Original richtig, Schreibweisen wie „Be-Emm-We“ liest es
  englisch („Bi-Em-Wi“). Einträge nur für Abkürzungen, die anders klingen sollen (IT → „Ei-Ti“, KI → „K-I“).
  „USA“ immer mit Artikel schreiben („die USA“, „in den USA“), die Regel macht daraus „Vereinigte Staaten“.
- **Zwischenstand sichern:** Ordner 01–06 auf `main` committen und pushen (`git pull --rebase` vorher). So geht die
  Arbeit nicht verloren, falls das Rendern scheitert.

## 6. Rendern mit ElevenLabs

```bash
nohup python3 tts/render_elevenlabs.py > /tmp/render.log 2>&1 &
echo $! > /tmp/render.pid
```

Überwachen nur per PID, **nicht mit `pgrep -f`** (trifft die eigene Überwachungsschleife):

```bash
for i in $(seq 9); do kill -0 $(cat /tmp/render.pid) 2>/dev/null || break; sleep 60; done; tail -5 /tmp/render.log
```

(läuft höchstens 9 Minuten; so oft wiederholen, bis `kill -0` scheitert, dann das ganze Log lesen). Das Rendern dauert etwa
30–45 Minuten. Bricht es mittendrin ab, Ursache beheben und **einmal** neu starten: fertige Abschnitte liegen im
Cache und kosten nichts mehr. Notiere aus dem Log die Credits vorher/nachher und alle Abschnitte mit einer
Whisper-Fehlerquote ab 0,04.

Ergebnis: `09 Folgen/$DATUM/elevenlabs/$DATUM_elevenlabs.mp3` mit `_kapitel.json/.txt` und `_transkript.json/.vtt`
(legt das Skript selbst dort ab), außerdem dieselben Dateien in `tts/out/`.

## 7. Webdaten der Folge

1. Die fünf Dateien in `tts/out` kopieren: `${DATUM}_elevenlabs{.mp3,_kapitel.json,_kapitel.txt,_transkript.json,_transkript.vtt}`
   → `${DATUM}{.mp3,_kapitel.json,…}` (ohne `_elevenlabs`).
2. **Die alten Folgen von der Live-Seite holen**, denn `web/veroeffentlichen.py` braucht alle Folgen in `tts/out`:

   ```bash
   B=https://pro-und-contra-ki-und-it-news.pages.dev
   for j in web/folgen/*.json; do d=$(basename $j .json); [ "$d" = "$DATUM" ] && continue
     curl -sfo tts/out/$d.mp3 $B/audio/$d.mp3
     curl -sfo tts/out/${d}_kapitel.json $B/kapitel/$d.json
     curl -sfo tts/out/${d}_transkript.json $B/transkript/$d.json
     curl -sfo tts/out/${d}_transkript.vtt $B/transkript/$d.vtt
   done
   mkdir -p web/public/werbung
   for v in spot_ki_stimmen spot_luegendetektor spot_zwei_meinungen; do curl -sfo web/public/werbung/$v.mp4 $B/werbung/$v.mp4; done
   ```

   Die Werbevideos liegen nur auf der Live-Seite und würden bei einem Deploy sonst gelöscht. Fehlt danach die MP3
   einer Folge aus `web/folgen/`, bricht `veroeffentlichen.py` mit „ist veröffentlicht, aber ihre MP3 fehlt“ ab:
   dann den Download dieser Folge wiederholen, nie die Folge weglassen (sie verschwände von Seite und Feed).
3. `web/folgen/$DATUM.json` anlegen, Aufbau wie `web/folgen/2026-10-04.json`: `nummer` (bisherige Folgen + 1),
   `titel` (die Themen der Folge, kurz), `kurz` (2–3 Sätze), `kapitel` mit **so vielen Einträgen wie Kapitel in
   `${DATUM}_kapitel.json`**, je Kapitel `text` (1–3 Sätze) und `links` (3–6 Paare `[Titel, URL]` aus
   `04 Recherche/$DATUM.md`). Felder `veroeffentlicht` und `hochgeladen` nicht selbst setzen.
4. `python3 web/veroeffentlichen.py --nur-bauen` muss ohne Fehler durchlaufen; `web/public/feed.xml` muss alle
   Folgen enthalten, die neue zuerst.
5. Spotify-Text: `python3 web/veroeffentlichen.py --spotify $DATUM` → `09 Folgen/$DATUM/${DATUM}_spotify.txt`.
6. Vorschaubild für YouTube und Facebook: `python3 "08 Werbung/vorschaubild.py" $DATUM` →
   `09 Folgen/$DATUM/${DATUM}_youtube.jpg` (1280x720; Hauptthema ist das erste Thema aus `titel`, ist es zu lang,
   in der json ein kürzeres Feld `vorschau` setzen). Das Bild ansehen: nichts abgeschnitten, Text lesbar.
7. Facebook-Text: `python3 web/veroeffentlichen.py --facebook $DATUM` → `09 Folgen/$DATUM/${DATUM}_facebook.txt`
   (das Video gibt es noch nicht, darum steht dort die Playlist).

## 8. Ablage

1. Auf `main` committen und pushen: `03`–`06` (falls seit Schritt 5 geändert), `web/folgen/$DATUM.json`,
   `tts/aussprache_mehrsprachig.json` (falls geändert). Nicht: MP3s, `tts/out`, `web/public`.
2. Die Folge pushen; der Workflow `.github/workflows/folge-release.yml` legt daraus das Release an, baut Seite und
   Feed, deployt sie und schreibt `web/folgen/$DATUM.json` (mit `veroeffentlicht`) zurück nach `main`. Punkt 1 muss
   vorher gepusht sein, denn der Workflow liest `web/folgen/$DATUM.json` aus dem Zweig.

   ```bash
   echo "Folge <nummer>: <titel>" > "09 Folgen/$DATUM/titel.txt"
   # nur wenn oben „nein“ steht:  touch "09 Folgen/$DATUM/nicht-veroeffentlichen"
   git checkout -b folge/$DATUM
   git add -f "09 Folgen/$DATUM"
   git commit -m "Folge $DATUM für das Release"
   git push origin folge/$DATUM
   git checkout main
   ```

## 9. Workflow abwarten und prüfen

Der Workflow braucht etwa 3–5 Minuten. Lesen über die GitHub-API geht über den Proxy (`gh` ist nicht angemeldet).
Prüfen, alle 60 s, höchstens 20 Minuten lang:
- Release `folge-$DATUM` existiert, Assets: MP3, Kapitel, Transkript, Spotify-Text, Vorschaubild, Facebook-Text.
- Lauf des Workflows „Folge als Release ablegen und veröffentlichen“ für `folge/$DATUM`: Ergebnis `success`.
  Bei `failure` die Schritt-Logs lesen (Actions-API) und den Fehler wörtlich in den Bericht schreiben; nicht selbst
  deployen. Der Zweig bleibt dann stehen, damit man den Lauf im Actions-Tab neu starten kann.
- Nur bei „ja“: `https://pro-und-contra-ki-und-it-news.pages.dev/feed.xml` enthält die neue Folge,
  `/audio/$DATUM.mp3` liefert 200, die alten Folgen und `/werbung/spot_zwei_meinungen.mp4` ebenfalls; auf `main` liegt
  der Commit „Folge $DATUM veröffentlicht“ (vor dem Bericht `git pull --rebase`).

Apple Podcasts, YouTube, fyyd, podcast.de und Podcast Index holen die Folge selbst aus dem Feed.

## 10. Bericht → `routine/berichte/$DATUM.md` (auf `main`)

Kurz und auf Deutsch:
- Titel, Nummer, Länge, Kapitel mit Zeitmarken, die fünf Themen und die Intro-Pointe
- Wortzahl, Zeichen laut `--kosten`, Credits vorher/nachher
- Abschnitte mit Whisper-Fehlerquote ab 0,04 (zum Nachhören), mit Block und Satzanfang
- Link zum Release, ob veröffentlicht wurde (und wenn nicht: dass der Nutzer freigeben muss)
- Dauer der Schritte, Probleme und was du geändert hast
- Context-Grenze in der Sitzung: `echo "${CLAUDE_CODE_AUTO_COMPACT_WINDOW:-nicht gesetzt} ${CLAUDE_AUTOCOMPACT_PCT_OVERRIDE:-nicht gesetzt}"`
  (soll laut `.claude/settings.json` `250000 100` sein: Zusammenfassen spätestens bei 250.000 Tokens)
- **Zu tun für den Nutzer:** Spotify-Upload (MP3 aus dem Release, Text aus `${DATUM}_spotify.txt`); bei YouTube das
  neue Video prüfen (Kommentare aus, KI-Kennzeichnung „Veränderte oder synthetische Inhalte: Ja“, Untertitel aus
  `transkript/$DATUM.vtt` hochladen, Kapitel prüfen, Vorschaubild `${DATUM}_youtube.jpg` hochladen, Video-ID als
  `youtube` in `web/folgen/$DATUM.json` eintragen); auf Facebook ein Beitrag mit `${DATUM}_youtube.jpg` und dem Text
  aus `${DATUM}_facebook.txt` (YouTube-Link aufs Video ändern, KI-Label setzen, Kommentare nur für erwähnte Profile);
  die ausführliche Anleitung dazu steht in `10 Post-Steps/post-steps.md`

Zum Schluss den Bericht pushen. Deine letzte Nachricht im Lauf ist eine Zusammenfassung dieses Berichts in drei bis
fünf Zeilen.
