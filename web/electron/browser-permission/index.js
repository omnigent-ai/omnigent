"use strict";

const bridge = window.omnigentBrowserPermission;
let submitted = false;

function choose(choice) {
  if (submitted) return;
  submitted = true;
  for (const button of document.querySelectorAll("button")) button.disabled = true;
  // The main process destroys this window as soon as it accepts the choice.
  void bridge.choose(choice).catch(() => {});
}

function showSecurityKey({ cookieImport, openInBrowser }) {
  document.title = "Security key";
  document.getElementById("close").ariaLabel = "Dismiss security key notice";
  document.getElementById("heading").textContent = "Use your security key";
  document.getElementById("description").textContent =
    "Insert your security key and touch it to continue. Built-in biometrics and passkeys saved on this device can't be used here.";
  document.getElementById("cookie-hint").hidden = !cookieImport;
  document.getElementById("other-method-hint").hidden = openInBrowser;
  document.getElementById("permission-choices").hidden = true;
  document.getElementById("security-key-choices").hidden = !openInBrowser;
}

document.getElementById("once").addEventListener("click", () => choose("allow-once"));
document.getElementById("always").addEventListener("click", () => choose("always-allow"));
document.getElementById("deny").addEventListener("click", () => choose("deny"));
document
  .getElementById("open-in-browser")
  .addEventListener("click", () => choose("open-in-browser"));
document.getElementById("close").addEventListener("click", () => choose("dismiss"));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") choose("dismiss");
});

void bridge
  .getInfo()
  .then((info) => {
    document.getElementById("origin").textContent = info.origin;
    document.getElementById("reload").hidden = !info.reload;
    if (info.kind === "security-key") showSecurityKey(info);
    if (!submitted) {
      for (const button of document.querySelectorAll("footer button")) button.disabled = false;
      document.getElementById("prompt").hidden = false;
      document.getElementById("close").focus();
    }
  })
  .catch(() => choose("failed"));
