const DEFAULT_SERVER = "http://localhost:15552";
const urlInput = document.getElementById("url");
const statusEl = document.getElementById("status");

chrome.storage.local.get("serverUrl").then((s) => {
  urlInput.value = s.serverUrl || DEFAULT_SERVER;
});

document.getElementById("save").addEventListener("click", async () => {
  const url = urlInput.value.trim() || DEFAULT_SERVER;
  await chrome.storage.local.set({ serverUrl: url });
  statusEl.textContent = "Saved";
  setTimeout(() => (statusEl.textContent = ""), 1500);
});
