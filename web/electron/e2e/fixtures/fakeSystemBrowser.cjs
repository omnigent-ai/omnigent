// Stand-in system browser for desktop e2e runs, preloaded before main.js: it
// replaces shell.openExternal with a redirect follower that keeps cookies per
// host, and records every URL it was asked to open.
"use strict";

const { shell } = require("electron");

globalThis.fakeBrowserOpened = [];

/** Fold one Set-Cookie line into a host's jar, honoring deletion. */
function applySetCookie(cookies, line) {
  const [pair, ...attributes] = line.split(";");
  const eq = pair.indexOf("=");
  const name = pair.slice(0, eq).trim();
  const expired = attributes.some((attribute) => {
    const [key, value = ""] = attribute.split("=").map((part) => part.trim());
    if (key.toLowerCase() === "max-age") return Number(value) <= 0;
    if (key.toLowerCase() === "expires") return Date.parse(value) <= Date.now();
    return false;
  });
  if (expired) cookies.delete(name);
  else cookies.set(name, pair.slice(eq + 1).trim());
}

shell.openExternal = async (start) => {
  globalThis.fakeBrowserOpened.push(start);
  const jar = new Map();
  let url = start;
  // A browser follows a handful of redirects: server → IdP → server → app.
  for (let hop = 0; hop < 10; hop += 1) {
    const target = new URL(url);
    const cookies = jar.get(target.host) ?? new Map();
    const headers = { "User-Agent": "fake-system-browser" };
    if (cookies.size) {
      headers.Cookie = [...cookies].map(([name, value]) => `${name}=${value}`).join("; ");
    }
    // oxlint-disable-next-line no-await-in-loop -- each hop needs the previous one.
    const response = await fetch(url, { headers, redirect: "manual" });
    for (const line of response.headers.getSetCookie()) applySetCookie(cookies, line);
    jar.set(target.host, cookies);
    const location = response.headers.get("location");
    // oxlint-disable-next-line no-await-in-loop -- drain before the next hop.
    await response.arrayBuffer();
    if (response.status < 300 || response.status >= 400 || !location) {
      if (response.status >= 400) {
        console.error(`[fake browser] stopped at HTTP ${response.status}: ${url}`);
      }
      return;
    }
    url = new URL(location, url).toString();
  }
  throw new Error(`[fake browser] too many redirects, last at ${url}`);
};
