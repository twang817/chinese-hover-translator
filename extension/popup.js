const DEFAULT_SERVER = "http://localhost:5001";
const input = document.getElementById("server");
const status = document.getElementById("status");

chrome.storage.local.get("server").then((r) => {
  input.value = (r && r.server) || DEFAULT_SERVER;
});

document.getElementById("save").addEventListener("click", () => {
  const server = input.value.trim().replace(/\/+$/, "");
  chrome.storage.local.set({ server }, () => {
    status.textContent = "Saved ✓";
    setTimeout(() => (status.textContent = ""), 1500);
  });
});

document.getElementById("clear").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "clearCache" }, (resp) => {
    status.textContent = resp && resp.cleared != null ? `Cleared ${resp.cleared} cached` : "Cleared";
    setTimeout(() => (status.textContent = ""), 1500);
  });
});
