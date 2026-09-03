import {
  EXTENSION_RPC_SOURCE,
  EXTENSION_RPC_VERSION,
  type ExtensionDisposeMessage,
  type ExtensionErrorMessage,
  type ExtensionIdentity,
  type ExtensionIncompatibleMessage,
  type ExtensionInitMessage,
  type ExtensionReadyMessage,
} from "./protocol";

export interface ExtensionContext {
  extensionId: string;
  pageId: string;
  view: string;
  apiVersion: number;
}

export interface ExtensionLifecycle {
  activate(context: ExtensionContext): void | Promise<void>;
  deactivate?(): void | Promise<void>;
}

declare global {
  var __OMNIGENT_EXTENSION__: ExtensionIdentity | undefined;
}

function matchesIdentity(
  message: ExtensionInitMessage,
  identity: ExtensionIdentity,
): boolean {
  return (
    message.source === EXTENSION_RPC_SOURCE &&
    message.type === "init" &&
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

  const deactivate = () => {
    if (!active) return;
    active = false;
    void lifecycle.deactivate?.();
    port?.close();
    port = null;
  };

  const handleInit = (event: MessageEvent<unknown>) => {
    if (active || event.source !== window.parent || event.ports.length !== 1)
      return;
    if (!event.data || typeof event.data !== "object") return;
    const init = event.data as ExtensionInitMessage;
    if (!matchesIdentity(init, identity)) return;
    port = event.ports[0];
    active = true;
    window.removeEventListener("message", handleInit);
    port.onmessage = (portEvent: MessageEvent<unknown>) => {
      const message = portEvent.data as Partial<ExtensionDisposeMessage> | null;
      if (
        message?.source === EXTENSION_RPC_SOURCE &&
        message.type === "dispose" &&
        message.nonce === identity.nonce
      ) {
        deactivate();
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
    const context: ExtensionContext = {
      extensionId: identity.extensionId,
      pageId: identity.pageId,
      view: identity.view,
      apiVersion: identity.apiVersion,
    };
    void Promise.resolve(lifecycle.activate(context)).then(
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
