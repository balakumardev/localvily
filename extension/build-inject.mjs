// Generates classic-script injection bundles from the ES-module source of truth.
// engines/bing.js  -> inject/serp.js   (globalThis.__serp = {serpUrl, detectBlock, parse, name})
// extract.js       -> inject/extract.js (globalThis.__extract = extractContent ; uses window.Readability)
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";

const stripExports = (src) =>
  src.replace(/^export\s+const\s+/gm, "const ")
     .replace(/^export\s+function\s+/gm, "function ");

mkdirSync(new URL("./inject/", import.meta.url), { recursive: true });

const bing = stripExports(readFileSync(new URL("./engines/bing.js", import.meta.url), "utf8"));
writeFileSync(
  new URL("./inject/serp.js", import.meta.url),
  `${bing}\nglobalThis.__serp = { name, serpUrl, detectBlock, parse };\n`,
);

const extract = stripExports(readFileSync(new URL("./extract.js", import.meta.url), "utf8"));
writeFileSync(
  new URL("./inject/extract.js", import.meta.url),
  `${extract}\nglobalThis.__extract = (doc) => extractContent(doc, window.Readability);\n`,
);

console.log("wrote extension/inject/serp.js and extension/inject/extract.js");
