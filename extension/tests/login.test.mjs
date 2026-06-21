import { test } from "node:test";
import assert from "node:assert/strict";
import { JSDOM } from "jsdom";
import { detectLoginWall } from "../extract.js";

test("detects a login wall: password field on a signin URL", () => {
  const dom = new JSDOM(`<body><form>Please sign in <input type="password"></form></body>`,
    { url: "https://example.com/login" });
  assert.equal(detectLoginWall(dom.window.document), true);
});

test("does not misflag a normal article", () => {
  const dom = new JSDOM(`<body><article>${"word ".repeat(500)}</article></body>`,
    { url: "https://example.com/article" });
  assert.equal(detectLoginWall(dom.window.document), false);
});
