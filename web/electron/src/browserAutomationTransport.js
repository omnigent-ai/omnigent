"use strict";

// Puppeteer's public ExtensionTransport uses the same virtual browser/tab/page
// topology to automate one debugger target without a browser-wide endpoint.
const TAB_TARGET_ID = "agent-tab";
const PAGE_TARGET_ID = "agent-page";
const TAB_SESSION_ID = "agent-tab-session";
const PAGE_SESSION_ID = "agent-page-session";
const CHILD_TARGET_TYPES = new Set(["iframe", "worker"]);

/**
 * Connect Puppeteer to exactly one Electron WebContents. Native tab creation,
 * closing and policy stay with the caller. `beforeSend` may reject a command
 * before it reaches the page; the caller releases held input before close().
 *
 * @param {object} webContents
 * @param {{ deadlineAt?: number, signal?: AbortSignal, debuggerClient?: object,
 *   beforeSend?: (method: string, params: object | undefined) => void,
 *   onClose?: (error?: Error) => void }} [options]
 */
async function createBrowserAutomationTransport(
  webContents,
  { deadlineAt, signal, beforeSend, onClose, debuggerClient = webContents.debugger } = {},
) {
  const debuggerApi = debuggerClient;
  const expired = () => Number.isFinite(deadlineAt) && Date.now() >= deadlineAt;
  const timeoutError = () => new Error("browser action expired before the page ran it");
  if (signal?.aborted) throw signal.reason ?? new Error("browser action cancelled");
  if (expired()) throw timeoutError();
  const attachedHere = !debuggerApi.isAttached();
  if (attachedHere) debuggerApi.attach("1.3");

  let closed = false;
  let deadlineTimer;
  let discovered = false;
  const attached = new Set();
  const children = new Map(); // native session id -> { parent, targetInfo }
  const automaticSessions = new Set();
  const documentScripts = new Map(); // native session id -> registrations owned by this connection
  const removeDocumentScript = (identifier, sessionId) => {
    try {
      webContents.debugger
        .sendCommand("Page.removeScriptToEvaluateOnNewDocument", { identifier }, sessionId)
        .catch(() => {});
    } catch {
      /* the debugger or its child has already gone away */
    }
  };

  const targetInfo = (targetId) => ({
    targetId,
    type: targetId === TAB_TARGET_ID ? "tab" : "page",
    title: webContents.getTitle?.() ?? "",
    url: webContents.getURL?.() || "about:blank",
    attached: false,
    canAccessOpener: false,
  });
  const dispatch = (message) => {
    queueMicrotask(() => {
      if (!closed) transport.onmessage?.(JSON.stringify(message));
    });
  };
  const forgetSession = (sessionId) => {
    for (const [child, info] of children) {
      if (info.parent === sessionId) forgetSession(child);
    }
    children.delete(sessionId);
  };
  const close = (error) => {
    if (closed) return;
    closed = true;
    clearTimeout(deadlineTimer);
    signal?.removeEventListener("abort", onAbort);
    debuggerApi.removeListener("message", onMessage);
    debuggerApi.removeListener("detach", onDetach);
    webContents.removeListener("destroyed", onDestroyed);
    for (const [sessionId, identifiers] of documentScripts) {
      for (const identifier of identifiers) removeDocumentScript(identifier, sessionId);
    }
    documentScripts.clear();
    // The caller can keep a borrowed debugger attached after this action.
    // Undo auto-attach now so later children cannot pause without a listener.
    for (const sessionId of [...automaticSessions].reverse()) {
      try {
        webContents.debugger
          .sendCommand(
            "Target.setAutoAttach",
            {
              autoAttach: false,
              waitForDebuggerOnStart: false,
              flatten: true,
            },
            sessionId === PAGE_SESSION_ID ? undefined : sessionId,
          )
          .catch(() => {});
      } catch {
        /* the debugger or its child has already gone away */
      }
    }
    automaticSessions.clear();
    children.clear();
    try {
      if (attachedHere && debuggerApi.isAttached()) debuggerApi.detach();
    } catch {
      /* the view has already gone away */
    }
    try {
      onClose?.(error);
    } finally {
      transport.onclose?.();
    }
  };
  const onAbort = () => close(signal.reason ?? new Error("browser action cancelled"));
  const onDetach = () => close(new Error("browser debugger detached"));
  const onDestroyed = () => close(new Error("browser view closed"));
  const onMessage = (_event, method, params, nativeSessionId) => {
    if (closed || (nativeSessionId && !children.has(nativeSessionId))) return;
    const sessionId = nativeSessionId || PAGE_SESSION_ID;
    if (method === "Target.attachedToTarget") {
      if (!CHILD_TARGET_TYPES.has(params?.targetInfo?.type)) return;
      children.set(params.sessionId, { parent: sessionId, targetInfo: params.targetInfo });
    } else if (method === "Target.detachedFromTarget") {
      if (!children.has(params?.sessionId)) return;
      forgetSession(params.sessionId);
    } else if (method.startsWith("Target.target")) {
      const targetId = params?.targetInfo?.targetId ?? params?.targetId;
      if (![...children.values()].some((child) => child.targetInfo.targetId === targetId)) return;
    }
    dispatch({ method, params, sessionId });
  };

  const transport = {
    pageTargetId: PAGE_TARGET_ID,
    onmessage: undefined,
    onclose: undefined,
    send(message) {
      if (closed) throw new Error("browser automation transport is closed");
      if (expired()) {
        close(timeoutError());
        return;
      }
      const request = JSON.parse(message);
      const { id, method, sessionId } = request;
      const reply = (result) => dispatch({ id, sessionId, result });
      const fail = (error) =>
        dispatch({
          id,
          sessionId,
          error: { code: -32000, message: error.message ?? String(error) },
        });
      try {
        beforeSend?.(method, request.params);
        if (closed) return;
        if (method === "Browser.getVersion") {
          reply({
            protocolVersion: "1.3",
            product: `Chrome/${process.versions.chrome ?? "unknown"}`,
            revision: "unknown",
            userAgent: "Electron",
            jsVersion: process.versions.v8,
          });
          return;
        }
        if (method === "Target.getBrowserContexts") {
          reply({ browserContextIds: [] });
          return;
        }
        if (method === "Target.getTargets") {
          reply({ targetInfos: [targetInfo(TAB_TARGET_ID), targetInfo(PAGE_TARGET_ID)] });
          return;
        }
        if (method === "Target.setDiscoverTargets") {
          if (request.params?.discover && !discovered) {
            discovered = true;
            for (const targetId of [TAB_TARGET_ID, PAGE_TARGET_ID]) {
              dispatch({
                method: "Target.targetCreated",
                params: { targetInfo: targetInfo(targetId) },
              });
            }
          }
          reply({});
          return;
        }
        if (method === "Target.setAutoAttach" && (!sessionId || sessionId === TAB_SESSION_ID)) {
          if (request.params?.autoAttach && !attached.has(sessionId)) {
            attached.add(sessionId);
            dispatch({
              method: "Target.attachedToTarget",
              ...(sessionId ? { sessionId } : {}),
              params: {
                sessionId: sessionId ? PAGE_SESSION_ID : TAB_SESSION_ID,
                targetInfo: targetInfo(sessionId ? PAGE_TARGET_ID : TAB_TARGET_ID),
                waitingForDebugger: false,
              },
            });
          }
          reply({});
          return;
        }
        if (method === "Runtime.runIfWaitingForDebugger" && sessionId === TAB_SESSION_ID) {
          reply({});
          return;
        }
        if (
          ["Target.getTargetInfo", "Target.activateTarget", "Target.attachToTarget"].includes(
            method,
          )
        ) {
          const targetId = request.params?.targetId ?? PAGE_TARGET_ID;
          if ([TAB_TARGET_ID, PAGE_TARGET_ID].includes(targetId)) {
            if (method === "Target.getTargetInfo") reply({ targetInfo: targetInfo(targetId) });
            else if (method === "Target.activateTarget") reply({});
            else
              reply({ sessionId: targetId === TAB_TARGET_ID ? TAB_SESSION_ID : PAGE_SESSION_ID });
            return;
          }
          if (![...children.values()].some((child) => child.targetInfo.targetId === targetId)) {
            throw new Error("browser target is outside this view");
          }
        }
        if (method === "Target.detachFromTarget") {
          const detached = request.params?.sessionId;
          if ([TAB_SESSION_ID, PAGE_SESSION_ID].includes(detached)) {
            reply({});
            return;
          }
          if (!children.has(detached)) throw new Error("browser session is outside this view");
        }
        const scopedTargetCommand = [
          "Target.attachToTarget",
          "Target.detachFromTarget",
          "Target.getTargetInfo",
          "Target.activateTarget",
        ].includes(method);
        if (
          method.startsWith("Browser.") ||
          (!sessionId && !scopedTargetCommand) ||
          sessionId === TAB_SESSION_ID
        ) {
          throw new Error("browser-wide commands are unavailable for this view");
        }
        if (sessionId && sessionId !== PAGE_SESSION_ID && !children.has(sessionId)) {
          throw new Error("browser session is outside this view");
        }
        if (
          method.startsWith("Target.") &&
          ![
            "Target.setAutoAttach",
            "Target.attachToTarget",
            "Target.detachFromTarget",
            "Target.getTargetInfo",
            "Target.activateTarget",
          ].includes(method)
        ) {
          throw new Error("browser target management belongs to the desktop app");
        }
        const params =
          method === "Target.setAutoAttach"
            ? {
                ...request.params,
                filter: [
                  { type: "iframe", exclude: false },
                  { type: "worker", exclude: false },
                  { exclude: true },
                ],
              }
            : request.params;
        if (method === "Target.setAutoAttach") {
          if (params.autoAttach) automaticSessions.add(sessionId);
          else automaticSessions.delete(sessionId);
        }
        const nativeSessionId = sessionId === PAGE_SESSION_ID ? undefined : sessionId;
        debuggerApi.sendCommand(method, params, nativeSessionId).then((result) => {
          if (
            method === "Page.addScriptToEvaluateOnNewDocument" &&
            typeof result?.identifier === "string"
          ) {
            if (closed) removeDocumentScript(result.identifier, nativeSessionId);
            else {
              const identifiers = documentScripts.get(nativeSessionId) ?? new Set();
              identifiers.add(result.identifier);
              documentScripts.set(nativeSessionId, identifiers);
            }
          } else if (method === "Page.removeScriptToEvaluateOnNewDocument") {
            documentScripts.get(nativeSessionId)?.delete(params.identifier);
          }
          if (closed) return;
          if (method === "Target.attachToTarget" && result?.sessionId) {
            const info = [...children.values()].find(
              (child) => child.targetInfo.targetId === params.targetId,
            );
            if (info)
              children.set(result.sessionId, { ...info, parent: sessionId || PAGE_SESSION_ID });
          }
          reply(result ?? {});
        }, fail);
      } catch (error) {
        fail(error);
      }
    },
    close() {
      close();
    },
  };
  debuggerApi.on("message", onMessage);
  debuggerApi.on("detach", onDetach);
  webContents.on("destroyed", onDestroyed);
  signal?.addEventListener("abort", onAbort, { once: true });
  if (signal?.aborted) onAbort();
  if (!closed && Number.isFinite(deadlineAt)) {
    deadlineTimer = setTimeout(() => close(timeoutError()), Math.max(0, deadlineAt - Date.now()));
    deadlineTimer.unref?.();
  }
  return transport;
}

module.exports = { createBrowserAutomationTransport };
