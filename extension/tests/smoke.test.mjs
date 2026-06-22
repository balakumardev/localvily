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
