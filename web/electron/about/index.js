"use strict";

const api = window.omnigentAbout;
const appIcon = document.getElementById("app-icon");
const productKind = document.getElementById("product-kind");
const desktopHeading = document.getElementById("desktop-heading");
const desktopVersion = document.getElementById("desktop-version");
const desktopActions = document.getElementById("desktop-actions");
const desktopCheck = document.getElementById("desktop-check");
const desktopUpdateNow = document.getElementById("desktop-update-now");
const desktopRestart = document.getElementById("desktop-restart");
const desktopResult = document.getElementById("desktop-result");
const desktopProgress = document.getElementById("desktop-progress");
const desktopProgressPercent = document.getElementById("desktop-progress-percent");
const desktopProgressBar = document.getElementById("desktop-progress-bar");
const cliBadge = document.getElementById("cli-badge");
const cliVersion = document.getElementById("cli-version");
const cliPath = document.getElementById("cli-path");
const closeButton = document.getElementById("close");

function versionOnly(value) {
  if (typeof value !== "string" || value.trim() === "") return "Unavailable";
  return value.match(/^omnigent\s+(\S+)/i)?.[1] ?? value;
}

function showResult(element, result) {
  element.textContent = result?.message ?? "";
  if (result?.state) element.dataset.state = result.state;
  else delete element.dataset.state;
  element.setAttribute("role", result?.state === "error" ? "alert" : "status");
}

function formatPercent(percent) {
  if (typeof percent !== "number" || !Number.isFinite(percent)) return 0;
  return Math.max(0, Math.min(100, Math.round(percent)));
}

let currentDesktopStatus = { state: "idle" };

function renderDesktopUpdate(status) {
  currentDesktopStatus = status && typeof status === "object" ? status : { state: "idle" };
  const state = currentDesktopStatus.state;

  desktopActions.hidden = false;
  desktopCheck.hidden = true;
  desktopCheck.disabled = false;
  desktopCheck.textContent = "Check for updates";
  desktopUpdateNow.hidden = true;
  desktopUpdateNow.disabled = false;
  desktopUpdateNow.textContent = "Update now";
  desktopRestart.hidden = true;
  desktopRestart.disabled = false;
  desktopRestart.textContent = "Restart to update";
  desktopProgress.hidden = true;
  showResult(desktopResult, null);

  if (state === "checking") {
    desktopCheck.hidden = false;
    desktopCheck.disabled = true;
    desktopCheck.textContent = "Checking…";
    return;
  }
  if (state === "none") {
    desktopCheck.hidden = false;
    showResult(desktopResult, {
      state: "up-to-date",
      message: "Omnigent Desktop is up to date.",
    });
    return;
  }
  if (state === "available") {
    desktopUpdateNow.hidden = false;
    const version = currentDesktopStatus.info?.version;
    showResult(desktopResult, {
      state: "available",
      message: version
        ? `Desktop update ${version} is available.`
        : "A desktop update is available.",
    });
    return;
  }
  if (state === "downloading") {
    const percent = formatPercent(currentDesktopStatus.progress?.percent);
    desktopActions.hidden = true;
    desktopProgress.hidden = false;
    desktopProgress.setAttribute("aria-valuenow", String(percent));
    desktopProgressPercent.textContent = `${percent}%`;
    desktopProgressBar.style.transform = `scaleX(${percent / 100})`;
    return;
  }
  if (state === "downloaded") {
    desktopRestart.hidden = false;
    const version = currentDesktopStatus.info?.version;
    showResult(desktopResult, {
      state: "available",
      message: version
        ? `Desktop update ${version} is ready to install.`
        : "A desktop update is ready to install.",
    });
    return;
  }

  desktopCheck.hidden = false;
  if (state === "error-security" || currentDesktopStatus.lastError) {
    showResult(desktopResult, {
      state: "error",
      message: "Unable to check for desktop updates.",
    });
  }
}

async function loadInfo() {
  if (!api) {
    desktopVersion.textContent = "Unavailable";
    cliVersion.textContent = "Unavailable";
    cliPath.textContent = "Unavailable";
    cliBadge.textContent = "Not detected";
    return;
  }

  try {
    const info = await api.getInfo();
    if (typeof info?.appIconDataUrl === "string" && info.appIconDataUrl.startsWith("data:image/")) {
      appIcon.src = info.appIconDataUrl;
    }
    const platformName = info?.platformName || "Desktop";
    productKind.textContent = platformName;
    desktopHeading.textContent = `Omnigent ${platformName} app`;
    desktopVersion.textContent = info?.desktopVersion || "Unavailable";

    const cli = info?.cli;
    const installed = Boolean(cli?.installed);
    cliBadge.dataset.installed = String(installed);
    cliBadge.textContent = installed ? "Detected" : "Not detected";
    cliVersion.textContent = installed ? versionOnly(cli.version) : "Unavailable";
    cliPath.textContent = installed && cli.path ? cli.path : "Unavailable";
    cliPath.title = installed && cli.path ? cli.path : "";
  } catch {
    desktopVersion.textContent = "Unavailable";
    cliBadge.dataset.installed = "false";
    cliBadge.textContent = "Not detected";
    cliVersion.textContent = "Unavailable";
    cliPath.textContent = "Unavailable";
  }
}

desktopCheck.addEventListener("click", async () => {
  const checkRevision = liveDesktopStatusRevision;
  try {
    renderDesktopUpdate({ state: "checking" });
    const status = await api.checkDesktopUpdates();
    if (liveDesktopStatusRevision === checkRevision) renderDesktopUpdate(status);
  } catch {
    if (liveDesktopStatusRevision === checkRevision) {
      renderDesktopUpdate({ state: "idle", lastError: "Update check failed" });
    }
  }
});

desktopUpdateNow.addEventListener("click", async () => {
  desktopUpdateNow.disabled = true;
  desktopUpdateNow.textContent = "Starting…";
  try {
    await api.downloadDesktopUpdate();
  } catch {
    renderDesktopUpdate({ state: "idle", lastError: "Update download failed" });
    showResult(desktopResult, { state: "error", message: "Unable to download the update." });
  }
});

desktopRestart.addEventListener("click", async () => {
  desktopRestart.disabled = true;
  desktopRestart.textContent = "Restarting…";
  try {
    await api.installDesktopUpdate();
  } catch {
    renderDesktopUpdate(currentDesktopStatus);
    showResult(desktopResult, { state: "error", message: "Unable to install the update." });
  }
});

closeButton.addEventListener("click", () => api?.close());
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  event.preventDefault();
  api?.close();
});

let liveDesktopStatusRevision = 0;
const unsubscribeDesktopStatus =
  api?.onDesktopUpdateStatus?.((status) => {
    liveDesktopStatusRevision += 1;
    renderDesktopUpdate(status);
  }) ?? (() => {});
window.addEventListener("beforeunload", unsubscribeDesktopStatus, { once: true });

void loadInfo();
if (api) {
  const snapshotRevision = liveDesktopStatusRevision;
  void api
    .getDesktopUpdateStatus()
    .then((status) => {
      if (liveDesktopStatusRevision === snapshotRevision) renderDesktopUpdate(status);
    })
    .catch(() => {
      if (liveDesktopStatusRevision === snapshotRevision) {
        renderDesktopUpdate({ state: "idle", lastError: "Update status unavailable" });
      }
    });
} else {
  renderDesktopUpdate({ state: "idle", lastError: "Update status unavailable" });
}
