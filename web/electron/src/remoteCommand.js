"use strict";

// URLs embedded in remote shell commands must exclude shell metacharacters.
const SAFE_URL_RE = /^[A-Za-z0-9\-._~:/?=&%]+$/;

/** Last non-empty line of captured command output. */
function lastLine(text) {
  const lines = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  return lines[lines.length - 1] ?? text;
}

module.exports = { lastLine, SAFE_URL_RE };
