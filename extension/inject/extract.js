// GENERATED FILE — DO NOT EDIT.
// Built from the ES-module source by extension/build-inject.mjs (`npm run build:inject`).
// Edit the source module instead, then regenerate.

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

function extractContent(doc, ReadabilityCtor) {
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

globalThis.__extract = (doc) => extractContent(doc, window.Readability);
