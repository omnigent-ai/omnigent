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

document.getElementById("once").addEventListener("click", () => choose("allow-once"));
document.getElementById("always").addEventListener("click", () => choose("always-allow"));
document.getElementById("deny").addEventListener("click", () => choose("deny"));
document.getElementById("close").addEventListener("click", () => choose("dismiss"));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") choose("dismiss");
});

void bridge
  .getInfo()
  .then(({ origin, reload }) => {
    document.getElementById("origin").textContent = origin;
    document.getElementById("reload").hidden = !reload;
    if (!submitted) {
      for (const button of document.querySelectorAll("footer button")) button.disabled = false;
      document.getElementById("close").focus();
    }
  })
  .catch(() => choose("dismiss"));
