import { getEngine } from "./engines/index.js";

const DEFAULT_SERVER = "http://localhost:15552";
const ALARM_NAME = "poll-relay";
const ALARM_WAKE_MINUTES = 0.5;

// --- Long-poll tuning -------------------------------------------------------
// The relay holds /pending open until a job is dispatchable or POLL_WAIT_SECONDS
// elapses, so pickup is effectively instant and the request stays outstanding —
// which keeps the MV3 service worker warm (no idle termination between jobs).
// Keep POLL_WAIT_SECONDS < 30 so each fetch finishes inside the MV3 event budget;
// we re-poll immediately afterwards to open the next window.
const POLL_WAIT_SECONDS = 25;
const PENDING_FETCH_TIMEOUT = (POLL_WAIT_SECONDS + 5) * 1000;
const POLL_ERROR_BACKOFF = 1000; // relay unreachable — wait before retrying

// --- Page-readiness tuning --------------------------------------------------
// We no longer wait for tab.status === "complete" (full window.onload, which on
// an ad-heavy SERP or media-heavy article is 5-20s of mostly-irrelevant network).
// Instead we poll the live DOM and act the moment the content we need exists:
//   - search: organic results (li.b_algo) are in Bing's initial HTML response.
//   - fetch:  extract once the text has rendered and stopped growing.
const SERP_POLL_INTERVAL = 150; // ms between SERP readiness probes
const SERP_READY_TIMEOUT = 12000; // cap waiting for results to render
const FETCH_POLL_INTERVAL = 300; // ms between article readiness probes
const FETCH_READY_TIMEOUT = 15000; // cap waiting for article text
const FETCH_MIN_TEXT = 200; // chars of innerText before we consider extracting

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
// The alarm is a revival net: if the worker was terminated while no request was
// outstanding, the next alarm tick restarts the long-poll loop.
chrome.alarms.onAlarm.addListener((a) => { if (a.name === ALARM_NAME) pollServer(); });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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
  let repollDelay = 0;
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), PENDING_FETCH_TIMEOUT);
    let resp;
    try {
      resp = await fetch(`${serverUrl}/pending?wait=${POLL_WAIT_SECONDS}`, { signal: ctrl.signal });
    } finally {
      clearTimeout(timer);
    }
    if (resp.ok) {
      const data = await resp.json();
      for (const tabId of data.close_tabs || []) {
        chrome.tabs.remove(tabId).catch(() => {});
      }
      for (const job of data.jobs || []) {
        // Fire each job concurrently; the relay already capped how many we got.
        handleJob(job).catch((err) => postResult(job.job_id, { error: `job_failed: ${err.message}` }));
      }
    } else {
      repollDelay = POLL_ERROR_BACKOFF; // server hiccup — back off
    }
  } catch {
    repollDelay = POLL_ERROR_BACKOFF; // relay down / request aborted — back off
  } finally {
    pollInFlight = false;
  }
  // Re-poll right away on success: the server long-poll already paced us. On
  // error, wait a beat. Always keeping one /pending outstanding is what holds
  // the MV3 worker alive during active use.
  setTimeout(pollServer, repollDelay);
}

async function handleJob(job) {
  if (job.kind === "search") return handleSearch(job);
  if (job.kind === "fetch") return handleFetch(job);
  return postResult(job.job_id, { error: `unknown_kind: ${job.kind}` });
}

async function openOrReuseTab(job, url) {
  if (job.recheck_tab_id != null) {
    try {
      const tab = await chrome.tabs.get(job.recheck_tab_id);
      return { tab, reused: true };
    } catch {
      // tab gone — fall through to a fresh one
    }
  }
  const tab = await chrome.tabs.create({ url, active: false });
  return { tab, reused: false };
}

// Run an injected probe against a tab, returning its result or null if the tab
// is mid-navigation (executeScript throws when the frame is being replaced).
async function runProbe(tabId, opts) {
  try {
    const out = await chrome.scripting.executeScript({ target: { tabId }, ...opts });
    return out && out[0] ? out[0].result : null;
  } catch {
    return null;
  }
}

// Poll the SERP tab until organic results render (ready), a CAPTCHA/block is
// detected (blocked), or we hit the cap (timeout, returning whatever parsed so
// far — usually empty). The pure parser is re-injected each probe; that is cheap
// and idempotent, and keeps a single source of truth for selectors.
async function waitForSerp(tabId, k) {
  const deadline = Date.now() + SERP_READY_TIMEOUT;
  let latest = [];
  for (;;) {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["inject/serp.js"] }).catch(() => {});
    const probe = await runProbe(tabId, {
      args: [k],
      func: (kk) => {
        if (!globalThis.__serp) return { state: "pending", results: [] };
        if (globalThis.__serp.detectBlock(document)) return { state: "blocked", results: [] };
        const results = globalThis.__serp.parse(document, kk);
        return { state: results.length ? "ready" : "pending", results };
      },
    });
    if (probe) {
      if (probe.state === "blocked") return probe;
      if (probe.results.length) latest = probe.results;
      if (probe.state === "ready") return probe;
    }
    if (Date.now() >= deadline) {
      return { state: latest.length ? "ready" : "timeout", results: latest };
    }
    await sleep(SERP_POLL_INTERVAL);
  }
}

async function handleSearch(job) {
  // getEngine is statically imported at the top — dynamic import() is disallowed
  // in an MV3 service worker (ServiceWorkerGlobalScope), even a module worker.
  const engine = getEngine(job.engine);
  const url = engine.serpUrl(job.query, job.k || 10);

  const { tab } = await openOrReuseTab(job, url);
  let escalated = false;
  try {
    const probe = await waitForSerp(tab.id, job.k || 10);
    if (probe.state === "blocked") {
      // Surface the tab and register the action FIRST; only then mark escalated
      // so the tab is kept. If surfacing/POST throws, the finally still cleans up.
      await chrome.tabs.update(tab.id, { active: true });
      await postResult(job.job_id, { action_required: true, action: "solve_captcha", tab_id: tab.id });
      escalated = true;
    } else {
      await postResult(job.job_id, { results: probe.results });
    }
  } finally {
    if (!escalated) chrome.tabs.remove(tab.id).catch(() => {});
  }
}

// Extract once: inject Readability + the extractor, run the login probe and the
// content extraction in a single pass. Returns { login, content } or null if the
// tab is mid-navigation.
async function extractArticle(tabId) {
  await chrome.scripting
    .executeScript({ target: { tabId }, files: ["lib/Readability.js", "inject/extract.js"] })
    .catch(() => {});
  return runProbe(tabId, {
    func: () => ({ login: globalThis.__detectLogin(document), content: globalThis.__extract(document) }),
  });
}

// Poll the article tab cheaply (readyState + innerText length) until rendering
// settles — the text length stops growing between probes, or readyState hits
// "complete", or we time out — then run a single Readability pass. This avoids
// waiting on images/ads/late subresources (full onload) while still letting the
// main content render. A login wall is surfaced for the resume flow.
async function waitForArticle(tabId) {
  const deadline = Date.now() + FETCH_READY_TIMEOUT;
  let prevLen = -1;
  for (;;) {
    const probe = await runProbe(tabId, {
      func: () => ({
        state: document.readyState,
        len: (document.body && document.body.innerText ? document.body.innerText.length : 0),
      }),
    });
    const timedOut = Date.now() >= deadline;
    const settled = probe && probe.state !== "loading" && probe.len >= FETCH_MIN_TEXT && probe.len === prevLen;
    const complete = probe && probe.state === "complete" && probe.len >= FETCH_MIN_TEXT;
    if (probe) prevLen = probe.len;
    if (timedOut || settled || complete) {
      const extracted = await extractArticle(tabId);
      if (extracted) return extracted;
      if (timedOut) return { login: false, content: null };
    }
    await sleep(FETCH_POLL_INTERVAL);
  }
}

async function handleFetch(job) {
  const { tab } = await openOrReuseTab(job, job.url);
  let escalated = false;
  try {
    const { login, content } = await waitForArticle(tab.id);
    if (login) {
      await chrome.tabs.update(tab.id, { active: true });
      await postResult(job.job_id, { action_required: true, action: "login", tab_id: tab.id });
      escalated = true;
    } else if (!content || !content.text) {
      await postResult(job.job_id, { error: "no extractable content" });
    } else {
      await postResult(job.job_id, content);
    }
  } finally {
    if (!escalated) chrome.tabs.remove(tab.id).catch(() => {});
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
