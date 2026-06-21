import { getEngine } from "./engines/index.js";

const DEFAULT_SERVER = "http://localhost:15552";
const POLL_INTERVAL = 1500;
const ALARM_NAME = "poll-relay";
const ALARM_WAKE_MINUTES = 0.5;
const TAB_LOAD_TIMEOUT = 20000;
const FETCH_SETTLE_MS = 800;

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
chrome.alarms.onAlarm.addListener((a) => { if (a.name === ALARM_NAME) pollServer(); });

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
  try {
    const resp = await fetch(`${serverUrl}/pending`);
    if (resp.ok) {
      const data = await resp.json();
      for (const tabId of data.close_tabs || []) {
        chrome.tabs.remove(tabId).catch(() => {});
      }
      for (const job of data.jobs || []) {
        // Fire each job concurrently; the relay already capped how many we got.
        handleJob(job).catch((err) => postResult(job.job_id, { error: `job_failed: ${err.message}` }));
      }
    }
  } catch {
    // relay down — retry next tick
  } finally {
    pollInFlight = false;
  }
  setTimeout(pollServer, POLL_INTERVAL);
}

async function handleJob(job) {
  if (job.kind === "search") return handleSearch(job);
  if (job.kind === "fetch") return handleFetch(job);
  return postResult(job.job_id, { error: `unknown_kind: ${job.kind}` });
}

function waitForComplete(tabId, timeout) {
  return new Promise((resolve, reject) => {
    let done = false;
    const finish = (ok) => {
      if (done) return;
      done = true;
      chrome.tabs.onUpdated.removeListener(listener);
      clearTimeout(timer);
      ok ? resolve() : reject(new Error("tab_load_timeout"));
    };
    const listener = (id, info) => { if (id === tabId && info.status === "complete") finish(true); };
    const timer = setTimeout(() => finish(false), timeout);
    chrome.tabs.onUpdated.addListener(listener);
    // In case it already completed:
    chrome.tabs.get(tabId).then((t) => { if (t.status === "complete") finish(true); }).catch(() => {});
  });
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

async function handleSearch(job) {
  // getEngine is statically imported at the top — dynamic import() is disallowed
  // in an MV3 service worker (ServiceWorkerGlobalScope), even a module worker.
  const engine = getEngine(job.engine);
  const url = engine.serpUrl(job.query, job.k || 10);

  const { tab } = await openOrReuseTab(job, url);
  let escalated = false;
  try {
    await waitForComplete(tab.id, TAB_LOAD_TIMEOUT);
    await new Promise((r) => setTimeout(r, FETCH_SETTLE_MS));
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["inject/serp.js"] });
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      args: [job.k || 10],
      func: (k) => {
        if (globalThis.__serp.detectBlock(document)) return { blocked: true };
        return { results: globalThis.__serp.parse(document, k) };
      },
    });
    if (result.blocked) {
      // Surface the tab and register the action FIRST; only then mark escalated
      // so the tab is kept. If surfacing/POST throws, the finally still cleans up.
      await chrome.tabs.update(tab.id, { active: true });
      await postResult(job.job_id, { action_required: true, action: "solve_captcha", tab_id: tab.id });
      escalated = true;
    } else {
      await postResult(job.job_id, { results: result.results });
    }
  } finally {
    if (!escalated) chrome.tabs.remove(tab.id).catch(() => {});
  }
}

async function handleFetch(job) {
  const { tab } = await openOrReuseTab(job, job.url);
  let escalated = false;
  try {
    await waitForComplete(tab.id, TAB_LOAD_TIMEOUT);
    await new Promise((r) => setTimeout(r, FETCH_SETTLE_MS));
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["lib/Readability.js", "inject/extract.js"] });
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => ({ login: globalThis.__detectLogin(document), content: globalThis.__extract(document) }),
    });
    if (result.login) {
      await chrome.tabs.update(tab.id, { active: true });
      await postResult(job.job_id, { action_required: true, action: "login", tab_id: tab.id });
      escalated = true;
    } else if (!result.content || !result.content.text) {
      await postResult(job.job_id, { error: "no extractable content" });
    } else {
      await postResult(job.job_id, result.content);
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
