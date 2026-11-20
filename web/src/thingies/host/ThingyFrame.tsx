import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { SelectionPopupActions } from "@/components/chat/SelectionPopup";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { getOmnigentThingyFrame } from "@/lib/host";
import { randomUUID } from "@/lib/randomUUID";
import { useNavigate, useRebasePath } from "@/lib/routing";
import { requestThingyRailTab } from "@/lib/thingyRailTabs";

import type {
  ThingyActionRequest,
  ThingyBridge,
  ThingyPageRequest,
  ThingySelection,
} from "./thingyBridge";
import { BridgeError, startThingyBridge } from "./thingyBridge";
import {
  composeThingyContextPrompt,
  thingyBoardNewSessionUrl,
  thingyNewAutomationUrl,
  thingyNewSessionUrl,
  thingySessionUrl,
} from "./thingyPages";
import { createPreviewRpc } from "./previewRpc";
import type { ThingyGestureGate } from "./thingyGestureGate";
import { createThingyGestureGate } from "./thingyGestureGate";
import {
  quoteInThingySession,
  requestThingyAction,
  thingyRailKey,
  thingySelectionQuote,
} from "./thingySession";
import type { Thingy, ThingyContent } from "../api/thingyApi";
import { callThingyRpc, getThingyActionProgress, listViewerSessions } from "../api/thingyApi";
import type {
  ThingyCapability,
  ThingyInitContext,
  ThingyMethod,
  ThingySessionListRequest,
} from "../sdk/thingyProtocol";
import {
  THINGY_CONNECT_MESSAGE_TYPE,
  THINGY_HELLO_MESSAGE_TYPE,
  THINGY_LIMITS,
  THINGY_PROTOCOL_VERSION,
} from "../sdk/thingyProtocol";
import { buildOmnigentSdkBootstrapScript } from "../sdk/omnigentSdkRuntimeSource";

/**
 * Policy for the uploaded document. Inline script and style only (the thingy is one
 * self-contained HTML file); no network, nested frames, forms, plugins, or workers.
 */
export const THINGY_CONTENT_SECURITY_POLICY = [
  "default-src 'none'",
  "script-src 'unsafe-inline'",
  "style-src 'unsafe-inline'",
  "img-src data: blob:",
  "font-src data:",
  "media-src 'none'",
  "connect-src 'none'",
  "frame-src 'none'",
  "child-src 'none'",
  "worker-src 'none'",
  "object-src 'none'",
  "form-action 'none'",
  "manifest-src 'none'",
  "base-uri 'none'",
].join("; ");

/** Scripts only: an opaque origin, no forms, pop-ups, top navigation, or same-origin access. */
export const THINGY_FRAME_SANDBOX = "allow-scripts";

const LEADING_DOCTYPE = /^\s*<!doctype[^>]*>/i;

/**
 * The frame's document: the CSP meta tag and the SDK bootstrap ahead of the thingy's HTML, after
 * its doctype so the page keeps standards mode. A policy applies only to content after it, so
 * both come first. DNS prefetching is off too, since CSP does not cover it.
 *
 * Known limit: CSP cannot stop the frame from navigating itself, which sends one request. The
 * host notices the new document, closes its port and stops the frame after a few loads.
 */
export function buildThingyFrameDocument(html: string, bootstrapScript: string): string {
  const doctype = LEADING_DOCTYPE.exec(html)?.[0].trim() ?? "<!DOCTYPE html>";
  const body = html.replace(LEADING_DOCTYPE, "");
  const policy = THINGY_CONTENT_SECURITY_POLICY.replace(/"/g, "&quot;");
  return `${doctype}<meta http-equiv="Content-Security-Policy" content="${policy}"><meta http-equiv="x-dns-prefetch-control" content="off"><script>${bootstrapScript}</script>\n${body}`;
}

const KNOWN_CAPABILITIES: ReadonlySet<string> = new Set<ThingyCapability>([
  "data.read",
  "data.write",
  "sessions.read",
  "sessions.create",
]);

function grantedCapabilities(content: ThingyContent): ThingyCapability[] {
  return content.capabilities.filter((capability): capability is ThingyCapability =>
    KNOWN_CAPABILITIES.has(capability),
  );
}

function randomNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function isHelloFor(data: unknown, frameId: string, nonce: string): boolean {
  return (
    typeof data === "object" &&
    data !== null &&
    "type" in data &&
    data.type === THINGY_HELLO_MESSAGE_TYPE &&
    "frameId" in data &&
    data.frameId === frameId &&
    "nonce" in data &&
    data.nonce === nonce
  );
}

// The selection popup's buttons sit centered above the point; keep them over the frame.
const POPUP_HALF_WIDTH = 160;
const POPUP_HEIGHT = 40;

/** Where the selection popup goes: above the selection, kept within the frame's area. */
export function selectionPopupPosition(
  area: { left: number; top: number; right: number; bottom: number },
  rect: ThingySelection["rect"],
): { x: number; y: number } {
  const clamp = (value: number, min: number, max: number) =>
    min > max ? (min + max) / 2 : Math.min(Math.max(value, min), max);
  return {
    x: clamp(
      area.left + rect.x + rect.width / 2,
      area.left + POPUP_HALF_WIDTH,
      area.right - POPUP_HALF_WIDTH,
    ),
    y: clamp(area.top + rect.y, area.top + POPUP_HEIGHT, area.bottom),
  };
}

export interface ThingyFrameProps {
  app: Thingy;
  /** Published content. Remount the frame (by `key`) when the revision changes. */
  content: ThingyContent;
  onRevisionChanged: () => void;
  /** A template preview: data lives in memory, and actions, navigation and selections are off. */
  preview?: boolean;
  /**
   * The session the thingy shows beside, in that session's side panel. Its actions and quotes go
   * there; without it they go to the viewer's thingy session.
   */
  sessionId?: string;
  /** Adds Add comment to a selection's buttons. */
  onAddComment?: (selection: ThingySelection) => void;
}

interface PendingConfirmation {
  resolve: (allowed: boolean) => void;
}

interface FrameMount {
  frameId: string;
  nonce: string;
}

function newMount(): FrameMount {
  return { frameId: `omnigent-thingy-${randomUUID()}`, nonce: randomNonce() };
}

/**
 * Runs a thingy's published HTML and answers its SDK calls. The default frame is a sandboxed
 * `srcdoc` iframe with an opaque origin and a CSP that blocks the network; a host can supply its
 * own frame (`OmnigentHostConfig.thingyFrame`). Each frame document gets one MessagePort, handed
 * over only after a hello carrying this mount's nonce from the frame's own window; the bridge
 * checks every call against the thingy's capabilities and the viewer's permissions, and the
 * server checks them again.
 */
export function ThingyFrame({
  app,
  content,
  onRevisionChanged,
  preview = false,
  sessionId,
  onAddComment,
}: ThingyFrameProps) {
  const HostFrame = getOmnigentThingyFrame();
  const previewRpc = useMemo(() => createPreviewRpc(), []);
  const colorScheme = useResolvedThemeMode();
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const frameWindowRef = useRef<Window | null>(null);
  const pendingHelloRef = useRef<unknown>(undefined);
  const bridgeRef = useRef<ThingyBridge | null>(null);
  // A document gets one port. A later load is a reload or a navigation away, which gets a fresh
  // document and nonce, so a page the frame navigated to can never take over the port.
  const handshakeDoneRef = useRef(false);
  const loadsRef = useRef(0);
  const remountsRef = useRef(0);
  const [mount, setMount] = useState(newMount);
  const [confirmation, setConfirmation] = useState<PendingConfirmation | null>(null);
  const confirmationRef = useRef<PendingConfirmation | null>(null);
  // The viewer's answer to letting this mount read their sessions, asked once per page load.
  const [sessionsAccess, setSessionsAccess] = useState<PendingConfirmation | null>(null);
  const sessionsAccessRef = useRef<Promise<boolean> | null>(null);
  const [closedReason, setClosedReason] = useState<"protocol_violations" | "navigated" | null>(
    null,
  );

  // Read through refs so a re-render (for example a theme toggle) does not tear down the port.
  const latestRef = useRef({ app, content, onRevisionChanged, colorScheme, sessionId });
  latestRef.current = { app, content, onRevisionChanged, colorScheme, sessionId };

  const htmlContent = useMemo(
    () => buildThingyFrameDocument(content.html, buildOmnigentSdkBootstrapScript(mount)),
    [content.html, mount],
  );

  const confirmSessionCreate = useCallback(
    () =>
      new Promise<boolean>((resolve) => {
        if (confirmationRef.current !== null) {
          resolve(false);
          return;
        }
        const pending = { resolve };
        confirmationRef.current = pending;
        setConfirmation(pending);
      }),
    [],
  );

  // Actions and navigation need a gesture inside the thingy (see `thingyGestureGate`).
  const frameHostRef = useRef<HTMLDivElement>(null);
  const gateRef = useRef<ThingyGestureGate | null>(null);
  useEffect(() => {
    const frameHost = frameHostRef.current;
    if (!frameHost) {
      return undefined;
    }
    const gate = createThingyGestureGate({ frameHost });
    gateRef.current = gate;
    return () => {
      gate.dispose();
      gateRef.current = null;
    };
  }, []);
  const claimGesture = useCallback(() => gateRef.current?.claim() ?? false, []);

  const navigate = useNavigate();
  const rebasePath = useRebasePath();
  const navigationRef = useRef({ navigate, rebasePath });
  navigationRef.current = { navigate, rebasePath };
  const goTo = useCallback((path: string) => navigationRef.current.navigate(path), []);

  const openPage = useCallback(
    (request: ThingyPageRequest) => {
      // Without a click inside the thingy, the frame cannot open or replace pages.
      if (!claimGesture()) {
        return false;
      }
      const current = { id: latestRef.current.app.id, name: latestRef.current.app.name };
      let path: string;
      switch (request.kind) {
        case "newSession": {
          const prompt = request.prompt ? composeThingyContextPrompt(request.prompt, current) : "";
          path =
            request.display === "panel"
              ? thingyBoardNewSessionUrl(current.id, prompt)
              : thingyNewSessionUrl(prompt);
          break;
        }
        case "newAutomation":
          path = thingyNewAutomationUrl({
            name: request.name,
            prompt: composeThingyContextPrompt(request.prompt, current),
            schedule: request.schedule,
          });
          break;
        case "openSession":
          path = thingySessionUrl(request.sessionId);
          if (request.display === "panel") {
            // The session opens with this thingy in its side panel.
            requestThingyRailTab(request.sessionId, thingyRailKey(current.id));
          }
          break;
      }
      if (request.display === "tab") {
        const url = new URL(navigationRef.current.rebasePath(path), window.location.origin);
        window.open(url.href, "_blank", "noopener,noreferrer");
      } else {
        navigationRef.current.navigate(path);
      }
      return true;
    },
    [claimGesture],
  );

  const requestAction = useCallback(
    async (request: ThingyActionRequest) => {
      if (!claimGesture()) {
        throw new BridgeError(
          "PERMISSION_DENIED",
          "Omnigent runs an action only right after a click in the thingy.",
        );
      }
      return requestThingyAction(latestRef.current.app, request, goTo, latestRef.current.sessionId);
    },
    [claimGesture, goTo],
  );

  const [selection, setSelection] = useState<ThingySelection | null>(null);
  // Only a selection made in the thingy (focus is in it) gets the popup, so the frame cannot show
  // it over Omnigent while the viewer works elsewhere.
  const showSelection = useCallback((next: ThingySelection | null) => {
    if (next === null || gateRef.current?.frameFocused()) {
      setSelection(next);
    }
  }, []);
  // A click anywhere in the page, including the popup's own buttons after they act, ends it.
  useEffect(() => {
    if (selection === null) {
      return undefined;
    }
    const clear = () => setSelection(null);
    document.addEventListener("click", clear);
    window.addEventListener("resize", clear);
    return () => {
      document.removeEventListener("click", clear);
      window.removeEventListener("resize", clear);
    };
  }, [selection]);
  const quoteSelection = useCallback(
    (kind: "reply" | "side_chat") => {
      if (selection === null) {
        return;
      }
      const current = latestRef.current.app;
      void quoteInThingySession(
        current,
        kind,
        thingySelectionQuote(current, selection),
        goTo,
        latestRef.current.sessionId,
      ).catch(() => undefined);
    },
    [goTo, selection],
  );
  const popupPosition = useMemo(() => {
    const area = frameHostRef.current?.getBoundingClientRect();
    if (selection === null || !area) {
      return null;
    }
    return selectionPopupPosition(area, selection.rect);
  }, [selection]);

  const actionProgress = useCallback(async (actionIds: string[], signal: AbortSignal) => {
    const items = await getThingyActionProgress(latestRef.current.app.id, actionIds, signal);
    return items.map((item) => ({ ...item }));
  }, []);

  const listSessions = useCallback(
    async (request: ThingySessionListRequest, signal: AbortSignal) => {
      // A thingy that can read sessions and write shared data could copy the viewer's session
      // titles where its author can read them, so the viewer decides first.
      sessionsAccessRef.current ??= new Promise<boolean>((resolve) => {
        setSessionsAccess({ resolve });
      });
      if (!(await sessionsAccessRef.current)) {
        return null;
      }
      return listViewerSessions(request, signal);
    },
    [],
  );

  const settleSessionsAccess = useCallback((allowed: boolean) => {
    setSessionsAccess((pending) => {
      pending?.resolve(allowed);
      return null;
    });
  }, []);

  const settleConfirmation = useCallback((allowed: boolean) => {
    confirmationRef.current?.resolve(allowed);
    confirmationRef.current = null;
    setConfirmation(null);
  }, []);

  const connect = useCallback(
    (frameWindow: Window) => {
      handshakeDoneRef.current = true;
      bridgeRef.current?.dispose();
      const latest = latestRef.current;
      const context: ThingyInitContext = {
        protocol: THINGY_PROTOCOL_VERSION,
        app: { id: latest.app.id, name: latest.app.name },
        revisionId: latest.content.revision_id,
        // Prefer the stable principal ID; fall back to the viewer's name for servers without it.
        viewer: preview
          ? { id: "preview", displayName: "Preview" }
          : {
              id: latest.app.viewer_principal_id ?? latest.app.viewer_id,
              displayName: latest.app.viewer_id,
            },
        capabilities: grantedCapabilities(latest.content),
        permissions: {
          writeData: latest.app.permissions.write_data,
          publish: latest.app.permissions.publish,
        },
        colorScheme: latest.colorScheme === "dark" ? "dark" : "light",
        ...(preview ? { preview: true } : {}),
      };
      const channel = new MessageChannel();
      bridgeRef.current = startThingyBridge({
        port: channel.port1,
        context,
        callRpc: (method: ThingyMethod, params: Record<string, unknown>, signal: AbortSignal) =>
          preview
            ? previewRpc(method, params)
            : callThingyRpc(
                latest.app.id,
                {
                  protocol: 1,
                  request_id: randomUUID(),
                  revision_id: latest.content.revision_id,
                  method,
                  params,
                },
                signal,
              ),
        confirmSessionCreate: preview ? undefined : confirmSessionCreate,
        openPage: preview ? undefined : openPage,
        requestAction: preview ? undefined : requestAction,
        // A preview has no actions to follow.
        actionProgress: preview ? async () => [] : actionProgress,
        onSelection: preview ? undefined : showSelection,
        listSessions: preview ? undefined : listSessions,
        onRevisionChanged: () => latestRef.current.onRevisionChanged(),
        onClosed: (reason) => {
          if (reason === "protocol_violations") {
            setClosedReason("protocol_violations");
          }
        },
      });
      frameWindow.postMessage(
        { type: THINGY_CONNECT_MESSAGE_TYPE, frameId: mount.frameId, nonce: mount.nonce },
        // The frame's origin is opaque; the nonce and the transferred port are the boundary.
        "*",
        [channel.port2],
      );
    },
    [
      actionProgress,
      confirmSessionCreate,
      listSessions,
      mount,
      openPage,
      preview,
      previewRpc,
      requestAction,
      showSelection,
    ],
  );

  const handleFrameMessage = useCallback(
    (data: unknown) => {
      if (handshakeDoneRef.current || !isHelloFor(data, mount.frameId, mount.nonce)) {
        return;
      }
      const frameWindow = frameWindowRef.current;
      if (!frameWindow) {
        pendingHelloRef.current = data;
        return;
      }
      pendingHelloRef.current = undefined;
      connect(frameWindow);
    },
    [connect, mount],
  );

  const handleFrameLoad = useCallback(
    (frameWindow: Window) => {
      frameWindowRef.current = frameWindow;
      loadsRef.current += 1;
      if (loadsRef.current > 1) {
        // The document reloaded or navigated away; its port is gone with it.
        bridgeRef.current?.dispose();
        bridgeRef.current = null;
        if (remountsRef.current + 1 >= THINGY_LIMITS.maxHandshakesPerMount) {
          setClosedReason("navigated");
          return;
        }
        remountsRef.current += 1;
        setMount(newMount());
        return;
      }
      if (pendingHelloRef.current !== undefined) {
        handleFrameMessage(pendingHelloRef.current);
      }
    },
    [handleFrameMessage],
  );

  // A new mount is a new document: start its handshake from scratch.
  useEffect(() => {
    handshakeDoneRef.current = false;
    loadsRef.current = 0;
    frameWindowRef.current = HostFrame ? null : (iframeRef.current?.contentWindow ?? null);
    pendingHelloRef.current = undefined;
  }, [HostFrame, mount]);

  // The default frame's messages; a host frame reports its own through `onMessage`.
  useEffect(() => {
    if (HostFrame) {
      return undefined;
    }
    const onMessage = (event: MessageEvent) => {
      const frameWindow = iframeRef.current?.contentWindow;
      if (!frameWindow || event.source !== frameWindow) {
        return;
      }
      frameWindowRef.current = frameWindow;
      handleFrameMessage(event.data);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [HostFrame, handleFrameMessage]);

  useEffect(
    () => () => {
      frameWindowRef.current = null;
      pendingHelloRef.current = undefined;
      bridgeRef.current?.dispose();
      bridgeRef.current = null;
      confirmationRef.current?.resolve(false);
      confirmationRef.current = null;
    },
    [],
  );

  const title = `Thingy: ${app.name}`;
  return (
    <div className="relative flex min-h-[480px] flex-1 flex-col">
      {popupPosition && (
        <SelectionPopupActions
          position={popupPosition}
          onReply={() => quoteSelection("reply")}
          onAskInSideChat={() => quoteSelection("side_chat")}
          onAddComment={onAddComment && selection ? () => onAddComment(selection) : undefined}
        />
      )}
      {closedReason !== null && (
        <Alert>
          <AlertTitle>Omnigent stopped answering this thingy</AlertTitle>
          <AlertDescription>
            {closedReason === "protocol_violations"
              ? "It sent too many invalid requests. Reload the page to try again."
              : "It kept reloading or tried to leave its page. Reload the page to try again."}
          </AlertDescription>
        </Alert>
      )}
      {/* Focus moves here after an action or navigation, so the next one needs a new click. */}
      <div ref={frameHostRef} tabIndex={-1} className="flex min-h-0 flex-1 flex-col outline-none">
        {closedReason !== "navigated" &&
          (HostFrame ? (
            <HostFrame
              key={mount.nonce}
              htmlContent={htmlContent}
              contentSecurityPolicy={THINGY_CONTENT_SECURITY_POLICY}
              title={title}
              onMessage={handleFrameMessage}
              onLoad={handleFrameLoad}
            />
          ) : (
            <iframe
              key={mount.nonce}
              ref={iframeRef}
              id={mount.frameId}
              title={title}
              sandbox={THINGY_FRAME_SANDBOX}
              allow=""
              referrerPolicy="no-referrer"
              srcDoc={htmlContent}
              onLoad={(event) => {
                const frameWindow = event.currentTarget.contentWindow;
                if (frameWindow) {
                  handleFrameLoad(frameWindow);
                }
              }}
              // The thingy themes itself from `client.ui.colorScheme`.
              className="min-h-0 w-full flex-1 border-0 bg-background"
            />
          ))}
      </div>
      <Dialog
        open={confirmation !== null}
        onOpenChange={(open) => !open && settleConfirmation(false)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Start an Omnigent session?</DialogTitle>
            <DialogDescription>
              {app.name} wants to start a new Omnigent session as you.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => settleConfirmation(false)}>
              Cancel
            </Button>
            <Button onClick={() => settleConfirmation(true)}>Start session</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog
        open={sessionsAccess !== null}
        onOpenChange={(open) => !open && settleSessionsAccess(false)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Let this thingy see your sessions?</DialogTitle>
            <DialogDescription>
              {app.name} wants to see the titles and status of your Omnigent sessions while this
              page is open. It could save them where other people with access to this thingy can see
              them.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => settleSessionsAccess(false)}>
              Don’t allow
            </Button>
            <Button onClick={() => settleSessionsAccess(true)}>Allow</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
