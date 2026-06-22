# Injection strategy

MV3 `chrome.scripting.executeScript({ files: [...] })` runs **classic** scripts in the
page's isolated world — it cannot load ES modules, and a `func:` closure cannot
`import()` our modules. But our tested source of truth — `engines/bing.js` and
`extract.js` — are ES modules (they use `export`). The service worker itself
(`background.js`) is `type: module`, so it can freely `import` them to build the SERP
URL; only the **in-page injected** code must be a classic script.

## Resolution: generate classic bundles from the module source

`extension/build-inject.mjs` is a tiny build step (`npm run build:inject`) that reads
each module, strips the `export ` keywords, and writes a classic-script bundle that
attaches the logic to `globalThis`:

| Source module        | Generated artifact          | Global it defines                                  |
| -------------------- | --------------------------- | -------------------------------------------------- |
| `engines/bing.js`    | `inject/serp.js`            | `globalThis.__serp = { name, serpUrl, detectBlock, parse }` |
| `extract.js`         | `inject/extract.js`         | `globalThis.__extract = (doc) => extractContent(doc, window.Readability)` |

`lib/Readability.js` is already a classic script that sets the global `Readability`, so
it is injected directly via `files` (no transform needed) ahead of `inject/extract.js`.

## Why a build step (not dual-mode files, not duplicated copies)

- A file containing `export` is parsed as a module; injecting it as a classic script
  throws `SyntaxError: Unexpected token 'export'`. So the module files can't be injected
  as-is.
- Hand-maintaining parallel non-module copies duplicates logic and drifts (DRY hazard).
- The build step keeps `engines/bing.js` + `extract.js` as the single tested source; the
  `inject/` files are **generated artifacts** regenerated whenever the source changes.

## Worker injection flow

- **search:** import `engines/index.js` in the worker → `engine.serpUrl(query, k)` → open
  background tab (`active:false`) → wait for `complete` + 800ms settle → inject
  `inject/serp.js` → call a `func` that runs `__serp.detectBlock(document)` /
  `__serp.parse(document, k)`. Block → `{error:"blocked: bing challenge"}`, else
  `{results:[...]}`. Tab removed in `finally`.
- **fetch:** open background tab → wait + settle → inject `lib/Readability.js` then
  `inject/extract.js` → call a `func` that runs `__extract(document)`. Empty text →
  `{error:"no extractable content"}`, else the `{title,text,excerpt,length}` object.
  Tab removed in `finally`.

## Regenerating

```bash
npm run build:inject
```

Do **not** edit `extension/inject/*.js` by hand — edit the module source and rebuild.
