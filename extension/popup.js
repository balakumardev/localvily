const DEFAULT_SERVER = "http://localhost:15552";

async function refresh() {
  const statusEl = document.getElementById("status");
  const detail = document.getElementById("detail");
  const { serverUrl = DEFAULT_SERVER } = await chrome.storage.local.get("serverUrl");
  try {
    const resp = await fetch(`${serverUrl}/health`);
    const h = await resp.json();
    statusEl.textContent = `Relay OK · ${h.engine}`;
    statusEl.className = "status ok";
    const drivers = h.drivers || {};
    const cloak = drivers.cloak || {};
    detail.innerHTML = `
      <dt>extension</dt><dd>${h.extension_connected ? "connected" : h.extension_status}</dd>
      <dt>cloak</dt><dd>${cloak.available ? "ready" : "unavailable"}</dd>
      <dt>in flight</dt><dd>${h.in_flight}</dd>
      <dt>queued</dt><dd>${h.search_queued + h.fetch_queued}</dd>`;
  } catch {
    statusEl.textContent = `Relay unreachable at ${serverUrl}`;
    statusEl.className = "status down";
    detail.innerHTML = "";
  }
}

document.getElementById("options").addEventListener("click", () => chrome.runtime.openOptionsPage());
refresh();
