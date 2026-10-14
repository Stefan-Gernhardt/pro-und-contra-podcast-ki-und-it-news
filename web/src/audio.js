// Liefert /audio/<datei> aus R2 oder aus den statischen Dateien aus, mit Range-Unterstützung (Vorspulen im Browser und in Podcast-Apps).
// Aufgerufen von der Pages-Function functions/audio/[[pfad]].js; alles andere sind statische Dateien aus public/.

import GROESSEN from "./audio_groessen.json";

const TYPEN = { mp3: "audio/mpeg", jpg: "image/jpeg", json: "application/json" };

export async function audio(request, env) {
  const url = new URL(request.url);
  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response("Methode nicht erlaubt", { status: 405, headers: { allow: "GET, HEAD" } });
  }

  if (!env.AUDIO) return ausAssets(request, env); // ohne R2: MP3 liegt als statische Datei in public/audio/

  const key = decodeURIComponent(url.pathname.slice(1)); // „audio/2026-09-27.mp3“
  const kopf = (obj) => {
    const h = new Headers();
    obj.writeHttpMetadata(h);
    if (!h.has("content-type")) h.set("content-type", TYPEN[key.split(".").pop()] ?? "application/octet-stream");
    h.set("etag", obj.httpEtag);
    h.set("accept-ranges", "bytes");
    h.set("cache-control", "public, max-age=86400");
    h.set("access-control-allow-origin", "*");
    return h;
  };

  if (request.method === "HEAD") {
    const obj = await env.AUDIO.head(key);
    if (!obj) return new Response(null, { status: 404 });
    const h = kopf(obj);
    h.set("content-length", String(obj.size));
    return new Response(null, { headers: h });
  }

  let obj;
  try {
    obj = await env.AUDIO.get(key, { range: request.headers, onlyIf: request.headers });
  } catch {
    const info = await env.AUDIO.head(key);
    return new Response("Bereich ungültig", { status: 416, headers: info ? { "content-range": `bytes */${info.size}` } : {} });
  }
  if (!obj) return new Response("Nicht gefunden", { status: 404 });

  const h = kopf(obj);
  if (!("body" in obj)) return new Response(null, { status: 304, headers: h }); // If-None-Match traf zu

  if (!request.headers.has("range") || !obj.range) {
    h.set("content-length", String(obj.size));
    return new Response(obj.body, { headers: h });
  }
  const r = obj.range;
  let start, ende;
  if ("suffix" in r) {
    start = Math.max(0, obj.size - r.suffix);
    ende = obj.size - 1;
  } else {
    start = r.offset ?? 0;
    ende = r.length !== undefined ? start + r.length - 1 : obj.size - 1;
  }
  h.set("content-range", `bytes ${start}-${ende}/${obj.size}`);
  h.set("content-length", String(ende - start + 1));
  return new Response(obj.body, { status: 206, headers: h });
}

// Statische Dateien beantworten keine Range-Anfragen; Safari und Apple Podcasts spielen ohne 206 aber nichts ab.
// Deshalb holt die Function die Datei und reicht nur den angefragten Ausschnitt durch.
async function ausAssets(request, env) {
  const voll = await env.ASSETS.fetch(new Request(request.url, { method: request.method }));
  const h = new Headers(voll.headers);
  h.set("accept-ranges", "bytes");
  h.set("cache-control", "public, max-age=86400");
  h.set("access-control-allow-origin", "*");

  // Die interne Asset-Antwort hat keine Content-Length; die Größen schreibt veroeffentlichen.py mit.
  const gesamt = GROESSEN[new URL(request.url).pathname.slice(1)] ?? Number(voll.headers.get("content-length"));
  const m = /^bytes=(\d*)-(\d*)$/.exec((request.headers.get("range") ?? "").trim());
  if (voll.ok && gesamt) h.set("content-length", String(gesamt));
  if (!voll.ok || request.method === "HEAD" || !gesamt || !m || (m[1] === "" && m[2] === "")) {
    return new Response(voll.body, { status: voll.status, headers: h });
  }
  let start, ende;
  if (m[1] === "") {
    start = Math.max(0, gesamt - Number(m[2]));
    ende = gesamt - 1;
  } else {
    start = Number(m[1]);
    ende = m[2] === "" ? gesamt - 1 : Math.min(Number(m[2]), gesamt - 1);
  }
  if (start > ende) {
    await voll.body?.cancel();
    return new Response(null, { status: 416, headers: { "content-range": `bytes */${gesamt}` } });
  }
  const laenge = ende - start + 1;
  let pos = 0;
  const schnitt = new TransformStream({
    transform(stueck, ctrl) {
      const a = pos;
      pos += stueck.byteLength;
      if (pos <= start || a > ende) return;
      ctrl.enqueue(stueck.subarray(Math.max(0, start - a), Math.min(stueck.byteLength, ende + 1 - a)));
      if (pos > ende) ctrl.terminate();
    },
  });
  const { readable, writable } = new FixedLengthStream(laenge);
  voll.body.pipeThrough(schnitt).pipeTo(writable).catch(() => {});
  h.set("content-range", `bytes ${start}-${ende}/${gesamt}`);
  h.set("content-length", String(laenge));
  return new Response(readable, { status: 206, headers: h });
}
