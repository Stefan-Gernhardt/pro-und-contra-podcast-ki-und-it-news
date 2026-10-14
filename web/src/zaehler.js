// Zählt Seitenaufrufe und Klicks der Podcast-Seite je Tag und Herkunft in D1 (Tabelle zaehler, siehe zaehler.sql).
// Gespeichert werden nur Summen: kein Cookie, keine IP-Adresse, keine Kennung des Besuchers.
// Aufgerufen von der Pages-Function functions/z.js; die Seite meldet per navigator.sendBeacon("/z", "<ereignis>|<herkunft>").

const EREIGNISSE = new Set(["seite", "apple", "spotify", "youtube", "app", "play", "mp3"]);
const HERKUENFTE = new Set(["tiktok", "meta", "google", "sonst", "direkt"]);
const BOTS = /bot|crawl|spider|slurp|facebookexternalhit|headless|lighthouse|preview/i;

export async function zaehlen(request, env) {
  const leer = new Response(null, { status: 204 });
  if (!env.ZAEHLER || BOTS.test(request.headers.get("user-agent") ?? "")) return leer;

  // nur Meldungen von der eigenen Seite
  const absender = request.headers.get("origin");
  if (absender && absender !== new URL(request.url).origin) return leer;

  const [ereignis, herkunft] = (await request.text()).slice(0, 40).split("|");
  if (!EREIGNISSE.has(ereignis) || !HERKUENFTE.has(herkunft)) return leer;

  const tag = new Intl.DateTimeFormat("sv-SE", { timeZone: "Europe/Berlin" }).format(new Date()); // „2026-10-04“
  await env.ZAEHLER.prepare(
    "INSERT INTO zaehler (tag, herkunft, ereignis, n) VALUES (?1, ?2, ?3, 1) " +
      "ON CONFLICT (tag, herkunft, ereignis) DO UPDATE SET n = n + 1",
  ).bind(tag, herkunft, ereignis).run();
  return leer;
}
