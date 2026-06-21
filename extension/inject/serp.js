// Bing SERP parser. Pure functions — no chrome.* APIs — so it runs under jsdom in
// tests and is injected into the live SERP tab by the service worker at runtime.
//
// Selectors verified against extension/tests/fixtures/bing-serp.html (a real Bing
// results page for "consistent hashing", captured 2026-06-20 via a no-User-Agent
// curl request — Bing serves a Cloudflare-Turnstile CAPTCHA to the desktop-Chrome
// UA but real organic results to a plain request):
//   - Organic results: `li.b_algo`  (10 present in the fixture)
//   - Title + link:     `h2 a`       (href is a Bing `https://www.bing.com/ck/a?...`
//                                     redirect wrapper — still absolute http(s))
//   - Snippet:          `.b_caption p` (with `.b_lineclamp*` / `.b_caption` fallbacks)
//
// Block detection is verified against extension/tests/fixtures/bing-blocked.html (the
// REAL Bing CAPTCHA page Bing served to the desktop-Chrome UA). That page uses
// `div.captcha` ("One last step" / "Please solve the challenge below to continue") and
// a `challenges.cloudflare.com/turnstile` <script> — NOT the `#b_captchaContainer` /
// iframe markup originally assumed. detectBlock matches the real signals plus a few
// generic robot/traffic phrases so a hand-authored challenge page also trips it.
//
// If Bing changes layout, re-capture the fixture and update these selectors together.
const name = "bing";

const RESULT_SELECTOR = "li.b_algo";
const TITLE_LINK_SELECTOR = "h2 a";
const SNIPPET_SELECTORS = [".b_caption p", "p.b_lineclamp2", "p.b_lineclamp3", ".b_caption"];

function serpUrl(query, k = 10) {
  const count = Math.max(1, Math.min(k, 50));
  return `https://www.bing.com/search?q=${encodeURIComponent(query)}&count=${count}`;
}

function detectBlock(doc) {
  // Real Bing CAPTCHA markup.
  if (doc.querySelector("div.captcha, #b_captchaContainer, .cf-turnstile")) return true;
  // Cloudflare Turnstile challenge embed (Bing loads it as a <script>; hand-authored
  // challenge pages may use an <iframe>).
  if (doc.querySelector(
    'script[src*="challenges.cloudflare.com"], iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]'
  )) return true;
  const text = (doc.body?.textContent || "").toLowerCase();
  if (text.includes("solve the challenge")) return true;
  if (/verify you are (not |a )?(human|robot)/.test(text)) return true;
  if (/unusual traffic|automated queries/.test(text)) return true;
  return false;
}

function parse(doc, k = 10) {
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

globalThis.__serp = { name, serpUrl, detectBlock, parse };
