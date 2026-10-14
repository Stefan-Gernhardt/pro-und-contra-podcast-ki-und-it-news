-- Tabelle des Zählers in der D1-Datenbank pro-und-contra-zaehler (gefüllt von src/zaehler.js).
-- Je Tag, Herkunft und Ereignis steht nur eine Summe.
--   npx wrangler d1 execute pro-und-contra-zaehler --remote --file zaehler.sql
CREATE TABLE IF NOT EXISTS zaehler (
  tag      TEXT NOT NULL,              -- Datum in deutscher Zeit, z. B. 2026-10-04
  herkunft TEXT NOT NULL,              -- tiktok | meta | google | sonst | direkt
  ereignis TEXT NOT NULL,              -- seite | apple | spotify | youtube | app | play | mp3
  n        INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (tag, herkunft, ereignis)
);
