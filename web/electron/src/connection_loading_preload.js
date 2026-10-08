"use strict";

const { ipcRenderer } = require("electron");

ipcRenderer.on("omnigent:connection-loading", (_event, label) => {
  const status = document.getElementById("status");
  if (status && typeof label === "string") status.textContent = label;
});
