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
