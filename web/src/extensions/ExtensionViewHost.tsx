import { useCallback, useEffect, useRef, useState } from "react";
import { Spinner } from "@/components/ui/spinner";
import type { ExtensionCatalogItem, ExtensionPage } from "./types";
import { buildExtensionDocument, createExtensionNonce, loadExtensionBundle } from "./rpc/host";
import {
  EXTENSION_RPC_SOURCE,
  type ExtensionDisposeMessage,
  type ExtensionEventMessage,
  type ExtensionIdentity,
  type ExtensionInitMessage,
  type ExtensionResponseMessage,
} from "./rpc/protocol";
import {
  isExtensionInboundMessage,
  isExtensionPayloadWithinBudget,
  isExtensionSessionPageWithinBudget,
} from "./rpc/validation";
import { ExtensionHostServiceError } from "./services/errors";

const ACTIVATION_TIMEOUT_MS = 10_000;
const REQUEST_TIMEOUT_MS = 10_000;
const MAX_PENDING_HOST_REQUESTS = 32;

type HostMethod = (params: unknown, signal: AbortSignal) => unknown | Promise<unknown>;
const NO_HOST_METHODS: Readonly<Record<string, HostMethod>> = {};
const NO_HOST_EVENTS: Readonly<Record<string, unknown>> = {};
interface PendingRequest {
  controller: AbortController;
  timeout: ReturnType<typeof setTimeout>;
}

interface DocumentState {
  srcDoc: string;
  identity: ExtensionIdentity;
}

export function ExtensionViewHost({
  extension,
  page,
  refresh,
  methods = NO_HOST_METHODS,
  events = NO_HOST_EVENTS,
}: {
  extension: ExtensionCatalogItem;
  page: ExtensionPage;
  refresh: () => Promise<ExtensionCatalogItem[]>;
  methods?: Readonly<Record<string, HostMethod>>;
  events?: Readonly<Record<string, unknown>>;
}) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const portRef = useRef<MessagePort | null>(null);
  const identityRef = useRef<ExtensionIdentity | null>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const handshakeDoneRef = useRef(false);
  const staleRetryDoneRef = useRef(false);
  const pendingRef = useRef(new Map<string, PendingRequest>());
  const methodsRef = useRef(methods);
  const [frameDocument, setFrameDocument] = useState<DocumentState | null>(null);
  const [status, setStatus] = useState<"loading" | "activating" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const [retryKey, setRetryKey] = useState(0);

  const closeRuntime = useCallback(() => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    timeoutRef.current = null;
    for (const pending of pendingRef.current.values()) {
      clearTimeout(pending.timeout);
      pending.controller.abort();
    }
    pendingRef.current.clear();
    const port = portRef.current;
    const identity = identityRef.current;
    if (port && identity) {
      const dispose: ExtensionDisposeMessage = {
        ...identity,
        source: EXTENSION_RPC_SOURCE,
        type: "dispose",
      };
      port.postMessage(dispose);
      port.onmessage = null;
      port.close();
    }
    portRef.current = null;
    identityRef.current = null;
  }, []);

  useEffect(() => {
    methodsRef.current = methods;
  }, [methods]);

  useEffect(() => {
    staleRetryDoneRef.current = false;
  }, [extension.id, page.id]);

  useEffect(() => {
    let cancelled = false;
    closeRuntime();
    handshakeDoneRef.current = false;
    setStatus("loading");
    setError(null);
    setFrameDocument(null);
    const refreshOnce = async () => {
      if (staleRetryDoneRef.current) return [];
      staleRetryDoneRef.current = true;
      return refresh();
    };
    void loadExtensionBundle(extension, refreshOnce)
      .then((bundle) => {
        if (cancelled) return;
        const nextDocument = buildExtensionDocument(bundle, page, createExtensionNonce());
        setFrameDocument(nextDocument);
        setStatus("activating");
        timeoutRef.current = setTimeout(() => {
          closeRuntime();
          setError("Extension activation timed out");
          setStatus("error");
        }, ACTIVATION_TIMEOUT_MS);
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        setError(reason instanceof Error ? reason.message : "Extension bundle failed to load");
        setStatus("error");
      });
    return () => {
      cancelled = true;
      closeRuntime();
    };
  }, [closeRuntime, extension, page, refresh, retryKey]);

  useEffect(() => {
    const port = portRef.current;
    const identity = identityRef.current;
    if (status !== "ready" || !port || !identity) return;
    for (const [event, value] of Object.entries(events)) {
      const message: ExtensionEventMessage = {
        ...identity,
        source: EXTENSION_RPC_SOURCE,
        type: "event",
        event,
        value,
      };
      if (isExtensionPayloadWithinBudget(message)) port.postMessage(message);
    }
  }, [events, status]);

  const handleLoad = useCallback(() => {
    if (!frameDocument || !iframeRef.current?.contentWindow) return;
    if (handshakeDoneRef.current) {
      closeRuntime();
      setError("Extension frame reloaded during activation");
      setStatus("error");
      return;
    }
    handshakeDoneRef.current = true;
    const channel = new MessageChannel();
    portRef.current = channel.port1;
    identityRef.current = frameDocument.identity;
    channel.port1.onmessage = (event: MessageEvent<unknown>) => {
      if (!isExtensionInboundMessage(event.data, frameDocument.identity)) return;
      const message = event.data;
      if (message.type === "ready") {
        if (timeoutRef.current) clearTimeout(timeoutRef.current);
        timeoutRef.current = null;
        setStatus("ready");
        return;
      }
      if (message.type === "incompatible") {
        closeRuntime();
        setError(
          `Extension SDK API ${message.sdkApiVersion} is incompatible with host API ${message.apiVersion}`,
        );
        setStatus("error");
        return;
      }
      if (message.type === "error") {
        closeRuntime();
        setError(message.message.slice(0, 512));
        setStatus("error");
        return;
      }
      if (message.type === "cancel") {
        const pending = pendingRef.current.get(message.requestId);
        if (pending) {
          clearTimeout(pending.timeout);
          pending.controller.abort();
          pendingRef.current.delete(message.requestId);
        }
        return;
      }
      const response: ExtensionResponseMessage = {
        ...frameDocument.identity,
        source: EXTENSION_RPC_SOURCE,
        type: "response",
        requestId: message.requestId,
      };
      const currentMethods = methodsRef.current;
      const method = Object.hasOwn(currentMethods, message.method)
        ? currentMethods[message.method]
        : undefined;
      if (!method) {
        response.error = { code: "MethodNotFound", message: "Host method is not available" };
        channel.port1.postMessage(response);
        return;
      }
      if (pendingRef.current.has(message.requestId)) {
        response.error = { code: "DuplicateRequest", message: "Request ID is already active" };
        channel.port1.postMessage(response);
        return;
      }
      if (pendingRef.current.size >= MAX_PENDING_HOST_REQUESTS) {
        response.error = { code: "Busy", message: "Too many extension host requests" };
        channel.port1.postMessage(response);
        return;
      }
      const controller = new AbortController();
      const requestTimeout = setTimeout(() => {
        if (!pendingRef.current.delete(message.requestId)) return;
        controller.abort();
        channel.port1.postMessage({
          ...response,
          error: { code: "RequestTimeout", message: "Host request timed out" },
        });
      }, REQUEST_TIMEOUT_MS);
      pendingRef.current.set(message.requestId, { controller, timeout: requestTimeout });
      void Promise.resolve()
        .then(() => method(message.params, controller.signal))
        .then(
          (result) => {
            const pending = pendingRef.current.get(message.requestId);
            if (!pending || !pendingRef.current.delete(message.requestId)) return;
            clearTimeout(pending.timeout);
            const withinBudget =
              message.method === "sessions.listPage" || message.method === "sessions.getCached"
                ? isExtensionSessionPageWithinBudget(result)
                : isExtensionPayloadWithinBudget(result);
            if (!withinBudget) {
              channel.port1.postMessage({
                ...response,
                error: { code: "ResponseTooLarge", message: "Host response exceeds the limit" },
              });
              return;
            }
            channel.port1.postMessage({ ...response, result });
          },
          (reason: unknown) => {
            const pending = pendingRef.current.get(message.requestId);
            if (!pending || !pendingRef.current.delete(message.requestId)) return;
            clearTimeout(pending.timeout);
            channel.port1.postMessage({
              ...response,
              error: {
                code:
                  reason instanceof ExtensionHostServiceError
                    ? reason.code
                    : controller.signal.aborted
                      ? "Cancelled"
                      : "HostError",
                message:
                  reason instanceof Error ? reason.message.slice(0, 512) : "Host call failed",
              },
            });
          },
        );
    };
    channel.port1.start();
    const init: ExtensionInitMessage = {
      ...frameDocument.identity,
      source: EXTENSION_RPC_SOURCE,
      type: "init",
      capabilities: Object.keys(methodsRef.current).sort(),
    };
    // A srcdoc frame has opaque origin "null"; the per-mount nonce and the
    // transferred port are the spoofing boundary, so targetOrigin must be "*".
    iframeRef.current.contentWindow.postMessage(init, "*", [channel.port2]);
  }, [closeRuntime, frameDocument]);

  if (status === "error") {
    return (
      <div role="alert" className="flex h-full items-center justify-center p-6">
        <div className="max-w-md rounded-lg border border-destructive/40 bg-card p-6 text-center">
          <h1 className="font-semibold">{page.title} could not start</h1>
          <p className="mt-2 text-sm text-muted-foreground">{error}</p>
          <button
            type="button"
            className="mt-4 rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
            onClick={() => {
              staleRetryDoneRef.current = false;
              setRetryKey((value) => value + 1);
            }}
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="extension-view-host relative flex h-full min-h-0 w-full flex-col overflow-hidden pt-14 md:pt-12">
      {frameDocument && (
        <iframe
          key={frameDocument.identity.nonce}
          ref={iframeRef}
          title={page.title}
          sandbox="allow-scripts"
          allow=""
          srcDoc={frameDocument.srcDoc}
          onLoad={handleLoad}
          className="min-h-0 w-full flex-1 border-0 bg-background"
        />
      )}
      {status !== "ready" && (
        <div className="extension-view-status absolute inset-x-0 bottom-0 top-14 flex items-center justify-center bg-background md:top-12">
          <Spinner className="size-5 text-muted-foreground" aria-label="Loading extension" />
        </div>
      )}
    </div>
  );
}
