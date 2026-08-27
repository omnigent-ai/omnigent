export const EXTENSION_RPC_SOURCE = "omnigent-extension";
export const EXTENSION_RPC_VERSION = 1;

export interface ExtensionIdentity {
  extensionId: string;
  pageId: string;
  view: string;
  nonce: string;
  apiVersion: number;
}

export interface ExtensionInitMessage extends ExtensionIdentity {
  source: typeof EXTENSION_RPC_SOURCE;
  type: "init";
}

export interface ExtensionReadyMessage extends ExtensionIdentity {
  source: typeof EXTENSION_RPC_SOURCE;
  type: "ready";
}

export interface ExtensionIncompatibleMessage extends ExtensionIdentity {
  source: typeof EXTENSION_RPC_SOURCE;
  type: "incompatible";
  sdkApiVersion: number;
}

export interface ExtensionErrorMessage extends ExtensionIdentity {
  source: typeof EXTENSION_RPC_SOURCE;
  type: "error";
  message: string;
}

export interface ExtensionDisposeMessage extends ExtensionIdentity {
  source: typeof EXTENSION_RPC_SOURCE;
  type: "dispose";
}
