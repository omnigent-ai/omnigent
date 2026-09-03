import {
  EXTENSION_RPC_SOURCE,
  EXTENSION_RPC_VERSION,
  type ExtensionCancelMessage,
  type ExtensionDisposeMessage,
  type ExtensionErrorMessage,
  type ExtensionEventMessage,
  type ExtensionIdentity,
  type ExtensionIncompatibleMessage,
  type ExtensionInitMessage,
  type ExtensionReadyMessage,
  type ExtensionRequestMessage,
  type ExtensionResponseMessage,
} from "./protocol";

export interface Disposable {
  dispose(): void;
}

export interface ThemeInfo {
  theme: "light" | "dark";
}

export class ExtensionApiError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ExtensionApiError";
  }
}

export interface ExtensionContext {
  extensionId: string;
  pageId: string;
  view: string;
  apiVersion: number;
  capabilities: readonly string[];
  navigation: {
    openPage(
      pageId: string,
      params?: Record<string, string | number | boolean>,
    ): Promise<void>;
    openSession(sessionId: string): Promise<void>;
    openNewSession(): Promise<void>;
  };
  theme: {
    getCurrent(): Promise<ThemeInfo>;
    subscribe(listener: (theme: ThemeInfo) => void): Promise<Disposable>;
  };
  storage: {
    user: {
      get<T = unknown>(key: string): Promise<T | null>;
      set(key: string, value: unknown): Promise<void>;
      delete(key: string): Promise<void>;
    };
  };
}

export interface ExtensionLifecycle {
  activate(context: ExtensionContext): void | Promise<void>;
  deactivate?(): void | Promise<void>;
}

interface PendingRequest {
  resolve(value: unknown): void;
  reject(reason: unknown): void;
  timeout: ReturnType<typeof setTimeout>;
}

declare global {
  var __OMNIGENT_EXTENSION__: ExtensionIdentity | undefined;
}

function matchesIdentity(
  message: Partial<ExtensionIdentity>,
  identity: ExtensionIdentity,
): boolean {
  return (
    message.extensionId === identity.extensionId &&
    message.pageId === identity.pageId &&
    message.view === identity.view &&
    message.nonce === identity.nonce &&
    message.apiVersion === identity.apiVersion
  );
}

export function defineExtension(lifecycle: ExtensionLifecycle): void {
  const identity = globalThis.__OMNIGENT_EXTENSION__;
  if (!identity) throw new Error("Omnigent extension bootstrap is missing");
  let active = false;
  let port: MessagePort | null = null;
  let requestSequence = 0;
  const pending = new Map<string, PendingRequest>();
  const listeners = new Map<string, Set<(value: unknown) => void>>();
  let grantedCapabilities = new Set<string>();

  const rejectPending = () => {
    for (const request of pending.values()) {
      clearTimeout(request.timeout);
      request.reject(
        new ExtensionApiError("Disposed", "Extension host was disposed"),
      );
    }
    pending.clear();
  };

  const deactivate = () => {
    if (!active) return;
    active = false;
    rejectPending();
    void lifecycle.deactivate?.();
    port?.close();
    port = null;
  };

  const request = <T>(method: string, params: unknown): Promise<T> => {
    if (!active || !port) {
      return Promise.reject(
        new ExtensionApiError("NotActive", "Extension is not active"),
      );
    }
    if (!grantedCapabilities.has(method)) {
      return Promise.reject(
        new ExtensionApiError(
          "Unsupported",
          `Host method ${method} is not granted`,
        ),
      );
    }
    const requestId = `${identity.nonce}-${++requestSequence}`;
    const message: ExtensionRequestMessage = {
      ...identity,
      source: EXTENSION_RPC_SOURCE,
      type: "request",
      requestId,
      method,
      params,
    };
    return new Promise<T>((resolve, reject) => {
      const timeout = setTimeout(() => {
        if (!pending.delete(requestId)) return;
        const cancel: ExtensionCancelMessage = {
          ...identity,
          source: EXTENSION_RPC_SOURCE,
          type: "cancel",
          requestId,
        };
        port?.postMessage(cancel);
        reject(
          new ExtensionApiError("Timeout", `Host method ${method} timed out`),
        );
      }, 10_000);
      pending.set(requestId, {
        resolve: (value) => resolve(value as T),
        reject,
        timeout,
      });
      port?.postMessage(message);
    });
  };

  const createContext = (): ExtensionContext => ({
    extensionId: identity.extensionId,
    pageId: identity.pageId,
    view: identity.view,
    apiVersion: identity.apiVersion,
    capabilities: [...grantedCapabilities].sort(),
    navigation: {
      async openPage(pageId, params) {
        await request("navigation.openPage", { pageId, params });
      },
      async openSession(sessionId) {
        await request("navigation.openSession", { sessionId });
      },
      async openNewSession() {
        await request("navigation.openNewSession", {});
      },
    },
    theme: {
      getCurrent: () => request<ThemeInfo>("theme.getCurrent", {}),
      async subscribe(listener) {
        const initial = await request<ThemeInfo>("theme.subscribe", {});
        let handlers = listeners.get("theme.changed");
        if (!handlers) {
          handlers = new Set();
          listeners.set("theme.changed", handlers);
        }
        const handler = listener as (value: unknown) => void;
        handlers.add(handler);
        listener(initial);
        return { dispose: () => handlers?.delete(handler) };
      },
    },
    storage: {
      user: {
        get: <T>(key: string) => request<T | null>("storage.user.get", { key }),
        async set(key, value) {
          await request("storage.user.set", { key, value });
        },
        async delete(key) {
          await request("storage.user.delete", { key });
        },
      },
    },
  });

  const handleInit = (event: MessageEvent<unknown>) => {
    if (active || event.source !== window.parent || event.ports.length !== 1)
      return;
    if (!event.data || typeof event.data !== "object") return;
    const init = event.data as ExtensionInitMessage;
    if (
      init.source !== EXTENSION_RPC_SOURCE ||
      init.type !== "init" ||
      !matchesIdentity(init, identity) ||
      !Array.isArray(init.capabilities) ||
      !init.capabilities.every((item) => typeof item === "string")
    ) {
      return;
    }
    port = event.ports[0];
    grantedCapabilities = new Set(init.capabilities);
    active = true;
    window.removeEventListener("message", handleInit);
    port.onmessage = (portEvent: MessageEvent<unknown>) => {
      if (!portEvent.data || typeof portEvent.data !== "object") return;
      const message = portEvent.data as
        | ExtensionDisposeMessage
        | ExtensionResponseMessage
        | ExtensionEventMessage;
      if (
        message.source !== EXTENSION_RPC_SOURCE ||
        !matchesIdentity(message, identity)
      )
        return;
      if (message.type === "dispose") {
        deactivate();
        return;
      }
      if (message.type === "response") {
        const requestState = pending.get(message.requestId);
        if (!requestState) return;
        pending.delete(message.requestId);
        clearTimeout(requestState.timeout);
        if (message.error) {
          requestState.reject(
            new ExtensionApiError(message.error.code, message.error.message),
          );
        } else {
          requestState.resolve(message.result);
        }
        return;
      }
      if (message.type === "event") {
        for (const listener of listeners.get(message.event) ?? [])
          listener(message.value);
      }
    };
    port.start();
    if (identity.apiVersion !== EXTENSION_RPC_VERSION) {
      const incompatible: ExtensionIncompatibleMessage = {
        ...identity,
        source: EXTENSION_RPC_SOURCE,
        type: "incompatible",
        sdkApiVersion: EXTENSION_RPC_VERSION,
      };
      port.postMessage(incompatible);
      return;
    }
    void Promise.resolve(lifecycle.activate(createContext())).then(
      () => {
        const ready: ExtensionReadyMessage = {
          ...identity,
          source: EXTENSION_RPC_SOURCE,
          type: "ready",
        };
        port?.postMessage(ready);
      },
      (reason: unknown) => {
        const failed: ExtensionErrorMessage = {
          ...identity,
          source: EXTENSION_RPC_SOURCE,
          type: "error",
          message:
            reason instanceof Error
              ? reason.message.slice(0, 512)
              : "Activation failed",
        };
        port?.postMessage(failed);
      },
    );
  };
  window.addEventListener("message", handleInit);
  window.addEventListener("pagehide", deactivate, { once: true });
}

export { EXTENSION_RPC_VERSION } from "./protocol";
