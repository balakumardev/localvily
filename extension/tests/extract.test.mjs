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
  // Wikipedia's left-nav contains 'Random article'/'What links here'; the extracted
  // main content should contain the article prose but NOT those navigation labels.
  assert.match(out.text, /hash/i, "article body present");
  assert.ok(!/Random article/.test(out.text), "left-nav 'Random article' stripped");
  assert.ok(!/What links here/.test(out.text), "tools-nav 'What links here' stripped");
});

test("falls back to innerText when Readability yields nothing", () => {
  const dom = new JSDOM(`<html><body><nav>menu</nav><main>Hello world body content here.</main><script>x=1</script></body></html>`);
  const out = extractContent(dom.window.document, Readability);
  assert.match(out.text, /Hello world body content/);
  assert.ok(!/x=1/.test(out.text), "script content stripped");
});
