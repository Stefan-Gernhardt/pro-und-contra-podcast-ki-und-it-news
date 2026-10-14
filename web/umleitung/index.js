// Die Seite lag zuerst unter pro-und-contra.peterschmidtshopping.workers.dev und ist nach Pages umgezogen.
// Dieser Worker leitet alte Links und alte Feed-Abos dauerhaft (301) an die neue Adresse weiter.
const ZIEL = "https://pro-und-contra-ki-und-it-news.pages.dev";

export default {
  fetch(request) {
    const url = new URL(request.url);
    return Response.redirect(ZIEL + url.pathname + url.search, 301);
  },
};
