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

test("does not misflag an /author/ URL (no password field)", () => {
  // 'auth' must not substring-match 'author'; and without a password field the
  // URL signal alone never escalates.
  const dom = new JSDOM(`<body><article>${"word ".repeat(500)}</article></body>`,
    { url: "https://blog.example.com/author/jane-doe" });
  assert.equal(detectLoginWall(dom.window.document), false);
});

test("detects an oauth URL with a password field", () => {
  const dom = new JSDOM(`<body><form>Sign in<input type="password"></form></body>`,
    { url: "https://id.example.com/oauth/authorize?client_id=x" });
  assert.equal(detectLoginWall(dom.window.document), true);
});
