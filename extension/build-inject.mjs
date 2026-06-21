// Generates classic-script injection bundles from the ES-module source of truth.
// engines/bing.js  -> inject/serp.js   (globalThis.__serp = {serpUrl, detectBlock, parse, name})
// extract.js       -> inject/extract.js (globalThis.__extract = extractContent ; uses window.Readability)
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";

const BANNER =
  "// GENERATED FILE — DO NOT EDIT.\n" +
  "// Built from the ES-module source by extension/build-inject.mjs (`npm run build:inject`).\n" +
  "// Edit the source module instead, then regenerate.\n\n";

const stripExports = (src) =>
  src
    .replace(/^export\s+default\s+/gm, "")
    .replace(/^export\s+async\s+function\s+/gm, "async function ")
    .replace(/^export\s+function\s+/gm, "function ")
    .replace(/^export\s+(const|let|var)\s+/gm, "$1 ")
    .replace(/^export\s+class\s+/gm, "class ")
    .replace(/^export\s*\{[^}]*\}\s*;?\s*$/gm, ""); // drop `export { ... }` aggregations

function toClassic(src, sourceName) {
  const out = stripExports(src);
  // Fail loud: a surviving `export` would silently break classic-script injection.
  if (/(^|\n)\s*export\b/.test(out)) {
    throw new Error(
      `build-inject: unhandled export form in ${sourceName} — extend stripExports() before shipping`,
    );
  }
  return out;
}

mkdirSync(new URL("./inject/", import.meta.url), { recursive: true });

const bing = toClassic(readFileSync(new URL("./engines/bing.js", import.meta.url), "utf8"), "engines/bing.js");
writeFileSync(
  new URL("./inject/serp.js", import.meta.url),
  `${BANNER}${bing}\nglobalThis.__serp = { name, serpUrl, detectBlock, parse };\n`,
);

const extract = toClassic(readFileSync(new URL("./extract.js", import.meta.url), "utf8"), "extract.js");
writeFileSync(
  new URL("./inject/extract.js", import.meta.url),
  `${BANNER}${extract}\nglobalThis.__extract = (doc) => extractContent(doc, window.Readability);\n`,
);

console.log("wrote extension/inject/serp.js and extension/inject/extract.js");
