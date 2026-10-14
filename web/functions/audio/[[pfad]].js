// Pages-Function für /audio/*: Vorspulen (Range-Anfragen) für die MP3s. Die Logik steht in src/audio.js.
import { audio } from "../../src/audio.js";

export const onRequest = ({ request, env }) => audio(request, env);
