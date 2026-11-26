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
import { withBasePath } from "@/lib/basePath";
import { getOmnigentGadgetFrame } from "@/lib/host";
import { randomUUID } from "@/lib/randomUUID";
import { useNavigate, useRebasePath } from "@/lib/routing";
import {
  noteGadgetFrameConnected,
  requestGadgetCreateSessionMenu,
  type GadgetPromptReview,
  gadgetFillsScreen,
} from "@/lib/gadgetRailTabs";

import type {
  GadgetActionRequest,
  GadgetBridge,
  GadgetPageRequest,
  GadgetSelection,
} from "./gadgetBridge";
import { BridgeError, startGadgetBridge } from "./gadgetBridge";
import {
  composeGadgetContextPrompt,
  gadgetBoardNewSessionUrl,
  gadgetNewAutomationUrl,
  gadgetNewSessionUrl,
  gadgetSessionUrl,
} from "./gadgetPages";
import { createPreviewRpc } from "./previewRpc";
import type { GadgetGestureGate } from "./gadgetGestureGate";
import { createGadgetGestureGate } from "./gadgetGestureGate";
import {
  gadgetSessionStarting,
  onGadgetSessionStarting,
  openGadgetInSession,
  quoteInGadgetSession,
  requestGadgetAction,
  gadgetSelectionQuote,
} from "./gadgetSession";
import type { Gadget, GadgetContent } from "../api/gadgetApi";
import { callGadgetRpc, getGadgetActionProgress, listViewerSessions } from "../api/gadgetApi";
import type {
  GadgetCapability,
  GadgetInitContext,
  GadgetMethod,
  GadgetSessionListRequest,
} from "../sdk/gadgetProtocol";
import {
  GADGET_CONNECT_MESSAGE_TYPE,
  GADGET_HELLO_MESSAGE_TYPE,
  GADGET_PROTOCOL_VERSION,
} from "../sdk/gadgetProtocol";
import { buildOmnigentSdkBootstrapScript } from "../sdk/omnigentSdkRuntimeSource";

/**
 * Policy for the uploaded document. Inline script and style only (the gadget is one
 * self-contained HTML file); no network requests, nested frames, forms, plugins, or workers.
 * No `webrtc` directive: browsers that don't know it log a warning on every load, and the
 * ones that do still leave peer connections open in places (designs/GADGETS.md).
 * It can't stop the document navigating itself, so gadget code can still send out what it
 * reads (designs/GADGETS.md, "What gadget code can send out").
 */
export const GADGET_CONTENT_SECURITY_POLICY = [
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
export const GADGET_FRAME_SANDBOX = "allow-scripts";

const LEADING_DOCTYPE = /^\s*<!doctype[^>]*>/i;

/**
 * The frame's document: the CSP and SDK bootstrap precede the gadget's content (after its
 * doctype, for standards mode), and DNS prefetching is off. A later frame navigation closes the
 * bridge (`handleFrameLoad`).
 */
export function buildGadgetFrameDocument(html: string, bootstrapScript: string): string {
  const doctype = LEADING_DOCTYPE.exec(html)?.[0].trim() ?? "<!DOCTYPE html>";
  const body = html.replace(LEADING_DOCTYPE, "");
  const policy = GADGET_CONTENT_SECURITY_POLICY.replace(/"/g, "&quot;");
  return `${doctype}<meta http-equiv="Content-Security-Policy" content="${policy}"><meta http-equiv="x-dns-prefetch-control" content="off"><script>${bootstrapScript}</script>\n${body}`;
}

const KNOWN_CAPABILITIES: ReadonlySet<string> = new Set<GadgetCapability>([
  "data.read",
  "data.write",
  "sessions.read",
]);

function grantedCapabilities(content: GadgetContent): GadgetCapability[] {
  return content.capabilities.filter((capability): capability is GadgetCapability =>
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
    data.type === GADGET_HELLO_MESSAGE_TYPE &&
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
  rect: GadgetSelection["rect"],
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

export interface GadgetFrameProps {
  app: Gadget;
  /** Published content. Remount the frame (by `key`) when the revision changes. */
  content: GadgetContent;
  onRevisionChanged: () => void;
  /** A template preview: data lives in memory, and actions, navigation and selections are off. */
  preview?: boolean;
  /**
   * The session the gadget shows beside, in that session's side panel. Its actions and quotes go
   * there; without it they go to the viewer's gadget session.
   */
  sessionId?: string;
  /** Adds Add comment to a selection's buttons. */
  onAddComment?: (selection: GadgetSelection) => void;
}

interface PendingConfirmation {
  resolve: (allowed: boolean) => void;
}

interface FrameMount {
  frameId: string;
  nonce: string;
}

function newMount(): FrameMount {
  return { frameId: `omnigent-gadget-${randomUUID()}`, nonce: randomNonce() };
}

/**
 * Runs a gadget's published HTML in a sandboxed frame (or the host's own frame) and answers its
 * SDK calls over one MessagePort per frame document.
 */
export function GadgetFrame({
  app,
  content,
  onRevisionChanged,
  preview = false,
  sessionId,
  onAddComment,
}: GadgetFrameProps) {
  const HostFrame = getOmnigentGadgetFrame();
  const previewRpc = useMemo(() => createPreviewRpc(), []);
  const colorScheme = useResolvedThemeMode();
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const frameWindowRef = useRef<Window | null>(null);
  const pendingHelloRef = useRef<unknown>(undefined);
  const bridgeRef = useRef<GadgetBridge | null>(null);
  // A document gets one port. A later load is a reload or a navigation away, which gets a fresh
  // document and nonce, so a page the frame navigated to can never take over the port.
  const handshakeDoneRef = useRef(false);
  const loadsRef = useRef(0);
  const [mount, setMount] = useState(newMount);
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
    () => buildGadgetFrameDocument(content.html, buildOmnigentSdkBootstrapScript(mount)),
    [content.html, mount],
  );

  // Actions and navigation need a gesture inside the gadget (see `gadgetGestureGate`).
  const frameHostRef = useRef<HTMLDivElement>(null);
  const gateRef = useRef<GadgetGestureGate | null>(null);
  useEffect(() => {
    const frameHost = frameHostRef.current;
    if (!frameHost) {
      return undefined;
    }
    const gate = createGadgetGestureGate({ frameHost });
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
    (request: GadgetPageRequest) => {
      // Without a click inside the gadget, the frame cannot open or replace pages.
      if (!claimGesture()) {
        return false;
      }
      const current = { id: latestRef.current.app.id, name: latestRef.current.app.name };
      let path: string;
      let createSessionMenu = false;
      let review: GadgetPromptReview | null = null;
      switch (request.kind) {
        case "newSession": {
          const prompt = request.prompt ? composeGadgetContextPrompt(request.prompt, current) : "";
          // A full screen gadget stays so: New session's composer holds the prompt, and the
          // Create session menu beside the gadget starts the session.
          createSessionMenu = request.display === "panel" && gadgetFillsScreen(current.id);
          // Full screen hides the composer, so the menu shows what Create session would send
          // and where it came from: confirming the session must not confirm unseen text.
          if (createSessionMenu && prompt) {
            review = { prompt, gadgetId: current.id, gadgetName: current.name };
          }
          path =
            request.display === "panel"
              ? gadgetBoardNewSessionUrl(current.id, prompt, { fullScreen: createSessionMenu })
              : gadgetNewSessionUrl(prompt);
          break;
        }
        case "newAutomation":
          path = gadgetNewAutomationUrl({
            name: request.name,
            prompt: composeGadgetContextPrompt(request.prompt, current),
            schedule: request.schedule,
          });
          break;
        case "openSession":
          if (request.display === "panel") {
            // The session opens with this gadget in its side panel, linked there first (the
            // panel lists only linked gadgets). A cited session does not take the gadget's
            // actions; those keep going to the viewer's gadget session.
            void openGadgetInSession(
              current.id,
              request.sessionId,
              navigationRef.current.navigate,
              { handlesActions: false },
            );
            return true;
          }
          path = gadgetSessionUrl(request.sessionId);
          break;
      }
      if (request.display === "tab") {
        // Under the deployment's base path too, which a new tab does not get from the router.
        const href = withBasePath(navigationRef.current.rebasePath(path));
        window.open(new URL(href, window.location.origin).href, "_blank", "noopener,noreferrer");
      } else {
        navigationRef.current.navigate(path);
        if (createSessionMenu) requestGadgetCreateSessionMenu(review);
      }
      return true;
    },
    [claimGesture],
  );

  const requestAction = useCallback(
    async (request: GadgetActionRequest, signal: AbortSignal) => {
      if (!claimGesture()) {
        throw new BridgeError(
          "PERMISSION_DENIED",
          "Omnigent runs an action only right after a click in the gadget.",
        );
      }
      return requestGadgetAction(
        latestRef.current.app,
        request,
        goTo,
        latestRef.current.sessionId,
        signal,
      );
    },
    [claimGesture, goTo],
  );

  const [selection, setSelection] = useState<GadgetSelection | null>(null);
  // Only a selection made in the gadget (focus is in it) gets the popup, so the frame cannot show
  // it over Omnigent while the viewer works elsewhere.
  const showSelection = useCallback((next: GadgetSelection | null) => {
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
      void quoteInGadgetSession(
        current,
        kind,
        gadgetSelectionQuote(current, selection),
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
    const items = await getGadgetActionProgress(latestRef.current.app.id, actionIds, signal);
    return items.map((item) => ({ ...item }));
  }, []);

  const listSessions = useCallback(
    async (request: GadgetSessionListRequest, signal: AbortSignal) => {
      // A gadget that can read sessions and write shared data could copy the viewer's session
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

  const connect = useCallback(
    (frameWindow: Window) => {
      handshakeDoneRef.current = true;
      bridgeRef.current?.dispose();
      const latest = latestRef.current;
      const context: GadgetInitContext = {
        protocol: GADGET_PROTOCOL_VERSION,
        app: { id: latest.app.id, name: latest.app.name },
        revisionId: latest.content.revision_id,
        // Gadget code never learns who the viewer is: the server's gadget-scoped ID and a real
        // name only, never the login or email (an older server's principal ID, and no name).
        viewer: preview
          ? { id: "preview", displayName: "Preview" }
          : {
              id: latest.content.viewer?.id ?? latest.app.viewer_principal_id ?? "viewer",
              displayName: latest.content.viewer?.display_name ?? null,
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
      bridgeRef.current = startGadgetBridge({
        port: channel.port1,
        context,
        callRpc: (method: GadgetMethod, params: Record<string, unknown>, signal: AbortSignal) =>
          preview
            ? previewRpc(method, params)
            : callGadgetRpc(
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
      if (!preview) {
        bridgeRef.current.setAgentStarting(gadgetSessionStarting(latest.app.id));
        noteGadgetFrameConnected(latest.app.id);
      }
      frameWindow.postMessage(
        { type: GADGET_CONNECT_MESSAGE_TYPE, frameId: mount.frameId, nonce: mount.nonce },
        // The frame's origin is opaque; the nonce and the transferred port are the boundary.
        "*",
        [channel.port2],
      );
    },
    [
      actionProgress,
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
        // The document reloaded or navigated away. Its request has already left, so stop the
        // frame and say so rather than reload it quietly: a gadget that does this again on
        // every load would otherwise keep sending what it can read.
        bridgeRef.current?.dispose();
        bridgeRef.current = null;
        // The reloaded gadget asks again before it reads the viewer's sessions.
        sessionsAccessRef.current = null;
        setSessionsAccess(null);
        setClosedReason("navigated");
        return;
      }
      if (pendingHelloRef.current !== undefined) {
        handleFrameMessage(pendingHelloRef.current);
      }
    },
    [handleFrameMessage],
  );

  // The frame shows its actions as waiting while the viewer's gadget session is being created.
  const appId = app.id;
  useEffect(() => {
    if (preview) return undefined;
    return onGadgetSessionStarting((startedFor, starting) => {
      if (startedFor === appId) bridgeRef.current?.setAgentStarting(starting);
    });
  }, [appId, preview]);

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
    },
    [],
  );

  const title = `Gadget: ${app.name}`;
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
      {closedReason === "protocol_violations" && (
        <Alert>
          <AlertTitle>Omnigent stopped answering this gadget</AlertTitle>
          <AlertDescription>
            It sent too many invalid requests. Reload the page to try again.
          </AlertDescription>
        </Alert>
      )}
      {closedReason === "navigated" && (
        <Alert>
          <AlertTitle>This gadget tried to leave its page, so Omnigent stopped it</AlertTitle>
          <AlertDescription>
            <p>
              Leaving its page can send what the gadget could read to another site. Ask its session
              to fix the page if you didn’t expect this.
            </p>
            <Button
              size="sm"
              variant="outline"
              className="mt-2"
              onClick={() => {
                setClosedReason(null);
                setMount(newMount());
              }}
            >
              Reload gadget
            </Button>
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
              contentSecurityPolicy={GADGET_CONTENT_SECURITY_POLICY}
              title={title}
              frameId={mount.frameId}
              onMessage={handleFrameMessage}
              onLoad={handleFrameLoad}
            />
          ) : (
            <iframe
              key={mount.nonce}
              ref={iframeRef}
              id={mount.frameId}
              title={title}
              sandbox={GADGET_FRAME_SANDBOX}
              allow=""
              referrerPolicy="no-referrer"
              srcDoc={htmlContent}
              onLoad={(event) => {
                const frameWindow = event.currentTarget.contentWindow;
                if (frameWindow) {
                  handleFrameLoad(frameWindow);
                }
              }}
              // The gadget themes itself from `client.ui.colorScheme`.
              className="min-h-0 w-full flex-1 border-0 bg-background"
            />
          ))}
      </div>
      <Dialog
        open={sessionsAccess !== null}
        onOpenChange={(open) => !open && settleSessionsAccess(false)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Let this gadget see your sessions?</DialogTitle>
            <DialogDescription>
              {app.name} wants to see the titles, status and activity details (agent, times, whether
              they are working) of your Omnigent sessions while this page is open. Its code could
              save them where other people with access to this gadget can see them, or send them out
              of Omnigent.
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
