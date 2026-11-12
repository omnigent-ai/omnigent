"use strict";

/**
 * The browser view registry's side of the agent browser tools.
 * Sizes views an agent acts on before the user has shown them, and runs agent
 * back / forward / reload under the same URL policy as agent navigations.
 */

// Layout size for an agent acting on a view the user has not shown yet.
const DETACHED_AGENT_VIEWPORT = { width: 1280, height: 800 };
// A history navigation that hasn't started loading by now finished from cache.
const HISTORY_IDLE_MS = 300;
// An idle page that still hasn't moved this long after the step didn't take
// it (and no veto said why).
const HISTORY_UNMOVED_MS = 1000;
const HISTORY_TIMEOUT_ERROR =
  "the page didn't respond in time, so the tab wasn't moved; take a snapshot to see where it is";
// The registry's wording for a navigation a newer one (or a close) replaced.
const SUPERSEDED = { ok: false, error: "navigation superseded or closed" };

/** `check()`, or false once the view is destroyed. */
function safely(check) {
  try {
    return check();
  } catch {
    return false;
  }
}

// Forget the last snapshot's refs and id, as a new document would.
const DROP_SNAPSHOT_JS =
  "(() => { delete window.__omni_refs__; delete window.__omni_snapshot_id__; })()";

/**
 * @param {object} deps
 * @param {Map<string, any>} deps.entries The registry's views by conversation id.
 * @param {(conversationId: string) => boolean} deps.isActive Whether a view is the shown one.
 * @param {(conversationId: string, url: string, agentContext: unknown) =>
 *   Promise<{ ok: boolean, error?: string }>} deps.checkAgentNavigationFor
 *   The registry's agent navigation policy.
 * @param {number} deps.historyLoadTimeoutMs How long a history load may take.
 * @param {(conversationId: string) => () => boolean} deps.beginAgentNavigation
 *   The registry's navigation generations: starts one, returning whether it's
 *   still the newest.
 */

function createAgentNavigation({
  entries,
  isActive,
  checkAgentNavigationFor,
  historyLoadTimeoutMs,
  beginAgentNavigation,
}) {
  // An inactive view is never laid out (0x0), so agent input would find no
  // viewport. Size it off-screen; setActive re-applies the pane's real bounds.
  function ensureAgentViewport(conversationId) {
    const entry = entries.get(conversationId);
    if (!entry || isActive(conversationId)) return;
    try {
      const bounds = entry.view.getBounds?.();
      if (bounds && bounds.width > 0 && bounds.height > 0) return;
      entry.view.setBounds({ x: 0, y: 0, ...DETACHED_AGENT_VIEWPORT });
    } catch {
      /* destroyed */
    }
  }

  // Agent back / forward / reload, with the destination checked against the issuing
  // session's agent policy. A newer navigation, a closed view or the history moving
  // cancels it, and nothing navigates past `deadlineAt`.
  async function agentNavigateHistory(
    conversationId,
    action,
    {
      deadlineAt = Date.now() + historyLoadTimeoutMs,
      agentContext = { agentRequested: true },
    } = {},
  ) {
    const entry = entries.get(conversationId);
    if (!entry) return { ok: false, error: "No browser view" };
    const isNewest = beginAgentNavigation(conversationId);
    const wc = entry.view.webContents;
    const nav = wc.navigationHistory;
    let url;
    let from;
    let unmoved;
    if (action === "reload") {
      url = wc.getURL();
      unmoved = () => wc.getURL() === url;
    } else if (action === "go_back" || action === "go_forward") {
      if (!nav || typeof nav.getActiveIndex !== "function") {
        return { ok: false, error: "this desktop shell does not support agent history navigation" };
      }
      const index = nav.getActiveIndex() + (action === "go_back" ? -1 : 1);
      if (index < 0 || index >= nav.length()) {
        return {
          ok: false,
          error: action === "go_back" ? "no previous page in this tab" : "no next page in this tab",
        };
      }
      url = nav.getEntryAtIndex(index)?.url;
      from = { index: nav.getActiveIndex(), url: wc.getURL() };
      unmoved = () =>
        nav.getActiveIndex() === from.index &&
        wc.getURL() === from.url &&
        nav.getEntryAtIndex(index)?.url === url;
    } else {
      return { ok: false, error: "Unsupported browser action" };
    }
    if (!url) return { ok: false, error: "no page to load in this tab" };
    // The URL check (DNS) may outlive the deadline; stop waiting then, and
    // the pending check's later answer finds the step expired.
    let expiry;
    const verdict = await Promise.race([
      checkAgentNavigationFor(conversationId, url, agentContext),
      new Promise((resolve) => {
        expiry = setTimeout(
          () => resolve({ ok: false, error: HISTORY_TIMEOUT_ERROR }),
          Math.max(0, deadlineAt - Date.now()),
        );
      }),
    ]).finally(() => clearTimeout(expiry));
    if (Date.now() >= deadlineAt) return { ok: false, error: HISTORY_TIMEOUT_ERROR };
    const current = () => isNewest() && entries.get(conversationId) === entry;
    if (!current() || !safely(unmoved)) return { ...SUPERSEDED };
    if (!verdict.ok) return verdict;
    entry.navigationPolicyEnforced = true;
    entry.agentContext = agentContext;
    entry.agentNavBlockedError = null;
    // Evidence the step happened, not just an idle page: the tab is at the
    // entry it was sent to, or (a reload) a document committed.
    const target = action === "reload" ? null : from.index + (action === "go_back" ? -1 : 1);
    // A reload is complete only for a new document: a same-document event
    // (a hash or history-state change) in the old one proves nothing.
    const settled = waitForLoad(
      wc,
      deadlineAt,
      (committed) => (target === null ? committed : safely(() => nav.getActiveIndex() === target)),
      { sameDocumentCounts: target !== null },
    );
    try {
      if (action === "reload") wc.reload();
      else if (action === "go_back") nav.goBack();
      else nav.goForward();
    } catch (e) {
      settled.cancel();
      return { ok: false, error: e && e.message ? e.message : String(e) };
    }
    const outcome = await settled;
    if (!current()) return { ...SUPERSEDED };
    // A redirect the agent policy blocked aborts the load, leaving the old page.
    if (entry.agentNavBlockedError) {
      return {
        ok: false,
        error: `the page redirected to a blocked address: ${entry.agentNavBlockedError}`,
      };
    }
    if (!outcome.ok) return outcome;
    // A same-document step keeps the page's window, and with it the last
    // snapshot's refs: they describe the state the tab has just left.
    try {
      await wc.executeJavaScript(DROP_SNAPSHOT_JS, false);
    } catch {
      /* the page is going away or has no script context: nothing to drop */
    }
    try {
      return { ok: true, url: wc.getURL(), title: wc.getTitle() };
    } catch {
      return { ok: false, error: "browser view closed" };
    }
  }

  // Ok once `moved(committed)` says the step happened and the main frame is idle;
  // a load failure, an unload veto, no movement or the timeout is an error, so the
  // agent is never told an old or half-loaded page loaded.
  function waitForLoad(
    wc,
    deadlineAt = Infinity,
    moved = () => true,
    { sameDocumentCounts = true } = {},
  ) {
    let cancel = () => {};
    const promise = new Promise((resolve) => {
      const startedAt = Date.now();
      const limitMs = Math.max(0, Math.min(historyLoadTimeoutMs, deadlineAt - startedAt));
      let poll;
      const finish = (outcome) => {
        clearInterval(poll);
        for (const [event, fn] of handlers) {
          try {
            wc.removeListener(event, fn);
          } catch {
            /* destroyed */
          }
        }
        resolve(outcome);
      };
      cancel = () => finish({ ok: false, error: "navigation did not start" });
      let committed = false;
      const happened = () => {
        try {
          return moved(committed) === true;
        } catch {
          return false;
        }
      };
      const isLoading = () => {
        try {
          return wc.isLoading();
        } catch {
          return null;
        }
      };
      const handlers = [
        [
          "did-stop-loading",
          () => {
            if (isLoading() === false && happened()) finish({ ok: true });
          },
        ],
        [
          "did-navigate",
          () => {
            committed = true;
          },
        ],
        [
          "did-navigate-in-page",
          (_event, _url, isMainFrame) => {
            if (!isMainFrame || !sameDocumentCounts) return;
            committed = true;
            if (happened()) finish({ ok: true });
          },
        ],
        [
          // Unhandled, Electron keeps the page: its beforeunload objected.
          "will-prevent-unload",
          () =>
            finish({
              ok: false,
              error:
                "the page asked to stay (a leave-page warning, such as for unsaved changes), so it didn't leave; nothing changed",
            }),
        ],
        [
          "did-fail-load",
          (_event, errorCode, errorDescription, _url, isMainFrame) => {
            // -3 is ERR_ABORTED: this load was replaced by a newer one, which
            // reports its own outcome.
            if (!isMainFrame || errorCode === -3) return;
            finish({
              ok: false,
              error: `the page failed to load: ${errorDescription || errorCode}`,
            });
          },
        ],
        ["destroyed", () => finish({ ok: false, error: "browser view closed" })],
      ];
      for (const [event, fn] of handlers) wc.on(event, fn);
      poll = setInterval(() => {
        const elapsed = Date.now() - startedAt;
        const loading = isLoading();
        if (loading === null) finish({ ok: false, error: "browser view closed" });
        else if (elapsed >= HISTORY_IDLE_MS && !loading && happened()) finish({ ok: true });
        else if (elapsed >= HISTORY_UNMOVED_MS && !loading) {
          finish({
            ok: false,
            error: "the page stayed where it was; take a snapshot to see where it is",
          });
        } else if (elapsed >= limitMs) {
          // The timeout is the answer, so the load mustn't go on to replace the page.
          try {
            wc.stop();
          } catch {
            /* destroyed */
          }
          finish({
            ok: false,
            error: `the page didn't finish loading within ${Math.max(1, Math.round(limitMs / 1000))} seconds; take a snapshot to see where it is`,
          });
        }
      }, 50);
    });
    promise.cancel = () => cancel();
    return promise;
  }

  return { ensureAgentViewport, agentNavigateHistory };
}

module.exports = { createAgentNavigation };
