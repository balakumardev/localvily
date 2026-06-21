import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// The inject/*.js bundles are generated from the ES-module sources by build-inject.mjs.
// These tests guard against (a) a surviving `export` token that would break classic-script
// injection, (b) the exposed globals drifting from what background.js calls, and
// (c) someone hand-editing a generated file without regenerating.

const injectFile = (name) =>
  readFileSync(new URL(`../inject/${name}`, import.meta.url), "utf8");

test("generated inject bundles contain no export tokens (classic scripts)", () => {
  for (const name of ["serp.js", "extract.js"]) {
    const src = injectFile(name);
    assert.ok(!/\bexport\b/.test(src), `${name} must not contain an export token`);
  }
});

test("generated inject bundles expose the globals background.js calls", () => {
  assert.match(injectFile("serp.js"), /globalThis\.__serp\s*=\s*\{[^}]*\bparse\b/);
  assert.match(injectFile("extract.js"), /globalThis\.__extract\s*=/);
});

test("committed inject bundles match a fresh build (no drift)", () => {
  const before = { serp: injectFile("serp.js"), extract: injectFile("extract.js") };
  const script = fileURLToPath(new URL("../build-inject.mjs", import.meta.url));
  execFileSync(process.execPath, [script]);
  assert.equal(injectFile("serp.js"), before.serp, "inject/serp.js is stale — run npm run build:inject");
  assert.equal(injectFile("extract.js"), before.extract, "inject/extract.js is stale — run npm run build:inject");
});
