"use strict";

const { isLocalhostUrl } = require("./localhost_cors");

const LOCAL_NETWORK_PERMISSIONS = new Set([
  "local-network-access",
  "local-network",
  "loopback-network",
]);

function permissionOrigin(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" || isLocalhostUrl(value) ? url.origin : null;
  } catch {
    return null;
  }
}

/** Saved site decisions apply across conversation browsers, not the shell. */
function createBrowserPermissionStore({ loadSettings, saveSettings }) {
  const revisions = new Map();
  return {
    get(origin) {
      const value = loadSettings().browser_local_network_permissions?.[origin];
      return typeof value === "boolean" ? value : undefined;
    },
    revision: (origin) => revisions.get(origin) ?? 0,
    set(origin, decision) {
      if (permissionOrigin(origin) !== origin) throw new Error("Invalid permission origin");
      if (decision !== undefined && typeof decision !== "boolean") {
        throw new Error("Invalid permission decision");
      }
      const settings = loadSettings();
      const saved = settings.browser_local_network_permissions;
      const entries =
        saved && typeof saved === "object" && !Array.isArray(saved) ? Object.entries(saved) : [];
      const choices = Object.fromEntries(entries.filter(([key]) => key !== origin));
      if (decision !== undefined) choices[origin] = decision;
      settings.browser_local_network_permissions = choices;
      saveSettings(settings);
      // Changing a saved choice also invalidates older one-visit grants.
      revisions.set(origin, (revisions.get(origin) ?? 0) + 1);
    },
  };
}

/** Install before constructing the view, then attach its webContents. */
function registerBrowserPermissions(session, { canPrompt, showPrompt, store }) {
  const visits = new Map();
  let contents = null;
  let generation = 0;
  let dismissedGeneration = null;
  let pending = null;

  function decisionFor(origin) {
    const saved = store.get(origin);
    if (saved !== undefined) return saved;
    const visit = visits.get(origin);
    return visit?.revision === store.revision(origin) ? visit.granted : undefined;
  }

  function context(webContents, requestingOrigin, details, check = false) {
    const origin = permissionOrigin(requestingOrigin);
    if (!origin || !contents || contents.isDestroyed()) return null;
    if (origin !== permissionOrigin(contents.getURL())) return null;
    if (webContents) {
      if (webContents !== contents || details.isMainFrame !== true) return null;
    } else {
      // Context-free checks must match both the embedding and current origin.
      if (!check || permissionOrigin(details.embeddingOrigin) !== origin) return null;
    }
    if (details.embeddingOrigin && permissionOrigin(details.embeddingOrigin) !== origin) {
      return null;
    }
    return { origin, generation, webContents: contents };
  }

  function isCurrent(ctx) {
    return (
      ctx.webContents === contents &&
      !contents.isDestroyed() &&
      ctx.generation === generation &&
      permissionOrigin(contents.getURL()) === ctx.origin
    );
  }

  function ask(ctx, reload) {
    if (!ctx || !isCurrent(ctx)) return Promise.resolve(false);
    const decision = decisionFor(ctx.origin);
    if (decision !== undefined) return Promise.resolve(decision);
    // Visibility gates new consent prompts, not an origin's existing grant.
    if (!canPrompt(contents)) return Promise.resolve(false);
    if (dismissedGeneration === generation) return Promise.resolve(false);
    if (pending) {
      return pending.origin === ctx.origin && pending.generation === ctx.generation
        ? pending.result
        : Promise.resolve(false);
    }

    const request = {
      ...ctx,
      controller: new AbortController(),
      storeRevision: store.revision(ctx.origin),
    };
    pending = request;
    request.result = Promise.resolve()
      .then(() => {
        if (!isCurrent(ctx) || !canPrompt(contents)) return "dismiss";
        return showPrompt({ origin: ctx.origin, reload, signal: request.controller.signal });
      })
      .then((choice) => {
        if (!isCurrent(ctx) || !canPrompt(contents)) return false;
        // Another shell window may have saved a decision while this prompt was
        // open. Never let this stale answer overwrite that newer choice.
        if (store.revision(ctx.origin) !== request.storeRevision) return false;
        if (choice === "always-allow" || choice === "deny") {
          store.set(ctx.origin, choice === "always-allow");
          visits.delete(ctx.origin);
        } else if (choice === "allow-once") {
          visits.set(ctx.origin, { granted: true, revision: request.storeRevision });
        } else {
          dismissedGeneration = generation;
          return false;
        }
        const granted = choice !== "deny";
        if (granted && reload) contents.reload();
        return granted;
      })
      .catch(() => {
        if (isCurrent(ctx)) dismissedGeneration = generation;
        return false;
      })
      .finally(() => {
        if (pending === request) pending = null;
      });
    return request.result;
  }

  session.setPermissionRequestHandler((webContents, permission, callback, details = {}) => {
    if (!LOCAL_NETWORK_PERMISSIONS.has(permission)) {
      callback(false);
      return;
    }
    const ctx = context(webContents, details.requestingUrl, details);
    void ask(ctx, false).then((granted) => {
      try {
        callback(!!(granted && ctx && isCurrent(ctx)));
      } catch {
        // Chromium may have discarded the request during navigation/teardown.
      }
    });
  });

  session.setPermissionCheckHandler((webContents, permission, requestingOrigin, details = {}) => {
    if (!LOCAL_NETWORK_PERMISSIONS.has(permission)) return false;
    const ctx = context(webContents, requestingOrigin, details, true);
    if (!ctx) return false;
    const decision = decisionFor(ctx.origin);
    if (decision !== undefined) return decision;
    // Electron's synchronous check cannot return "prompt". Query-first auth
    // flows need a reload after the user approves the request.
    void ask(ctx, true);
    return false;
  });

  return {
    attach(webContents) {
      contents = webContents;
      contents.on("did-start-navigation", (_event, url, isInPlace, isMainFrame) => {
        if (!isMainFrame || isInPlace) return;
        generation++;
        pending?.controller.abort();
        if (permissionOrigin(url) !== permissionOrigin(contents.getURL())) visits.clear();
      });
      contents.on("did-navigate", (_event, url) => {
        for (const origin of visits.keys()) {
          if (origin !== permissionOrigin(url)) visits.delete(origin);
        }
      });
      const invalidateVisit = () => {
        generation++;
        pending?.controller.abort();
        visits.clear();
      };
      contents.on("render-process-gone", invalidateVisit);
      contents.once("destroyed", invalidateVisit);
    },
  };
}

module.exports = { registerBrowserPermissions, createBrowserPermissionStore };
