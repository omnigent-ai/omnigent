import { authenticatedFetch } from "@/lib/identity";
import type { ExtensionCatalogItem, ExtensionPage } from "../types";
import { EXTENSION_RPC_VERSION, type ExtensionIdentity } from "./protocol";

export interface LoadedExtensionBundle {
  extension: ExtensionCatalogItem;
  script: string;
  styles: string;
}

export function createExtensionNonce(): string {
  const bytes = new Uint8Array(24);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

async function fetchAsset(url: string): Promise<Response> {
  return authenticatedFetch(url);
}

async function readBundle(extension: ExtensionCatalogItem): Promise<LoadedExtensionBundle> {
  const scriptUrl = extension.browser.script_url;
  if (!scriptUrl || !extension.browser.digest) throw new Error("Extension bundle is unavailable");
  const scriptResponse = await fetchAsset(scriptUrl);
  if (!scriptResponse.ok) {
    const error = new Error(`Extension script failed to load (${scriptResponse.status})`);
    Object.assign(error, { status: scriptResponse.status });
    throw error;
  }
  let styles = "";
  if (extension.browser.style_url) {
    const styleResponse = await fetchAsset(extension.browser.style_url);
    if (!styleResponse.ok) {
      const error = new Error(`Extension styles failed to load (${styleResponse.status})`);
      Object.assign(error, { status: styleResponse.status });
      throw error;
    }
    styles = await styleResponse.text();
  }
  return { extension, script: await scriptResponse.text(), styles };
}

export async function loadExtensionBundle(
  extension: ExtensionCatalogItem,
  refresh: () => Promise<ExtensionCatalogItem[]>,
): Promise<LoadedExtensionBundle> {
  try {
    return await readBundle(extension);
  } catch (error) {
    if (!(error instanceof Error) || (error as Error & { status?: number }).status !== 404) {
      throw error;
    }
    const refreshed = (await refresh()).find((item) => item.id === extension.id);
    if (!refreshed || refreshed.status !== "enabled") throw error;
    return readBundle(refreshed);
  }
}

function encodeBase64(value: string): string {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

export function buildExtensionDocument(
  bundle: LoadedExtensionBundle,
  page: ExtensionPage,
  nonce: string,
): { srcDoc: string; identity: ExtensionIdentity } {
  const identity: ExtensionIdentity = {
    extensionId: bundle.extension.id,
    pageId: page.id,
    view: page.view,
    nonce,
    apiVersion: EXTENSION_RPC_VERSION,
  };
  const serializedIdentity = JSON.stringify(identity).replaceAll("<", "\\u003c");
  const encodedStyles = encodeBase64(bundle.styles);
  const encodedScript = encodeBase64(bundle.script);
  const bootstrap = `(()=>{const d=(s)=>new TextDecoder().decode(Uint8Array.from(atob(s),c=>c.charCodeAt(0)));globalThis.__OMNIGENT_EXTENSION__=${serializedIdentity};const style=document.createElement('style');style.textContent=d('${encodedStyles}');document.head.append(style);const script=document.createElement('script');script.nonce='${nonce}';script.textContent=d('${encodedScript}');document.body.append(script)})();`;
  const csp = [
    "default-src 'none'",
    `script-src 'nonce-${nonce}'`,
    "style-src 'unsafe-inline'",
    "connect-src 'none'",
    "img-src data: blob:",
    "font-src data:",
    "form-action 'none'",
    "base-uri 'none'",
    "webrtc 'none'",
  ].join("; ");
  return {
    identity,
    srcDoc: `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${csp}"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="root"></div><script nonce="${nonce}">${bootstrap}</script></body></html>`,
  };
}
