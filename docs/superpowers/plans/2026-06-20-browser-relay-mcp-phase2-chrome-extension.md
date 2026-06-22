# browser-relay-mcp — Plan 2: Chrome Extension (Relay Driver) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Chrome MV3 extension that drives the user's logged-in browser as the `relay` driver — polling the Phase 1 relay's `/pending`, running Bing searches and Readability fetches in background tabs, and posting structured results to `/result/{job_id}`.

**Architecture:** A Manifest V3 service worker (`background.js`) polls the relay via `chrome.alarms` (survives MV3 sleep) + `setTimeout` (fast when awake), dispatches each job to a background tab, injects shared pure-JS modules (`engines/bing.js` parser, `extract.js` Readability wrapper) via `chrome.scripting.executeScript`, and posts results back. The DOM-facing logic lives in pure functions that are unit-tested under jsdom with saved HTML fixtures, independent of any browser. A popup + options page configure the relay URL and show live status.

**Tech Stack:** Chrome MV3 (service worker, `chrome.tabs`/`scripting`/`alarms`/`storage`), vanilla JS (ES modules for the pure logic), Mozilla Readability 0.6.0 (vendored), Node 22 + `node:test` + jsdom for unit tests.

## Global Constraints

- **Relay URL default:** `http://localhost:15552` (matches Phase 1 port). Configurable via options, stored in `chrome.storage.local` key `serverUrl`.
- **Manifest:** MV3. `permissions: ["tabs","scripting","alarms","storage"]`. `host_permissions: ["<all_urls>","http://localhost/*","http://127.0.0.1/*"]`. **No** broad `content_scripts` — inject on demand. `background.service_worker: "background.js"` with `"type":"module"`.
- **Poll protocol (from Phase 1, exact):** `GET /pending` → `{jobs:[{job_id, kind, ...payload}], close_tabs:[...]}`. `kind ∈ {"search","fetch"}`. Search payload has `{query, k, engine}`; fetch payload has `{url, include_html}`. Post results to `POST /result/{job_id}` with arbitrary JSON.
- **Result payloads (exact shapes the relay's `_shape_*` expects):**
  - search success: `{results:[{title, url, snippet}]}`; search failure: `{error:"<string>"}`.
  - fetch success: `{title, text, excerpt, length}` (+ `html` when `include_html`); fetch failure: `{error:"<string>"}`.
- **Block/CAPTCHA detection:** a detected challenge returns `{error:"blocked: <engine> challenge"}` — NEVER an empty result. (Interactive escalation is Phase 3; Phase 2 only reports the block as an error.)
- **Concurrency:** the relay already caps dispatch (≤FETCH_CAP fetches, ≤1 search w/ spacing). The extension processes every job returned by a `/pending` batch, each in its own background tab, and must not exceed what it was handed. Fetch tabs are created per-job and removed after extraction.
- **No focus stealing:** all tabs created with `active:false`. Never navigate a tab the extension didn't create.
- **Poll cadence:** `chrome.alarms` at 0.5 min (MV3 floor) + `setTimeout` at 1500ms when worker alive. A single in-flight poll guard (`pollInFlight`).
- **Pure-logic / chrome-API split:** `engines/*.js` and `extract.js` are pure functions (input: a DOM `Document` or HTML string; output: plain objects) with ZERO `chrome.*` references, so jsdom can test them. `background.js` holds all `chrome.*` orchestration and is validated by live acceptance tests.

---

### Task 1: Extension scaffold — manifest, package.json, vendored Readability, test harness

**Files:**
- Create: `extension/manifest.json`
- Create: `extension/lib/Readability.js` (vendored, downloaded)
- Create: `package.json` (repo root — Node test tooling for the extension)
- Create: `extension/tests/smoke.test.mjs`
- Create: `extension/icons/icon16.png`, `icon48.png`, `icon128.png` (placeholder 1x1 PNGs)

**Interfaces:**
- Consumes: nothing.
- Produces: a loadable (if inert) MV3 manifest; `npm test` runs `node --test` over `extension/tests/`; jsdom available as a dev dependency.

- [ ] **Step 1: Create the Node test harness config**

`package.json` (repo root):
```json
{
  "name": "browser-relay-mcp-extension",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {
    "test": "node --test extension/tests/"
  },
  "devDependencies": {
    "jsdom": "^24.0.0"
  }
}
```

- [ ] **Step 2: Vendor Readability 0.6.0**

Run:
```bash
cd /Users/bkumara/personal/localvily
mkdir -p extension/lib extension/icons extension/tests extension/engines
curl -sL "https://unpkg.com/@mozilla/readability@0.6.0/Readability.js" -o extension/lib/Readability.js
head -3 extension/lib/Readability.js
```
Expected: Apache license header for Readability. Verify the file is >50KB (`wc -c extension/lib/Readability.js`, expect ~70KB).

- [ ] **Step 3: Create placeholder icons**

Run:
```bash
cd /Users/bkumara/personal/localvily
# 1x1 transparent PNG, base64-decoded, reused at all sizes (placeholders; real icons in Phase 5)
B64="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
for s in 16 48 128; do echo "$B64" | base64 -d > "extension/icons/icon${s}.png"; done
ls -l extension/icons/
```
Expected: three `.png` files, each ~70 bytes.

- [ ] **Step 4: Write the manifest**

`extension/manifest.json`:
```json
{
  "manifest_version": 3,
  "name": "Browser Relay (search & fetch)",
  "version": "0.1.0",
  "description": "Drives your logged-in browser as an unlimited web search & fetch backend for a local MCP server",
  "permissions": ["tabs", "scripting", "alarms", "storage"],
  "host_permissions": ["<all_urls>", "http://localhost/*", "http://127.0.0.1/*"],
  "icons": { "16": "icons/icon16.png", "48": "icons/icon48.png", "128": "icons/icon128.png" },
  "action": { "default_popup": "popup.html", "default_title": "Browser Relay" },
  "background": { "service_worker": "background.js", "type": "module" },
  "options_ui": { "page": "options.html", "open_in_tab": false }
}
```

- [ ] **Step 5: Write a smoke test (proves the harness + jsdom work)**

`extension/tests/smoke.test.mjs`:
```javascript
import { test } from "node:test";
import assert from "node:assert/strict";
import { JSDOM } from "jsdom";
import { readFileSync } from "node:fs";

test("jsdom parses HTML into a queryable document", () => {
  const dom = new JSDOM(`<html><body><h1 id="t">hi</h1></body></html>`);
  assert.equal(dom.window.document.querySelector("#t").textContent, "hi");
});

test("manifest is valid JSON with MV3 + module worker", () => {
  const m = JSON.parse(readFileSync(new URL("../manifest.json", import.meta.url)));
  assert.equal(m.manifest_version, 3);
  assert.equal(m.background.type, "module");
  assert.deepEqual(m.permissions, ["tabs", "scripting", "alarms", "storage"]);
});
```

- [ ] **Step 6: Install deps and run the smoke test**

Run:
```bash
cd /Users/bkumara/personal/localvily && npm install && npm test
```
Expected: jsdom installs; both smoke tests pass.

- [ ] **Step 7: Create .gitignore entry for node_modules**

Append to the repo-root `.gitignore` (create if missing):
```
node_modules/
```

- [ ] **Step 8: Commit**

```bash
git add package.json package-lock.json extension/manifest.json extension/lib/Readability.js extension/icons/ extension/tests/smoke.test.mjs .gitignore
git commit -m "feat(ext): scaffold MV3 extension, vendor Readability 0.6.0, node:test+jsdom harness"
```

---

### Task 2: Bing SERP parser (`engines/bing.js`) — pure functions + fixtures

**Files:**
- Create: `extension/engines/bing.js`
- Create: `extension/tests/fixtures/bing-serp.html` (saved real Bing results page)
- Create: `extension/tests/fixtures/bing-blocked.html` (saved Bing challenge/blocked page, or a minimal hand-authored one)
- Create: `extension/tests/bing.test.mjs`

**Interfaces:**
- Consumes: a DOM `Document` (jsdom in tests, the live SERP in the extension).
- Produces (ES module exports):
  - `export const name = "bing";`
  - `export function serpUrl(query, k = 10) -> string` — the Bing search URL.
  - `export function detectBlock(doc) -> boolean` — true if the page is a CAPTCHA/challenge/block.
  - `export function parse(doc, k = 10) -> [{title, url, snippet}]` — top-k organic results.

- [ ] **Step 1: Capture a real Bing SERP fixture**

Run (saves a real logged-out Bing results page for "consistent hashing"):
```bash
cd /Users/bkumara/personal/localvily
curl -sL -A "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36" \
  "https://www.bing.com/search?q=consistent+hashing" -o extension/tests/fixtures/bing-serp.html
wc -c extension/tests/fixtures/bing-serp.html
grep -c "b_algo" extension/tests/fixtures/bing-serp.html || echo "NO b_algo — inspect the file"
```
Expected: a multi-KB HTML file. If `b_algo` count is 0 (Bing served a different layout or a challenge), open the file and identify the actual result-container class. **Adjust the selectors in Step 3 to match what the fixture actually contains** — the fixture is the source of truth, not the class names assumed here. Document the selectors you settled on in a comment at the top of `bing.js`.

- [ ] **Step 2: Write the failing parser test**

`extension/tests/bing.test.mjs`:
```javascript
import { test } from "node:test";
import assert from "node:assert/strict";
import { JSDOM } from "jsdom";
import { readFileSync } from "node:fs";
import { name, serpUrl, detectBlock, parse } from "../engines/bing.js";

function docFromFixture(file) {
  const html = readFileSync(new URL(`./fixtures/${file}`, import.meta.url), "utf8");
  return new JSDOM(html).window.document;
}

test("name is bing", () => assert.equal(name, "bing"));

test("serpUrl builds a bing search URL with the encoded query", () => {
  const u = serpUrl("consistent hashing", 10);
  assert.match(u, /^https:\/\/www\.bing\.com\/search\?/);
  assert.match(u, /q=consistent(\+|%20)hashing/);
});

test("parse extracts at least 8 results with title/url/snippet from a real SERP", () => {
  const doc = docFromFixture("bing-serp.html");
  const results = parse(doc, 10);
  assert.ok(results.length >= 8, `expected >=8 results, got ${results.length}`);
  for (const r of results) {
    assert.ok(r.title && r.title.length > 0, "title present");
    assert.match(r.url, /^https?:\/\//, "url is absolute http(s)");
    assert.equal(typeof r.snippet, "string");
  }
});

test("parse respects k", () => {
  const doc = docFromFixture("bing-serp.html");
  assert.ok(parse(doc, 3).length <= 3);
});

test("detectBlock is false for a normal SERP", () => {
  assert.equal(detectBlock(docFromFixture("bing-serp.html")), false);
});

test("detectBlock is true for a challenge page", () => {
  assert.equal(detectBlock(docFromFixture("bing-blocked.html")), true);
});
```

- [ ] **Step 3: Create the blocked-page fixture**

If Step 1 didn't produce a real challenge page, hand-author a minimal one that matches the block signals you'll detect. `extension/tests/fixtures/bing-blocked.html`:
```html
<!doctype html><html><head><title>Bing</title></head>
<body>
  <h1>To continue, please verify you are not a robot</h1>
  <div id="b_captchaContainer">
    <iframe src="https://challenges.cloudflare.com/turnstile"></iframe>
  </div>
</body></html>
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `cd /Users/bkumara/personal/localvily && node --test extension/tests/bing.test.mjs`
Expected: FAIL — `Cannot find module '../engines/bing.js'` (or export-not-found).

- [ ] **Step 5: Implement the parser**

`extension/engines/bing.js` (adjust the SELECTORS object to match the real fixture from Step 1):
```javascript
// Bing SERP parser. Selectors verified against extension/tests/fixtures/bing-serp.html.
// Organic results live in `li.b_algo`; title+link in `h2 > a`; snippet in `.b_caption p`
// (with `.b_lineclamp*` variants on newer layouts). If Bing changes layout, update the
// fixture and these selectors together.
export const name = "bing";

const RESULT_SELECTOR = "li.b_algo";
const TITLE_LINK_SELECTOR = "h2 a";
const SNIPPET_SELECTORS = [".b_caption p", "p.b_lineclamp2", "p.b_lineclamp3", ".b_caption"];

export function serpUrl(query, k = 10) {
  const count = Math.max(1, Math.min(k, 50));
  return `https://www.bing.com/search?q=${encodeURIComponent(query)}&count=${count}`;
}

export function detectBlock(doc) {
  if (doc.querySelector("#b_captchaContainer")) return true;
  if (doc.querySelector('iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]')) return true;
  const text = (doc.body?.textContent || "").toLowerCase();
  if (/verify you are (not |a )?(human|robot)/.test(text)) return true;
  if (/unusual traffic|automated queries/.test(text)) return true;
  return false;
}

export function parse(doc, k = 10) {
  const out = [];
  for (const node of doc.querySelectorAll(RESULT_SELECTOR)) {
    const link = node.querySelector(TITLE_LINK_SELECTOR);
    if (!link) continue;
    const url = link.getAttribute("href");
    const title = (link.textContent || "").trim();
    if (!url || !/^https?:\/\//.test(url) || !title) continue;

    let snippet = "";
    for (const sel of SNIPPET_SELECTORS) {
      const el = node.querySelector(sel);
      if (el && el.textContent.trim()) { snippet = el.textContent.trim(); break; }
    }
    out.push({ title, url, snippet });
    if (out.length >= k) break;
  }
  return out;
}
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `cd /Users/bkumara/personal/localvily && node --test extension/tests/bing.test.mjs`
Expected: PASS (6 tests). If `parse` returns <8, inspect the fixture and fix selectors (the implementation must match the real DOM, not vice versa).

- [ ] **Step 7: Commit**

```bash
git add extension/engines/bing.js extension/tests/bing.test.mjs extension/tests/fixtures/bing-serp.html extension/tests/fixtures/bing-blocked.html
git commit -m "feat(ext): Bing SERP parser with block detection + fixture tests"
```

---

### Task 3: Engine registry (`engines/index.js`)

**Files:**
- Create: `extension/engines/index.js`
- Create: `extension/tests/registry.test.mjs`

**Interfaces:**
- Consumes: `engines/bing.js`.
- Produces: `export function getEngine(name) -> {name, serpUrl, detectBlock, parse}` (defaults to bing for unknown names? NO — throws for unknown, so a typo fails loud); `export const DEFAULT_ENGINE = "bing"`.

- [ ] **Step 1: Write the failing test**

`extension/tests/registry.test.mjs`:
```javascript
import { test } from "node:test";
import assert from "node:assert/strict";
import { getEngine, DEFAULT_ENGINE } from "../engines/index.js";

test("default engine is bing", () => assert.equal(DEFAULT_ENGINE, "bing"));

test("getEngine returns the bing module", () => {
  const e = getEngine("bing");
  assert.equal(e.name, "bing");
  assert.equal(typeof e.serpUrl, "function");
  assert.equal(typeof e.parse, "function");
  assert.equal(typeof e.detectBlock, "function");
});

test("getEngine throws (fails loud) for an unknown engine", () => {
  assert.throws(() => getEngine("nope"), /unknown engine/i);
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /Users/bkumara/personal/localvily && node --test extension/tests/registry.test.mjs`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the registry**

`extension/engines/index.js`:
```javascript
import * as bing from "./bing.js";

const ENGINES = { bing };

export const DEFAULT_ENGINE = "bing";

export function getEngine(name) {
  const engine = ENGINES[name || DEFAULT_ENGINE];
  if (!engine) {
    throw new Error(`unknown engine: ${name}`);
  }
  return engine;
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd /Users/bkumara/personal/localvily && node --test extension/tests/registry.test.mjs`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add extension/engines/index.js extension/tests/registry.test.mjs
git commit -m "feat(ext): engine registry with fail-loud unknown-engine guard"
```

---

### Task 4: Content extractor (`extract.js`) — Readability + innerText fallback

**Files:**
- Create: `extension/extract.js`
- Create: `extension/tests/fixtures/article.html` (a real JS-light article with nav/footer chrome)
- Create: `extension/tests/extract.test.mjs`

**Interfaces:**
- Consumes: a DOM `Document` + the `Readability` global/class.
- Produces: `export function extractContent(doc, ReadabilityCtor) -> {title, text, excerpt, length}`. Uses Readability on a clone; falls back to `body.innerText` (nav/script/style stripped) when Readability returns null; returns `{error:"no extractable content"}`-shaped object only via empty text → caller decides. Actually returns `{title, text, excerpt, length}` always; `text` may be "" if truly empty.

- [ ] **Step 1: Capture an article fixture**

Run:
```bash
cd /Users/bkumara/personal/localvily
curl -sL -A "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36" \
  "https://en.wikipedia.org/wiki/Consistent_hashing" -o extension/tests/fixtures/article.html
wc -c extension/tests/fixtures/article.html
```
Expected: a large HTML file (Wikipedia article, >100KB) with real nav/footer chrome to strip.

- [ ] **Step 2: Write the failing test**

`extension/tests/extract.test.mjs`:
```javascript
import { test } from "node:test";
import assert from "node:assert/strict";
import { JSDOM } from "jsdom";
import { readFileSync } from "node:fs";
import { Readability } from "@mozilla/readability";
import { extractContent } from "../extract.js";

function docFromFixture(file, url = "https://en.wikipedia.org/wiki/Consistent_hashing") {
  const html = readFileSync(new URL(`./fixtures/${file}`, import.meta.url), "utf8");
  return new JSDOM(html, { url }).window.document;
}

test("extracts clean article text >= 1000 chars", () => {
  const out = extractContent(docFromFixture("article.html"), Readability);
  assert.ok(out.title && out.title.length > 0, "title present");
  assert.ok(out.length >= 1000, `expected >=1000 chars, got ${out.length}`);
  assert.equal(out.text.length, out.length);
});

test("extracted text excludes obvious nav chrome", () => {
  const out = extractContent(docFromFixture("article.html"), Readability);
  // Wikipedia's left-nav contains 'Main page'/'Random article'; body prose should dominate.
  assert.match(out.text, /hash/i, "article body present");
});

test("falls back to innerText when Readability yields nothing", () => {
  const dom = new JSDOM(`<html><body><nav>menu</nav><main>Hello world body content here.</main><script>x=1</script></body></html>`);
  const out = extractContent(dom.window.document, Readability);
  assert.match(out.text, /Hello world body content/);
  assert.ok(!/x=1/.test(out.text), "script content stripped");
});
```

> Note: the test imports Readability from the npm package `@mozilla/readability` for jsdom (it needs the module form). Add it as a dev dependency in Step 3. The extension itself uses the vendored `extension/lib/Readability.js` (global `Readability`), injected before `extract.js`. `extractContent` takes the constructor as a parameter so both worlds work.

- [ ] **Step 3: Add the dev dependency**

Run:
```bash
cd /Users/bkumara/personal/localvily && npm install --save-dev @mozilla/readability@0.6.0
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `cd /Users/bkumara/personal/localvily && node --test extension/tests/extract.test.mjs`
Expected: FAIL — `Cannot find module '../extract.js'`.

- [ ] **Step 5: Implement the extractor**

`extension/extract.js`:
```javascript
// Pure content extraction: Readability on a document clone, with an innerText fallback.
// `ReadabilityCtor` is passed in so this works both under jsdom (npm @mozilla/readability)
// and in the extension (vendored global `Readability` injected before this module).

function innerTextFallback(doc) {
  const clone = doc.body ? doc.body.cloneNode(true) : null;
  if (!clone) return "";
  for (const el of clone.querySelectorAll("script, style, noscript, nav, header, footer, aside, svg")) {
    el.remove();
  }
  // jsdom has no layout, so innerText is undefined there; fall back to textContent.
  const text = clone.innerText || clone.textContent || "";
  return text.replace(/\n{3,}/g, "\n\n").replace(/[ \t]{2,}/g, " ").trim();
}

export function extractContent(doc, ReadabilityCtor) {
  let title = (doc.title || "").trim();
  let text = "";
  let excerpt = "";

  try {
    // Readability mutates the document, so operate on a clone.
    const clone = doc.cloneNode(true);
    const article = new ReadabilityCtor(clone).parse();
    if (article && article.textContent && article.textContent.trim()) {
      text = article.textContent.trim();
      title = (article.title || title).trim();
      excerpt = (article.excerpt || "").trim();
    }
  } catch {
    // fall through to innerText fallback
  }

  if (!text) {
    text = innerTextFallback(doc);
  }
  if (!excerpt) {
    excerpt = text.slice(0, 280);
  }

  return { title, text, excerpt, length: text.length };
}
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `cd /Users/bkumara/personal/localvily && node --test extension/tests/extract.test.mjs`
Expected: PASS (3 tests). If the article fixture yields <1000 chars, the fixture is too small — re-capture a longer article.

- [ ] **Step 7: Run the full extension suite**

Run: `cd /Users/bkumara/personal/localvily && npm test`
Expected: PASS (all .mjs tests across smoke/bing/registry/extract).

- [ ] **Step 8: Commit**

```bash
git add extension/extract.js extension/tests/extract.test.mjs extension/tests/fixtures/article.html package.json package-lock.json
git commit -m "feat(ext): Readability content extractor with innerText fallback + fixture tests"
```

---

### Task 5: Service worker (`background.js`) — poll loop, tab orchestration, injection

**Files:**
- Create: `extension/background.js`
- Create: `extension/inject-bundle-note.md` (a short note documenting the injection approach — see Step 1)

**Interfaces:**
- Consumes: relay endpoints `/pending`, `/result/{job_id}`; injects `engines/bing.js`, `lib/Readability.js`, `extract.js` into tabs.
- Produces: a running service worker that turns `/pending` jobs into tab work and posts results. No unit tests (chrome APIs) — validated by Task 7 live acceptance.

This task is integration-heavy and chrome-API-bound. It STOPS and reports DONE_WITH_CONCERNS if the injection strategy doesn't work as written, so the controller can adjust before live testing.

- [ ] **Step 1: Decide and document the injection strategy**

MV3 `chrome.scripting.executeScript` with `files:[...]` runs classic scripts (not ES modules) in the page's isolated world. Our `engines/bing.js` and `extract.js` use `export`. Two options:
- (a) Inject `func:` closures that `import()` — not available in executeScript world.
- (b) Inject the files as classic scripts that attach to a global, by shipping a small non-module wrapper.

Chosen approach (document this in `extension/inject-bundle-note.md`): inject via `func` + an inline bundle. Specifically, `background.js` reads the engine/extract logic by injecting three `files` that are written as **classic scripts assigning to `globalThis`** — so we ship parallel non-module copies is wasteful. INSTEAD: inject `lib/Readability.js` (already a classic script defining global `Readability`) via `files`, then inject a single `func` that contains the parse/extract calls, receiving the page document via `document`. Because `executeScript func` cannot import our modules, `background.js` will pass the engine's selectors/logic by calling `chrome.scripting.executeScript` with a `func` that inlines the small amount of DOM logic, OR (preferred) we add a build step.

To avoid a build step for v1, use this concrete strategy:
- `lib/Readability.js` → injected via `files` (classic script, sets `window.Readability`).
- `extension/inject/serp.js` and `extension/inject/extract.js` → **classic (non-module) scripts** that define `window.__bingParse(k)` / `window.__bingDetectBlock()` / `window.__extract()` using the SAME logic as the module versions. To keep one source of truth, these inject files `import`-free mirror the module bodies.

Given the DRY concern, the cleanest v1 is: make `engines/bing.js` and `extract.js` **dual-mode** — plain scripts that, when run as modules, `export`, and when injected, also assign to `globalThis`. Implement by appending at the end of each module file:
```javascript
// Injection bridge: when run as a classic script in a page (no module scope), expose globally.
if (typeof window !== "undefined") { window.__bing = { name, serpUrl, detectBlock, parse }; }
```
…but `export` statements make the file a module and break classic injection.

**RESOLUTION (do this):** Add a tiny build step. Create `extension/build-inject.mjs` that concatenates each module's body (stripped of `export`) into `extension/inject/serp.js` and `extension/inject/extract.js` as classic scripts exposing `globalThis.__serp` / `globalThis.__extract`. Run it as an npm `prebuild`/manual script. This keeps `engines/bing.js` + `extract.js` as the single tested source, and the injected files are generated artifacts.

Report DONE_WITH_CONCERNS after Step 1 with your concrete plan if you believe a simpler approach fits; otherwise proceed with the build-step resolution.

- [ ] **Step 2: Write the inject-build script**

`extension/build-inject.mjs`:
```javascript
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
```

Add to repo-root `package.json` scripts: `"build:inject": "node extension/build-inject.mjs"`. Run it:
```bash
cd /Users/bkumara/personal/localvily && npm run build:inject && ls extension/inject/
```
Expected: `serp.js` and `extract.js` exist, contain no `export ` tokens (`grep -c '^export' extension/inject/*.js` → 0).

- [ ] **Step 3: Write `background.js`**

`extension/background.js`:
```javascript
const DEFAULT_SERVER = "http://localhost:15552";
const POLL_INTERVAL = 1500;
const ALARM_NAME = "poll-relay";
const ALARM_WAKE_MINUTES = 0.5;
const TAB_LOAD_TIMEOUT = 20000;
const FETCH_SETTLE_MS = 800;

let serverUrl = DEFAULT_SERVER;
let pollInFlight = false;
let initialized = false;

async function init() {
  if (initialized) return;
  const stored = await chrome.storage.local.get("serverUrl");
  if (stored.serverUrl) serverUrl = stored.serverUrl;
  initialized = true;
}

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.serverUrl) serverUrl = changes.serverUrl.newValue || DEFAULT_SERVER;
});

chrome.runtime.onInstalled.addListener(() => startPolling());
chrome.runtime.onStartup.addListener(() => startPolling());
chrome.alarms.onAlarm.addListener((a) => { if (a.name === ALARM_NAME) pollServer(); });

async function startPolling() {
  await init();
  await chrome.alarms.clear(ALARM_NAME);
  await chrome.alarms.create(ALARM_NAME, { periodInMinutes: ALARM_WAKE_MINUTES });
  pollServer();
}

async function pollServer() {
  await init();
  if (pollInFlight) return;
  pollInFlight = true;
  try {
    const resp = await fetch(`${serverUrl}/pending`);
    if (resp.ok) {
      const data = await resp.json();
      for (const tabId of data.close_tabs || []) {
        chrome.tabs.remove(tabId).catch(() => {});
      }
      for (const job of data.jobs || []) {
        // Fire each job concurrently; the relay already capped how many we got.
        handleJob(job).catch((err) => postResult(job.job_id, { error: `job_failed: ${err.message}` }));
      }
    }
  } catch {
    // relay down — retry next tick
  } finally {
    pollInFlight = false;
  }
  setTimeout(pollServer, POLL_INTERVAL);
}

async function handleJob(job) {
  if (job.kind === "search") return handleSearch(job);
  if (job.kind === "fetch") return handleFetch(job);
  return postResult(job.job_id, { error: `unknown_kind: ${job.kind}` });
}

function waitForComplete(tabId, timeout) {
  return new Promise((resolve, reject) => {
    let done = false;
    const finish = (ok) => {
      if (done) return;
      done = true;
      chrome.tabs.onUpdated.removeListener(listener);
      clearTimeout(timer);
      ok ? resolve() : reject(new Error("tab_load_timeout"));
    };
    const listener = (id, info) => { if (id === tabId && info.status === "complete") finish(true); };
    const timer = setTimeout(() => finish(false), timeout);
    chrome.tabs.onUpdated.addListener(listener);
    // In case it already completed:
    chrome.tabs.get(tabId).then((t) => { if (t.status === "complete") finish(true); }).catch(() => {});
  });
}

async function handleSearch(job) {
  // Build the SERP URL using the injected engine logic via a function call in the worker.
  // We import the module here (service worker is type:module).
  const { getEngine } = await import("./engines/index.js");
  const engine = getEngine(job.engine);
  const url = engine.serpUrl(job.query, job.k || 10);

  const tab = await chrome.tabs.create({ url, active: false });
  try {
    await waitForComplete(tab.id, TAB_LOAD_TIMEOUT);
    await new Promise((r) => setTimeout(r, FETCH_SETTLE_MS));
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["inject/serp.js"] });
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      args: [job.k || 10],
      func: (k) => {
        if (globalThis.__serp.detectBlock(document)) return { blocked: true };
        return { results: globalThis.__serp.parse(document, k) };
      },
    });
    if (result.blocked) {
      await postResult(job.job_id, { error: "blocked: bing challenge" });
    } else {
      await postResult(job.job_id, { results: result.results });
    }
  } finally {
    chrome.tabs.remove(tab.id).catch(() => {});
  }
}

async function handleFetch(job) {
  const tab = await chrome.tabs.create({ url: job.url, active: false });
  try {
    await waitForComplete(tab.id, TAB_LOAD_TIMEOUT);
    await new Promise((r) => setTimeout(r, FETCH_SETTLE_MS));
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["lib/Readability.js", "inject/extract.js"] });
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => globalThis.__extract(document),
    });
    if (!result || !result.text) {
      await postResult(job.job_id, { error: "no extractable content" });
    } else {
      await postResult(job.job_id, result);
    }
  } finally {
    chrome.tabs.remove(tab.id).catch(() => {});
  }
}

async function postResult(jobId, payload) {
  try {
    await fetch(`${serverUrl}/result/${jobId}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch {
    // relay went away; nothing to do
  }
}

// Kick off polling when the worker first loads.
startPolling();
```

- [ ] **Step 4: Lint-check the worker loads as a module**

Run (syntactic sanity — Node can parse it as a module even though chrome.* is undefined):
```bash
cd /Users/bkumara/personal/localvily && node --input-type=module -e "import('node:fs').then(fs=>{const s=fs.readFileSync('extension/background.js','utf8'); new Function(s.replace(/chrome\./g,'globalThis.__chrome_stub__?.')); console.log('parses OK')})" 2>&1 | tail -2
```
Expected: `parses OK` (this only checks syntax, not behavior).

- [ ] **Step 5: Commit**

```bash
git add extension/background.js extension/build-inject.mjs extension/inject/ extension/inject-bundle-note.md package.json package-lock.json
git commit -m "feat(ext): service worker poll loop, tab orchestration, inject build for SERP+extract"
```

---

### Task 6: Popup + options UI

**Files:**
- Create: `extension/popup.html`, `extension/popup.js`, `extension/popup.css`
- Create: `extension/options.html`, `extension/options.js`

**Interfaces:**
- Consumes: relay `/health`; `chrome.storage.local` `serverUrl`.
- Produces: a popup showing live relay/extension status; an options page to set the server URL.

- [ ] **Step 1: Options page**

`extension/options.html`:
```html
<!doctype html><html><head><meta charset="utf-8"><title>Browser Relay Options</title>
<style>body{font:13px system-ui;padding:16px;width:320px} input{width:100%;padding:6px;margin:6px 0} button{padding:6px 12px}</style>
</head><body>
  <label>Relay server URL</label>
  <input id="url" type="text" placeholder="http://localhost:15552">
  <button id="save">Save</button>
  <span id="status"></span>
  <script src="options.js"></script>
</body></html>
```

`extension/options.js`:
```javascript
const DEFAULT_SERVER = "http://localhost:15552";
const urlInput = document.getElementById("url");
const statusEl = document.getElementById("status");

chrome.storage.local.get("serverUrl").then((s) => {
  urlInput.value = s.serverUrl || DEFAULT_SERVER;
});

document.getElementById("save").addEventListener("click", async () => {
  const url = urlInput.value.trim() || DEFAULT_SERVER;
  await chrome.storage.local.set({ serverUrl: url });
  statusEl.textContent = "Saved";
  setTimeout(() => (statusEl.textContent = ""), 1500);
});
```

- [ ] **Step 2: Popup**

`extension/popup.html`:
```html
<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="popup.css"></head>
<body>
  <h3>Browser Relay</h3>
  <div id="status" class="status">Checking…</div>
  <dl id="detail"></dl>
  <button id="options">Settings</button>
  <script src="popup.js"></script>
</body></html>
```

`extension/popup.css`:
```css
body { font: 13px system-ui; width: 260px; padding: 12px; }
h3 { margin: 0 0 8px; }
.status { padding: 6px; border-radius: 4px; margin-bottom: 8px; }
.status.ok { background: #e6f4ea; color: #137333; }
.status.down { background: #fce8e6; color: #c5221f; }
dl { display: grid; grid-template-columns: auto 1fr; gap: 2px 8px; margin: 0 0 8px; font-size: 12px; }
dt { color: #666; }
button { padding: 6px 12px; }
```

`extension/popup.js`:
```javascript
const DEFAULT_SERVER = "http://localhost:15552";

async function refresh() {
  const statusEl = document.getElementById("status");
  const detail = document.getElementById("detail");
  const { serverUrl = DEFAULT_SERVER } = await chrome.storage.local.get("serverUrl");
  try {
    const resp = await fetch(`${serverUrl}/health`);
    const h = await resp.json();
    statusEl.textContent = `Relay OK · ${h.engine}`;
    statusEl.className = "status ok";
    detail.innerHTML = `
      <dt>extension</dt><dd>${h.extension_connected ? "connected" : h.extension_status}</dd>
      <dt>in flight</dt><dd>${h.in_flight}</dd>
      <dt>queued</dt><dd>${h.search_queued + h.fetch_queued}</dd>`;
  } catch {
    statusEl.textContent = `Relay unreachable at ${serverUrl}`;
    statusEl.className = "status down";
    detail.innerHTML = "";
  }
}

document.getElementById("options").addEventListener("click", () => chrome.runtime.openOptionsPage());
refresh();
```

- [ ] **Step 3: Manual sanity (no automated test — UI)**

Confirm the files are valid HTML/JS by loading the extension later (Task 7). For now, verify no syntax errors:
```bash
cd /Users/bkumara/personal/localvily && for f in extension/popup.js extension/options.js; do node --check "$f" && echo "$f OK"; done
```
Expected: both `OK`. (Note: `chrome.*` is undefined at parse time but `node --check` only checks syntax.)

- [ ] **Step 4: Commit**

```bash
git add extension/popup.html extension/popup.js extension/popup.css extension/options.html extension/options.js
git commit -m "feat(ext): popup status UI + options page for relay URL"
```

---

### Task 7: Live acceptance — wire it up and run the 5 PRD criteria

**Files:**
- Create: `tests/acceptance/run_acceptance.py` (drives the MCP tools over the running relay)
- Create: `tests/acceptance/README.md` (how to run, what each criterion checks)

**Interfaces:**
- Consumes: the running relay (`browser-relay-mcp --backend`) + the loaded extension + a live Chrome.
- Produces: a script asserting the 5 acceptance criteria; manual confirmation steps.

This task REQUIRES a human-attended Chrome with the extension loaded. The implementer cannot fully self-run it headlessly; it writes the script and the controller/human runs it, OR the implementer runs as much as possible and reports which criteria passed.

- [ ] **Step 1: Write the acceptance script**

`tests/acceptance/run_acceptance.py`:
```python
"""Live acceptance checks for browser-relay-mcp Phase 2 (relay + Chrome extension).

Prerequisites:
  1. `cd server && uv run browser-relay-mcp --backend --port 15552` running.
  2. Chrome open, signed in, with the unpacked extension (extension/) loaded and its
     server URL set to http://localhost:15552.
Run: `uv run --with httpx python tests/acceptance/run_acceptance.py`
"""
import asyncio
import sys

import httpx

BASE = "http://localhost:15552"


async def main() -> int:
    failures = []
    async with httpx.AsyncClient(base_url=BASE, timeout=130.0) as c:
        # Criterion 4: health reports connected
        h = (await c.get("/health")).json()
        if not h.get("extension_connected"):
            failures.append("C4: extension not connected — open Chrome with the extension loaded")
            print("health:", h)
            for f in failures:
                print("FAIL", f)
            return 1
        print("C4 health connected:", h["extension_status"])

        # Criterion 1: search >= 8 results
        s = (await c.get("/search", params={"q": "consistent hashing", "k": 10})).json()
        n = len(s.get("results", []))
        print(f"C1 search returned {n} results, status={s.get('status')}")
        if s.get("status") != "ok" or n < 8:
            failures.append(f"C1: expected >=8 results, got {n} (status {s.get('status')})")

        # Criterion 2: fetch a JS-heavy article >= 1000 chars
        f2 = (await c.get("/fetch", params={"url": "https://en.wikipedia.org/wiki/Consistent_hashing"})).json()
        length = f2.get("length", 0)
        print(f"C2 fetch length={length}, status={f2.get('status')}")
        if f2.get("status") != "ok" or length < 1000:
            failures.append(f"C2: expected >=1000 chars, got {length}")

        # Criterion 3: 50 sequential search+fetch with 0 failures / 0 silent-empty
        empties = 0
        errors = 0
        for i in range(50):
            q = f"distributed systems topic {i}"
            r = (await c.get("/search", params={"q": q, "k": 5})).json()
            if r.get("status") != "ok":
                errors += 1
            elif len(r.get("results", [])) == 0:
                empties += 1
        print(f"C3 burst: errors={errors} silent_empties={empties}")
        if errors or empties:
            failures.append(f"C3: burst had errors={errors} empties={empties} (must be 0/0)")

    if failures:
        for f in failures:
            print("FAIL", f)
        return 1
    print("\nALL ACCEPTANCE CRITERIA PASSED (C1, C2, C3, C4)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 2: Write the acceptance README**

`tests/acceptance/README.md`: document the two prerequisites (relay running; Chrome + unpacked extension loaded with server URL set), the run command, and that Criterion 5 (STORM adapter) is covered by `adapters/tests/test_storm.py` plus a note that `search_and_fetch` feeds `to_storm`.

- [ ] **Step 3: Controller/human runs the live acceptance**

Run (with relay + Chrome + extension up):
```bash
cd /Users/bkumara/personal/localvily && uv run --with httpx python tests/acceptance/run_acceptance.py
```
Expected: `ALL ACCEPTANCE CRITERIA PASSED`. If C3 shows any empties/errors, that is the headline failure the whole project targets — investigate Bing block detection vs. real cadence before declaring Phase 2 done.

- [ ] **Step 4: Commit**

```bash
git add tests/acceptance/
git commit -m "test(acceptance): live 5-criteria acceptance script for relay+extension"
```

---

## Self-Review

**1. Spec coverage (Plan 2 portion):**
- §6 extension manifest/perms, no-content-scripts/on-demand injection → Task 1, 5. ✓
- §9 shared DOM logic: Bing parser (`serpUrl`/`detectBlock`/`parse`), registry, Readability extract → Tasks 2, 3, 4. ✓ (single tested source; injected via generated classic-script bundles — Task 5 build step.)
- §7 search/fetch flows (dedicated/owned background tabs, settle delay, block→error, tab cleanup) → Task 5. ✓
- §6 popup/options (server URL, status) → Task 6. ✓
- Acceptance criteria #1–#5 → Task 7 (C1-C4 live; C5 via adapter tests from Plan 1). ✓
- Out of Plan 2 (correct): escalation/`action_required` (Plan 3), cloak (Plan 4), Web Store packaging (Plan 5).

**2. Placeholder scan:** Task 5 Step 1 deliberately walks the injection-strategy decision then RESOLVES to a concrete build step (not a TBD) — the implementer has runnable code in Step 2-3. Task 7 is explicitly human-attended with a complete runnable script. All code blocks are complete. No "handle errors"/"similar to"/"TODO".

**3. Type consistency:** Result payload shapes (`{results:[{title,url,snippet}]}`, `{title,text,excerpt,length}`, `{error}`) match Plan 1's `_shape_search`/`_shape_fetch` exactly. `getEngine`/`serpUrl`/`detectBlock`/`parse`/`extractContent` signatures consistent across tasks 2-5. `globalThis.__serp`/`globalThis.__extract` injection names consistent between build script (Task 5 Step 2) and worker calls (Task 5 Step 3).

---

## Notes for Plan 3 (escalation)
- `background.js` `handleSearch`/`handleFetch` already isolate the block check — Plan 3 will, on `detectBlock` true (or a login wall), set the tab `active`, keep it open, and POST an `action_required` signal instead of an error; the relay's action registry + `resume` (Plan 3 backend tasks) drive re-check.
