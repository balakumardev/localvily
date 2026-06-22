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
  // Must NOT carry &count= — Bing serves a degraded SERP for that non-human param.
  assert.ok(!/[?&]count=/.test(u), "serpUrl must not include a count param");
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
