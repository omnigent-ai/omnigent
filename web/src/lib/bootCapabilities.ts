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

/**
 * Race a boot probe against a timeout so first paint never waits on a
 * stalled server: whichever settles first wins, and `fallback` stands in
 * when the probe is too slow. The timer is cleared once the probe settles
 * so a fast boot doesn't leave a pending handle behind (which would hold
 * the event loop open in tests).
 */
export function withBootTimeout<T>(settled: Promise<T>, fallback: T, timeoutMs = 1500): Promise<T> {
  let timeout: ReturnType<typeof setTimeout>;
  const timer = new Promise<T>((resolve) => {
    timeout = setTimeout(() => resolve(fallback), timeoutMs);
  });
  void settled.then(
    () => clearTimeout(timeout),
    () => clearTimeout(timeout),
  );
  return Promise.race([settled, timer]);
}

export function createBootServerInfo(
  settled: Promise<ServerInfo>,
  timeoutMs = 1500,
): { initial: Promise<ServerInfo>; settled: Promise<ServerInfo> } {
  return {
    initial: withBootTimeout(settled, SERVER_INFO_OFFLINE_FALLBACK, timeoutMs),
    settled,
  };
}
