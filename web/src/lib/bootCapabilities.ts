import type { ServerInfo } from "./capabilities";

export const SERVER_INFO_OFFLINE_FALLBACK: ServerInfo = {
  accounts_enabled: false,
  single_user: false,
  login_url: null,
  needs_setup: false,
  databricks_features: false,
  managed_sandboxes_enabled: false,
  sandbox_provider: null,
  sandbox_providers: [],
  sharing_mode: "on",
  public_sharing_enabled: true,
  server_version: null,
  smart_routing_enabled: false,
  smart_routing_sources: { external: false, oss: false },
  features: {},
  harness_install_enabled: false,
  installable_harnesses: [],
  dictation_available: false,
  branding: null,
};

export function createBootServerInfo(
  settled: Promise<ServerInfo>,
  timeoutMs = 1500,
): { initial: Promise<ServerInfo>; settled: Promise<ServerInfo> } {
  let timeout: ReturnType<typeof setTimeout>;
  const fallback = new Promise<ServerInfo>((resolve) => {
    timeout = setTimeout(() => resolve(SERVER_INFO_OFFLINE_FALLBACK), timeoutMs);
  });
  void settled.then(
    () => clearTimeout(timeout),
    () => clearTimeout(timeout),
  );
  return { initial: Promise.race([settled, fallback]), settled };
}
