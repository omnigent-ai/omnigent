// Recent-servers list logic for the desktop shell.
//
// The connect screen and the title-bar switcher both show the servers the user
// has connected to before. Each entry is a URL that may carry an optional
// user-set nickname, so a list of near-identical URLs (same host, differing
// only by a long path or random-looking subdomain) can be told apart at a
// glance.
//
// Stored in the Electron `userData/settings.json` under `recent_servers`. To
// stay backward- and forward-compatible, an entry on disk is EITHER a bare URL
// string (the historical format, still written when no nickname is set) OR a
// `{ url, label }` object (written only once a nickname exists). A plain string
// read back is treated as an unlabelled entry, and an older shell that only
// understands strings keeps working for the unlabelled entries it wrote.
//
// Only web/Node globals (URL) are used, so the same source runs unchanged under
// CommonJS (main process, `require("./recents")`) and in the renderer (the
// setup page, as `window.omnigentRecents`) — one copy keeps the two from
// drifting, mirroring `src/url.js`.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.omnigentRecents = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  /** Maximum number of entries kept in the persisted recent-servers list. */
  const MAX_RECENT_SERVERS = 5;

  /** Short display label for a server URL — its host, e.g. "localhost:8000". */
  function hostOf(url) {
    try {
      return new URL(url).host;
    } catch {
      return url;
    }
  }

  /**
   * Read a raw `recent_servers` value from disk into a clean, deduplicated list
   * of `{ url, label }` entries (most recent first). Tolerates a hand-edited or
   * corrupt file: a non-array yields `[]`, and junk entries (non-string urls,
   * empty urls) are dropped rather than throwing. A bare string becomes an
   * unlabelled entry; a duplicate url keeps its first (most recent) occurrence.
   *
   * @param {unknown} raw The stored `settings.recent_servers` value.
   * @returns {Array<{ url: string, label: string }>}
   */
  function normalizeRecents(raw) {
    if (!Array.isArray(raw)) return [];
    const out = [];
    const seen = new Set();
    for (const entry of raw) {
      let url;
      let label = "";
      if (typeof entry === "string") {
        url = entry;
      } else if (entry && typeof entry === "object" && typeof entry.url === "string") {
        url = entry.url;
        if (typeof entry.label === "string") label = entry.label;
      } else {
        continue; // junk entry — drop it
      }
      if (!url || seen.has(url)) continue;
      seen.add(url);
      out.push({ url, label });
    }
    return out;
  }

  /**
   * Record a connected server URL at the head of the list: most recent first,
   * deduplicated, capped at MAX_RECENT_SERVERS. A url already in the list keeps
   * its existing nickname when it bumps to the head — re-connecting must never
   * silently drop a label the user set.
   *
   * @param {Array<{ url: string, label: string }>} list Normalized list.
   * @param {string} url Connected server URL (already normalizeUrl()'d).
   * @returns {Array<{ url: string, label: string }>} A new list.
   */
  function rememberRecent(list, url) {
    const existing = list.find((e) => e.url === url);
    const head = { url, label: existing ? existing.label : "" };
    return [head, ...list.filter((e) => e.url !== url)].slice(0, MAX_RECENT_SERVERS);
  }

  /**
   * Set (or clear) the nickname for a url already in the list. A blank/whitespace
   * label clears it, reverting the entry to showing its URL. A url not in the
   * list is a no-op — you can only label a server you've connected to.
   *
   * @param {Array<{ url: string, label: string }>} list Normalized list.
   * @param {string} url Target server URL.
   * @param {string} label New nickname; empty/whitespace clears it.
   * @returns {Array<{ url: string, label: string }>} A new list.
   */
  function setLabel(list, url, label) {
    const trimmed = typeof label === "string" ? label.trim() : "";
    return list.map((e) => (e.url === url ? { url: e.url, label: trimmed } : e));
  }

  /**
   * Serialize a normalized list for disk: an entry with a nickname is written as
   * a `{ url, label }` object, one without stays a bare string. Keeping the
   * historical string shape for unlabelled entries means an older shell (which
   * only reads strings) still sees them, and the file stays readable.
   *
   * @param {Array<{ url: string, label: string }>} list Normalized list.
   * @returns {Array<string | { url: string, label: string }>}
   */
  function serializeRecents(list) {
    return list.map((e) => (e.label ? { url: e.url, label: e.label } : e.url));
  }

  return {
    MAX_RECENT_SERVERS,
    hostOf,
    normalizeRecents,
    rememberRecent,
    setLabel,
    serializeRecents,
  };
});
