// IPC surface for the embedded browser pane, extracted out of main.js. main.js
// wires the per-window registry + trust gate and calls `registerBrowserIpc(...)`.
//
// SECURITY (Risk-2): every handler is gated on `isPinnedOriginSender` and
// resolves the sender window's own registry, so one window can never drive
// another's panes. Do NOT drop the gate from any handler (toolbar ones included).

"use strict";

const crypto = require("node:crypto");

// Max age of a real native input event for a design-mode submit marker to be
// honored (see the gesture gate below). Covers click/Enter → console.log.
const DESIGN_MODE_GESTURE_WINDOW_MS = 1500;

/**
 * Detach design-mode listeners (console-message + input-event) off an entry and
 * null them out. Safe when none attached; shared by enable-cleanup and disable.
 *
 * @param {object} entry registry entry
 */
function detachDesignModeListeners(entry) {
  if (!entry) return;
  const wc = entry.designModeWebContents;
  if (wc) {
    if (entry.designModeListener) {
      try {
        wc.removeListener("console-message", entry.designModeListener);
      } catch {
        /* destroyed */
      }
    }
    if (entry.designModeInputListener) {
      try {
        wc.removeListener("input-event", entry.designModeInputListener);
      } catch {
        /* destroyed */
      }
    }
  }
  entry.designModeListener = null;
  entry.designModeInputListener = null;
  entry.designModeWebContents = null;
}

/**
 * Read back/forward availability off a webContents. Prefers the Electron 42
 * `navigationHistory` API, falls back to the deprecated top-level methods for
 * older Electron. Never throws.
 *
 * @param {Electron.WebContents} wc
 * @returns {{ canGoBack: boolean, canGoForward: boolean }}
 */
function readNavState(wc) {
  try {
    const nav = wc.navigationHistory;
    if (nav && typeof nav.canGoBack === "function") {
      return { canGoBack: !!nav.canGoBack(), canGoForward: !!nav.canGoForward() };
    }
    if (typeof wc.canGoBack === "function") {
      return { canGoBack: !!wc.canGoBack(), canGoForward: !!wc.canGoForward() };
    }
  } catch {
    /* destroyed / mid-teardown */
  }
  return { canGoBack: false, canGoForward: false };
}

/** Navigate back through history, preferring the Electron 42 navigationHistory
 *  API. Returns true if a back navigation was issued. Never throws. */
function goBack(wc) {
  try {
    const nav = wc.navigationHistory;
    if (nav && typeof nav.canGoBack === "function") {
      if (nav.canGoBack()) {
        nav.goBack();
        return true;
      }
      return false;
    }
    if (typeof wc.canGoBack === "function" && wc.canGoBack()) {
      wc.goBack();
      return true;
    }
  } catch {
    /* destroyed */
  }
  return false;
}

/** Navigate forward through history. Returns true if issued. Never throws. */
function goForward(wc) {
  try {
    const nav = wc.navigationHistory;
    if (nav && typeof nav.canGoForward === "function") {
      if (nav.canGoForward()) {
        nav.goForward();
        return true;
      }
      return false;
    }
    if (typeof wc.canGoForward === "function" && wc.canGoForward()) {
      wc.goForward();
      return true;
    }
  } catch {
    /* destroyed */
  }
  return false;
}

/**
 * Wire nav listeners onto a new view so the URL bar live-tracks the real url
 * (redirects, in-page links, agent nav). Fires `browser-url-changed` +
 * `browser-nav-state` to the owning renderer. Attached once at create time.
 *
 * @param {object} params
 * @param {string} params.conversationId
 * @param {Electron.WebContents} params.webContents
 * @param {(channel: string, payload: unknown) => void} params.send  window-scoped sender
 */
function attachNavListeners({ conversationId, webContents, send }) {
  const emitUrl = (url) => {
    send("browser-url-changed", { conversationId, url });
    const { canGoBack, canGoForward } = readNavState(webContents);
    send("browser-nav-state", { conversationId, canGoBack, canGoForward });
  };
  // Full main-frame navigation (loadURL, redirects, back/forward, reload).
  webContents.on("did-navigate", (_e, url) => emitUrl(url));
  // SPA route changes / hash links / history.pushState within the same doc.
  webContents.on("did-navigate-in-page", (_e, url, isMainFrame) => {
    if (isMainFrame) emitUrl(url);
  });
}

// ── Design mode (point-and-prompt) ─────────────────────────────────────────
// A toolbar toggle injects an in-page picker: hover highlights, click opens an
// anchored input+Send popup, Send routes the element + a cropped screenshot to
// the agent via the normal chat path (no backend route — pure client affordance).
// The injected script (can't require electron) reports back over `console.log`
// markers, which the console listener below forwards to the owning renderer:
//   __omni_<nonce>_element_select__<json>         element clicked, popup shown
//   __omni_<nonce>_element_prompt_submit__<json>  user pressed Send / Enter
//   __omni_<nonce>_element_dismiss__              user pressed × / Escape
//
// SECURITY (console.log is a main-world back-channel a hostile top page could
// forge, so two layers):
//   1. Gesture gate (primary): a submit marker is honored only if a REAL native
//      input event landed in the view within DESIGN_MODE_GESTURE_WINDOW_MS —
//      page JS can't synthesize one, so unattended forged submits are dropped.
//   2. Nonce (defense-in-depth): every marker must echo a per-enable random
//      nonce; a cross-realm iframe can't read the top frame's console to learn it.

/**
 * Per-conversation design-mode console listener, bound to its webContents,
 * conversationId, the per-enable `nonce`, and a `gestureState` ref. A late
 * marker is tagged with its own conversationId and can't mutate another's
 * state. Stored on the registry entry so `close()` detaches it.
 *
 * @param {string} conversationId
 * @param {object} entry  registry entry (holds `.view`)
 * @param {(channel: string, payload: unknown) => void} send  window-scoped sender
 * @param {string} nonce  per-enable secret every legit marker must echo
 * @param {{ lastGestureAt: number }} gestureState  updated by the input-event listener
 * @returns {(event: unknown, level: unknown, message: unknown) => void}
 */
function makeDesignModeConsoleHandler(conversationId, entry, send, nonce, gestureState) {
  const SELECT = `__omni_${nonce}_element_select__`;
  const SUBMIT = `__omni_${nonce}_element_prompt_submit__`;
  const DISMISS = `__omni_${nonce}_element_dismiss__`;
  return (_event, _level, message) => {
    // The webContents may be destroyed mid-callback during teardown; bail
    // rather than fire against a dead object.
    if (!entry || !entry.view || entry.view.webContents.isDestroyed?.()) return;
    if (typeof message !== "string") return;
    // Nonce gate: any marker whose prefix doesn't carry THIS view's nonce is
    // ignored outright (stops cross-realm/iframe forgery).
    if (message.startsWith(SELECT)) {
      (async () => {
        try {
          const info = JSON.parse(message.slice(SELECT.length));
          let screenshotDataUrl = null;
          if (info.rect && info.rect.width > 0 && info.rect.height > 0) {
            const dpr = entry.view.webContents.getZoomFactor() || 1;
            const image = await entry.view.webContents.capturePage({
              x: Math.round(info.rect.x * dpr),
              y: Math.round(info.rect.y * dpr),
              width: Math.round(info.rect.width * dpr),
              height: Math.round(info.rect.height * dpr),
            });
            screenshotDataUrl = "data:image/png;base64," + image.toPNG().toString("base64");
          }
          send("browser-element-selected", { conversationId, ...info, screenshot: screenshotDataUrl });
        } catch (e) {
          console.error("[design-mode]", e);
        }
      })();
      return;
    }
    if (message.startsWith(SUBMIT)) {
      // GESTURE GATE — the load-bearing check. A submit is an auto-send into
      // the agent, so it must be backed by a real, recent native gesture in
      // the view. No recent native input → drop silently (forged/unattended).
      const now = Date.now();
      const sinceGesture = now - (gestureState.lastGestureAt || 0);
      if (!gestureState.lastGestureAt || sinceGesture > DESIGN_MODE_GESTURE_WINDOW_MS) {
        return;
      }
      try {
        const payload = JSON.parse(message.slice(SUBMIT.length));
        send("browser-element-prompt-submit", { conversationId, ...payload });
      } catch (e) {
        console.error("[design-mode]", e);
      }
      return;
    }
    if (message === DISMISS) {
      send("browser-element-prompt-dismiss", { conversationId });
    }
  };
}

/**
 * Native input-event listener: stamps `gestureState.lastGestureAt` on a real
 * mouse/key-down inside the view. Page JS can't synthesize native input, so a
 * fresh stamp proves genuine interaction — the gesture gate the console handler
 * requires before honoring a submit marker.
 *
 * @param {{ lastGestureAt: number }} gestureState
 * @returns {(event: unknown, input: { type?: string }) => void}
 */
function makeDesignModeInputHandler(gestureState) {
  return (_event, input) => {
    const type = input && input.type;
    // mouseDown = click-to-select; keyDown = Enter-to-submit; rawKeyDown = pre-IME.
    if (type === "mouseDown" || type === "keyDown" || type === "rawKeyDown") {
      gestureState.lastGestureAt = Date.now();
    }
  };
}

// In-page design-mode driver injected via `executeJavaScript`: hover overlay +
// label + anchored input/Send popup, hosted in the page DOM (the native view
// paints over its rect, so a React popup there would be hidden). Built per-enable
// with a fresh `nonce` (hex, safe to interpolate) baked into every marker prefix;
// the console handler trusts only markers carrying THIS view's nonce.
function buildDesignModeScript(nonce) {
  const SELECT = "__omni_" + nonce + "_element_select__";
  const SUBMIT = "__omni_" + nonce + "_element_prompt_submit__";
  const DISMISS = "__omni_" + nonce + "_element_dismiss__";
  return `
(function() {
  if (window.__omniDesignMode) return;
  window.__omniDesignMode = true;
  var __OMNI_SELECT = ${JSON.stringify(SELECT)};
  var __OMNI_SUBMIT = ${JSON.stringify(SUBMIT)};
  var __OMNI_DISMISS = ${JSON.stringify(DISMISS)};

  const overlay = document.createElement('div');
  overlay.id = '__omni-highlight';
  overlay.style.cssText = 'position:fixed;pointer-events:none;z-index:2147483646;border:2px solid #c15f3c;background:rgba(193,95,60,0.08);transition:all 0.1s ease;display:none;';
  document.body.appendChild(overlay);
  const label = document.createElement('div');
  label.id = '__omni-label';
  label.style.cssText = 'position:fixed;z-index:2147483646;pointer-events:none;background:#c15f3c;color:#fff;font:11px/1.4 -apple-system,sans-serif;padding:2px 6px;border-radius:3px;display:none;white-space:nowrap;';
  document.body.appendChild(label);

  const popup = document.createElement('div');
  popup.id = '__omni-popup';
  popup.style.cssText = [
    'position:fixed', 'display:none', 'z-index:2147483647',
    'background:rgba(28,28,30,0.96)', 'color:#f5f5f7',
    'border:1px solid rgba(255,255,255,0.12)', 'border-radius:12px',
    'box-shadow:0 10px 28px rgba(0,0,0,0.45)',
    'padding:10px 12px', 'min-width:280px', 'max-width:380px',
    'font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif',
    'font-size:13px', 'letter-spacing:-0.01em',
    'backdrop-filter:blur(20px)', '-webkit-backdrop-filter:blur(20px)',
  ].join(';') + ';';
  popup.innerHTML =
    '<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">' +
      '<span id="__omni-popup-tag" style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:#0a84ff;font-weight:600;"></span>' +
      '<span id="__omni-popup-text" style="flex:1;color:#aaaaae;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"></span>' +
      '<button id="__omni-popup-close" type="button" style="background:none;border:none;color:#7c7c80;cursor:pointer;font-size:18px;line-height:1;padding:0 4px;font-family:inherit;">&times;</button>' +
    '</div>' +
    '<div id="__omni-popup-row" style="display:flex;gap:6px;">' +
      '<input id="__omni-popup-input" type="text" placeholder="What should change?" autocomplete="off" spellcheck="false" ' +
        'style="flex:1;padding:7px 10px;font-size:13px;border:1px solid rgba(255,255,255,0.14);border-radius:8px;background:rgba(0,0,0,0.32);color:#f5f5f7;outline:none;font-family:inherit;" />' +
      '<button id="__omni-popup-send" type="button" ' +
        'style="padding:7px 14px;background:#0a84ff;color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:13px;font-weight:500;font-family:inherit;transition:opacity 0.12s;">Send</button>' +
    '</div>' +
    '<div id="__omni-popup-feedback" style="display:none;font-size:13px;font-weight:500;padding:4px 0;"></div>' +
    '<div id="__omni-popup-arrow" style="position:absolute;width:12px;height:12px;background:rgba(28,28,30,0.96);border:1px solid rgba(255,255,255,0.12);display:none;"></div>';
  document.body.appendChild(popup);

  const popupTag = popup.querySelector('#__omni-popup-tag');
  const popupText = popup.querySelector('#__omni-popup-text');
  const popupClose = popup.querySelector('#__omni-popup-close');
  const popupRow = popup.querySelector('#__omni-popup-row');
  const popupInput = popup.querySelector('#__omni-popup-input');
  const popupSend = popup.querySelector('#__omni-popup-send');
  const popupFeedback = popup.querySelector('#__omni-popup-feedback');
  const popupArrow = popup.querySelector('#__omni-popup-arrow');

  let currentEl = null;
  let activeEl = null;
  let popupVisible = false;
  let sending = false;

  function getReactComponent(el) {
    let fiber = null;
    for (const key of Object.keys(el)) {
      if (key.startsWith('__reactFiber$') || key.startsWith('__reactInternalInstance$')) { fiber = el[key]; break; }
    }
    if (!fiber) return null;
    let node = fiber;
    for (let i = 0; i < 20 && node; i++) {
      if (node.type && typeof node.type === 'function') return node.type.displayName || node.type.name || null;
      if (node.type && typeof node.type === 'object' && node.type.render) return node.type.displayName || node.type.render.displayName || node.type.render.name || null;
      node = node.return;
    }
    return null;
  }

  function getElementInfo(el) {
    const rect = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const tag = el.tagName.toLowerCase();
    return {
      tag, id: el.id ? '#' + el.id : '',
      classes: el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\\s+/).slice(0,3).join('.') : '',
      text: (el.textContent || '').trim().slice(0, 80),
      testId: el.getAttribute('data-testid') || '',
      ariaLabel: el.getAttribute('aria-label') || '',
      role: el.getAttribute('role') || '',
      component: getReactComponent(el),
      rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      styles: { color: cs.color, backgroundColor: cs.backgroundColor, fontSize: cs.fontSize, fontWeight: cs.fontWeight, padding: cs.padding, margin: cs.margin, display: cs.display, position: cs.position }
    };
  }

  function positionPopup(targetRect) {
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const gap = 8;
    popup.style.left = '-9999px';
    popup.style.top = '0px';
    popup.style.display = 'block';
    const popW = popup.offsetWidth;
    const popH = popup.offsetHeight;
    let top = targetRect.bottom + gap;
    let arrowOnTop = true;
    if (top + popH > vh - 8) {
      top = targetRect.top - popH - gap;
      arrowOnTop = false;
    }
    if (top < 8) top = 8;
    let left = targetRect.left + (targetRect.width / 2) - (popW / 2);
    if (left < 8) left = 8;
    if (left + popW > vw - 8) left = vw - popW - 8;
    popup.style.left = left + 'px';
    popup.style.top = top + 'px';
    const arrowSize = 12;
    let arrowLeft = targetRect.left + (targetRect.width / 2) - left - (arrowSize / 2);
    if (arrowLeft < 10) arrowLeft = 10;
    if (arrowLeft > popW - arrowSize - 10) arrowLeft = popW - arrowSize - 10;
    popupArrow.style.display = 'block';
    popupArrow.style.left = arrowLeft + 'px';
    if (arrowOnTop) {
      popupArrow.style.top = (-arrowSize / 2 - 1) + 'px';
      popupArrow.style.bottom = '';
      popupArrow.style.borderRight = 'none';
      popupArrow.style.borderBottom = 'none';
      popupArrow.style.transform = 'rotate(45deg)';
    } else {
      popupArrow.style.bottom = (-arrowSize / 2 - 1) + 'px';
      popupArrow.style.top = '';
      popupArrow.style.borderLeft = 'none';
      popupArrow.style.borderTop = 'none';
      popupArrow.style.transform = 'rotate(45deg)';
    }
  }

  let submitId = 0;
  let resultTimer = null;

  function resetInputRow() {
    popupRow.style.display = 'flex';
    popupFeedback.style.display = 'none';
    popupFeedback.textContent = '';
    popupInput.value = '';
    popupInput.disabled = false;
    popupSend.disabled = false;
    popupSend.textContent = 'Send';
    popupSend.style.opacity = '1';
    popupSend.style.cursor = 'pointer';
  }

  function showPopup(el, info) {
    activeEl = el;
    const niceTag = info.component ? '<' + info.component + '>' : '<' + info.tag + '>';
    popupTag.textContent = niceTag;
    popupText.textContent = info.text ? '\\u201c' + info.text.slice(0, 40) + '\\u201d' : '';
    resetInputRow();
    sending = false;
    positionPopup(el.getBoundingClientRect());
    popupVisible = true;
    overlay.style.left = info.rect.x + 'px';
    overlay.style.top = info.rect.y + 'px';
    overlay.style.width = info.rect.width + 'px';
    overlay.style.height = info.rect.height + 'px';
    overlay.style.display = 'block';
    setTimeout(function() { popupInput.focus(); popupInput.select(); }, 30);
  }

  function hidePopup(emitDismiss) {
    if (resultTimer) { clearTimeout(resultTimer); resultTimer = null; }
    popup.style.display = 'none';
    activeEl = null;
    popupVisible = false;
    sending = false;
    popupRow.style.display = 'flex';
    popupFeedback.style.display = 'none';
    popupInput.disabled = false;
    popupSend.disabled = false;
    if (emitDismiss) console.log(__OMNI_DISMISS);
  }

  function showFeedback(ok, message) {
    popupRow.style.display = 'none';
    popupFeedback.textContent = message;
    popupFeedback.style.color = ok ? '#30d158' : '#ff453a';
    popupFeedback.style.display = 'block';
  }

  window.__omniOnDesignResult = function(result) {
    if (!result || result.id !== submitId) return;
    if (!popupVisible || !sending) return;
    showFeedback(!!result.ok, String(result.message || (result.ok ? 'Applied.' : 'Failed.')));
    if (resultTimer) clearTimeout(resultTimer);
    resultTimer = setTimeout(function() { hidePopup(false); }, result.ok ? 900 : 2400);
  };

  function submitPopup() {
    if (sending) return;
    const text = popupInput.value.trim();
    if (!text || !activeEl) return;
    sending = true;
    submitId += 1;
    const id = submitId;
    popupSend.textContent = 'Sending\\u2026';
    popupSend.disabled = true;
    popupSend.style.opacity = '0.6';
    popupSend.style.cursor = 'default';
    popupInput.disabled = true;
    const info = getElementInfo(activeEl);
    console.log(__OMNI_SUBMIT + JSON.stringify({ id: id, element: info, prompt: text }));
    if (resultTimer) clearTimeout(resultTimer);
    resultTimer = setTimeout(function() {
      if (!popupVisible || !sending || submitId !== id) return;
      showFeedback(false, 'No response (timed out).');
      resultTimer = setTimeout(function() { hidePopup(false); }, 1500);
    }, 8000);
  }

  popupClose.addEventListener('click', function(e) { e.stopPropagation(); hidePopup(true); });
  popupSend.addEventListener('click', function(e) { e.stopPropagation(); submitPopup(); });
  popupInput.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitPopup(); return; }
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); hidePopup(true); }
  });

  function onMouseMove(e) {
    if (popupVisible) return;
    const el = document.elementFromPoint(e.clientX, e.clientY);
    if (!el || el === overlay || el === label) return;
    if (popup.contains(el)) return;
    currentEl = el;
    const rect = el.getBoundingClientRect();
    overlay.style.display = 'block';
    overlay.style.left = rect.left + 'px'; overlay.style.top = rect.top + 'px';
    overlay.style.width = rect.width + 'px'; overlay.style.height = rect.height + 'px';
    const component = getReactComponent(el);
    const tag = el.tagName.toLowerCase();
    const cls = el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\\s+/)[0] : '';
    label.textContent = (component ? '<' + component + '> ' : '') + tag + cls;
    label.style.display = 'block';
    label.style.left = rect.left + 'px'; label.style.top = Math.max(0, rect.top - 22) + 'px';
  }
  function onClick(e) {
    if (popup.contains(e.target)) return;
    let el = currentEl;
    if (popupVisible) {
      const hit = document.elementFromPoint(e.clientX, e.clientY);
      if (hit && hit !== overlay && hit !== label && !popup.contains(hit)) el = hit;
    }
    if (!el) return;
    e.preventDefault(); e.stopPropagation();
    currentEl = el;
    window.__omniSelectedEl = el;
    const info = getElementInfo(el);
    console.log(__OMNI_SELECT + JSON.stringify(info));
    showPopup(el, info);
  }
  document.addEventListener('mousemove', onMouseMove, true);
  document.addEventListener('click', onClick, true);

  window.__omniDisableDesignMode = function() {
    document.removeEventListener('mousemove', onMouseMove, true);
    document.removeEventListener('click', onClick, true);
    if (resultTimer) { clearTimeout(resultTimer); resultTimer = null; }
    overlay.remove(); label.remove(); popup.remove();
    delete window.__omniDesignMode;
    delete window.__omniDisableDesignMode;
    delete window.__omniOnDesignResult;
  };
})();
`;
}

/**
 * Register every `omnigent:browser-*` IPC handler. Idempotent per process is
 * NOT guaranteed — call exactly once from main.js's registerIpc.
 *
 * @param {object} deps
 * @param {Electron.IpcMain} deps.ipcMain
 * @param {(event: Electron.IpcMainInvokeEvent) => boolean} deps.isPinnedOriginSender
 *        The privileged-origin trust gate. Load-bearing — applied to every handler.
 * @param {(event: Electron.IpcMainInvokeEvent) =>
 *          (import('./browserViewRegistry').Registry | null)} deps.getRegistryForEvent
 *        Resolves the sender window's own browser-view registry.
 */
function registerBrowserIpc({ ipcMain, isPinnedOriginSender, getRegistryForEvent }) {
  /**
   * Resolve the sender's registry after the privileged-origin gate. Returns
   * `{ registry }` on success or `{ error }` (a structured result, never a
   * throw) so the relay/toolbar surfaces a clean error.
   */
  const gateRegistry = (event) => {
    if (!isPinnedOriginSender(event)) {
      return { error: "browser IPC is only available to the connected server's page" };
    }
    const registry = getRegistryForEvent(event);
    if (!registry) return { error: "no browser registry for this window" };
    return { registry };
  };

  /** A window-scoped sender for the event's own webContents. Used to push
   *  url/nav-state pings back to exactly the renderer that drives the view. */
  const senderFor = (event) => (channel, payload) => {
    try {
      event.sender.send(channel, payload);
    } catch {
      /* window torn down */
    }
  };

  // Open (create-if-absent) or navigate a conversation's view, and measure it
  // into place. `force` reloads even on the same URL (agent "bring me back"
  // intent). Returns the registry's structured `{ ok, created, error }`.
  ipcMain.handle("omnigent:browser-open-or-navigate", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const { conversationId, url, bounds, opts } = args ?? {};
    if (typeof conversationId !== "string" || !conversationId) {
      return { ok: false, error: "conversationId is required" };
    }
    const r = g.registry.openOrNavigate(conversationId, url, bounds, opts);
    // On first creation, wire nav listeners here (not in the registry factory,
    // which stays Electron-free) so the URL bar can live-track the real url.
    if (r.ok && r.created && r.entry) {
      attachNavListeners({
        conversationId,
        webContents: r.entry.view.webContents,
        send: senderFor(event),
      });
    }
    // Strip the non-serializable `entry` before it crosses the IPC boundary.
    return { ok: r.ok, created: r.created ?? false, error: r.error };
  });

  // Attach the named conversation's view to the host window (detaching the
  // previous active one), or detach everything when conversationId is null.
  ipcMain.handle("omnigent:browser-set-active", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const conversationId = args?.conversationId ?? null;
    const r = g.registry.setActive(conversationId);
    return { ok: r.ok, error: r.error };
  });

  // Reposition the active conversation's view to freshly-measured bounds.
  ipcMain.handle("omnigent:browser-resize", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const { conversationId, bounds } = args ?? {};
    if (typeof conversationId !== "string" || !conversationId) {
      return { ok: false, error: "conversationId is required" };
    }
    const entry = g.registry.get(conversationId);
    if (!entry) return { ok: false, error: "No browser view" };
    if (bounds) entry.boundsController.setRendererBounds(bounds);
    return { ok: true };
  });

  // Capture the conversation's view as a base64 PNG.
  ipcMain.handle("omnigent:browser-screenshot", async (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const { conversationId } = args ?? {};
    const entry = g.registry.get(conversationId);
    if (!entry) return { ok: false, error: "No browser view" };
    try {
      const image = await entry.view.webContents.capturePage();
      const dataUrl = `data:image/png;base64,${image.toPNG().toString("base64")}`;
      return { ok: true, dataUrl };
    } catch (e) {
      return { ok: false, error: e && e.message ? e.message : String(e) };
    }
  });

  // Run relay-template JS in the conversation's view. PRIVATE to the relay's
  // fixed templates (snapshot / click / type) — NOT an agent-facing generic
  // `evaluate` (Risk-4 trust boundary; see README).
  ipcMain.handle("omnigent:browser-execute", async (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const { conversationId, js } = args ?? {};
    if (typeof js !== "string") return { ok: false, error: "js must be a string" };
    const entry = g.registry.get(conversationId);
    if (!entry) return { ok: false, error: "No browser view" };
    try {
      // `true` = user gesture, so the page can call gesture-gated APIs.
      const result = await entry.view.webContents.executeJavaScript(js, true);
      // Normalize to a string — the relay JSON.parses snapshot/upload results.
      return { ok: true, result: typeof result === "string" ? result : JSON.stringify(result) };
    } catch (e) {
      return { ok: false, error: e && e.message ? e.message : String(e) };
    }
  });

  // Whether a view currently exists for a conversation. Lets a (re)mounting
  // pane re-attach an already-created view without waiting for a create event.
  ipcMain.handle("omnigent:browser-has-view", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { exists: false };
    const { conversationId } = args ?? {};
    return { exists: typeof conversationId === "string" && g.registry.has(conversationId) };
  });

  // Destroy the conversation's view (explicit close — unmount only detaches).
  ipcMain.handle("omnigent:browser-close", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const { conversationId, reason } = args ?? {};
    const r = g.registry.close(conversationId, reason);
    return { ok: r.ok, removed: r.removed ?? false };
  });

  // ── Toolbar: history navigation ──────────────────────────────────────────
  // Back / forward / reload. Each returns fresh nav-state so the caller updates
  // button-disabled immediately without waiting for the did-navigate event.

  ipcMain.handle("omnigent:browser-go-back", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const entry = g.registry.get(args?.conversationId);
    if (!entry) return { ok: false, error: "No browser view" };
    goBack(entry.view.webContents);
    return { ok: true, ...readNavState(entry.view.webContents) };
  });

  ipcMain.handle("omnigent:browser-go-forward", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const entry = g.registry.get(args?.conversationId);
    if (!entry) return { ok: false, error: "No browser view" };
    goForward(entry.view.webContents);
    return { ok: true, ...readNavState(entry.view.webContents) };
  });

  ipcMain.handle("omnigent:browser-reload", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const entry = g.registry.get(args?.conversationId);
    if (!entry) return { ok: false, error: "No browser view" };
    try {
      entry.view.webContents.reload();
    } catch {
      /* destroyed */
    }
    return { ok: true };
  });

  // ── Toolbar: DevTools toggle ─────────────────────────────────────────────
  // Toggle DevTools docked 'bottom' — it shares the view's bounds, so the
  // syncBounds loop already covers it and Chromium splits page + devtools.
  ipcMain.handle("omnigent:open-browser-devtools", (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const entry = g.registry.get(args?.conversationId);
    if (!entry) return { ok: false, error: "No browser view" };
    try {
      const wc = entry.view.webContents;
      if (wc.isDevToolsOpened()) {
        wc.closeDevTools();
      } else {
        wc.openDevTools({ mode: "bottom" });
      }
      return { ok: true };
    } catch (e) {
      return { ok: false, error: e && e.message ? e.message : String(e) };
    }
  });

  // ── Design mode (point-and-prompt) ───────────────────────────────────────
  // Enable/disable the in-page picker and signal a submit result back to the
  // popup. Listeners are stored per-entry (and detached by the registry's
  // close()) so a late background-conversation marker can't leak into another UI.

  ipcMain.handle("omnigent:browser-enable-design-mode", async (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const { conversationId } = args ?? {};
    const entry = g.registry.get(conversationId);
    if (!entry) return { ok: false, error: "No browser view" };
    try {
      // Fresh per-enable nonce baked into the injected script's marker prefixes.
      const nonce = crypto.randomBytes(16).toString("hex");
      await entry.view.webContents.executeJavaScript(buildDesignModeScript(nonce));
      // Detach prior handlers so toggling on/off doesn't stack listeners.
      detachDesignModeListeners(entry);
      // Shared gesture state: input-event listener stamps the last native press;
      // the console handler requires a recent stamp before honoring a submit.
      const gestureState = { lastGestureAt: 0 };
      const consoleHandler = makeDesignModeConsoleHandler(
        conversationId,
        entry,
        senderFor(event),
        nonce,
        gestureState,
      );
      const inputHandler = makeDesignModeInputHandler(gestureState);
      entry.designModeListener = consoleHandler;
      entry.designModeInputListener = inputHandler;
      entry.designModeWebContents = entry.view.webContents;
      entry.designModeWebContents.on("console-message", consoleHandler);
      entry.designModeWebContents.on("input-event", inputHandler);
      return { ok: true };
    } catch (e) {
      const msg = e && e.message ? e.message : String(e);
      if (msg.includes("Object has been destroyed")) return { ok: false, error: "browser closed" };
      return { ok: false, error: msg };
    }
  });

  ipcMain.handle("omnigent:browser-disable-design-mode", async (event, args) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    const { conversationId } = args ?? {};
    const entry = g.registry.get(conversationId);
    if (!entry) return { ok: false };
    detachDesignModeListeners(entry);
    try {
      await entry.view.webContents.executeJavaScript(
        "window.__omniDisableDesignMode && window.__omniDisableDesignMode()",
      );
    } catch {
      /* destroyed */
    }
    return { ok: true };
  });

  // Forward a submit's result envelope into the page for green/red feedback.
  // `id` matches the page's submitId so a late callback can't paint over a fresh
  // popup. Fields are defensively coerced before crossing back into the page.
  ipcMain.handle("omnigent:browser-signal-design-result", async (event, payload) => {
    const g = gateRegistry(event);
    if (g.error) return { ok: false, error: g.error };
    if (!payload || typeof payload !== "object") return { ok: false, error: "bad payload" };
    const entry = g.registry.get(payload.conversationId);
    if (!entry) return { ok: false, error: "No browser view" };
    const safe = {
      id: typeof payload.id === "number" ? payload.id : 0,
      ok: !!payload.ok,
      message: typeof payload.message === "string" ? payload.message : "",
    };
    try {
      await entry.view.webContents.executeJavaScript(
        `window.__omniOnDesignResult && window.__omniOnDesignResult(${JSON.stringify(safe)})`,
      );
      return { ok: true };
    } catch (e) {
      const msg = e && e.message ? e.message : String(e);
      if (msg.includes("Object has been destroyed")) return { ok: false, error: "browser closed" };
      return { ok: false, error: msg };
    }
  });
}

module.exports = {
  registerBrowserIpc,
  // Exported for unit tests (drive nav-state / listener logic without Electron).
  attachNavListeners,
  readNavState,
  goBack,
  goForward,
  // Design-mode security surface (nonce-gated console handler + native-gesture
  // tracker), exported so tests can drive the forge/gesture logic directly.
  makeDesignModeConsoleHandler,
  makeDesignModeInputHandler,
  buildDesignModeScript,
  DESIGN_MODE_GESTURE_WINDOW_MS,
};
