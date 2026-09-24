import { authenticatedFetch } from "./identity";
import { withBasePath } from "./basePath";

export interface RegistryService {
  id: string;
  title: string;
  description: string;
  auth: "none" | "bearer" | "oauth" | "github" | "databricks";
  connected: boolean;
  tools: string[];
  connect_provider: string;
}

export async function registryRequest<T>(path = "", init?: RequestInit): Promise<T> {
  const response = await authenticatedFetch(`/v1/mcp-registry/services${path}`, init);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : "MCP registry request failed");
  }
  return response.json() as Promise<T>;
}

export function authorizeRegistryService(service: RegistryService, signal: AbortSignal) {
  const returnTo = withBasePath("/settings/integrations");
  const url = withBasePath(
    `/v1/connections/${encodeURIComponent(service.connect_provider)}/connect?return_to=${encodeURIComponent(returnTo)}`,
  );
  const popup = window.open("about:blank", "_blank", "popup,width=600,height=720");
  if (!popup) return Promise.reject(new Error("Allow pop-ups to connect this account."));
  // The provider page must not be able to navigate the session window.
  popup.opener = null;
  popup.location.href = url;
  return new Promise<void>((resolve, reject) => {
    const finish = (error?: string) => {
      window.clearInterval(interval);
      window.clearTimeout(timeout);
      signal.removeEventListener("abort", cancel);
      popup.close();
      if (error) reject(new Error(error));
      else resolve();
    };
    const cancel = () => finish("Sign-in cancelled. Try again when ready.");
    const interval = window.setInterval(() => {
      if (popup.closed) {
        cancel();
        return;
      }
      try {
        const location = new URL(popup.location.href);
        if (location.origin !== window.location.origin || location.pathname !== returnTo) return;
        const status = location.searchParams.get(service.connect_provider);
        if (status === "connected") finish();
        else if (status === "error") finish("Sign-in failed. Please try again.");
      } catch {
        // The provider's cross-origin page is unreadable until it redirects back.
      }
    }, 500);
    const timeout = window.setTimeout(
      () => finish("Sign-in timed out. Please try again."),
      300_000,
    );
    signal.addEventListener("abort", cancel, { once: true });
    if (signal.aborted) cancel();
  });
}
