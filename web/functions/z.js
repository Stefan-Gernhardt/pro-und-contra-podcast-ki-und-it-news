// Pages-Function für /z: zählt Seitenaufrufe und Klicks je Tag und Herkunft. Die Logik steht in src/zaehler.js.
import { zaehlen } from "../src/zaehler.js";

export const onRequestPost = ({ request, env }) => zaehlen(request, env);
