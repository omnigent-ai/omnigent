// Test host: embeds Omnigent through the public embed API and repoints it at
// another Server the way a production host does (new host config + remount).
// The identity probes below are test-only reads of internals, not part of it.
import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { OmnigentApp, setOmnigentHostConfig, type OmnigentHostConfig } from "../src/embed";
import { getOmnigentServerIdentity } from "../src/lib/host";
import { getCurrentIsAdmin, resolveIdentity } from "../src/lib/identity";

type ServerKey = "a" | "b";

// Injected by vite.e2e-embed-host.config.ts from the harness's environment.
declare const OMNIGENT_E2E_USERS: Record<ServerKey, string>;

interface HostServer {
  key: ServerKey;
  label: string;
  serverIdentity: string;
  /** The user the host has signed in on this Server. */
  user: string;
  /** Same-origin prefix the preview server proxies to this Server. */
  prefix: string;
}

const SERVERS: Record<ServerKey, HostServer> = {
  a: {
    key: "a",
    label: "Server A",
    serverIdentity: "server-a",
    user: OMNIGENT_E2E_USERS.a,
    prefix: "/server-a",
  },
  b: {
    key: "b",
    label: "Server B",
    serverIdentity: "server-b",
    user: OMNIGENT_E2E_USERS.b,
    prefix: "/server-b",
  },
};

interface SentRequest {
  path: string;
  /** `X-Forwarded-Email` as stamped by the embed, before the gateway overrides it. */
  embedUser: string | null;
}

interface AnsweredRequest {
  path: string;
}

const params = new URLSearchParams(window.location.search);
const initialServer: ServerKey = params.get("server") === "b" ? "b" : "a";
// `holdMeA=1` holds Server A's `/v1/me` answer until the test releases it;
// `meDelayA=<ms>` delays it by a fixed time instead.
const holdMeA = params.get("holdMeA") === "1";
const meDelayA = Number(params.get("meDelayA") ?? "0");
let releaseMeA: () => void = () => {};
const meGateA = new Promise<void>((resolve) => {
  releaseMeA = resolve;
});

const transportLog = {
  sent: { a: [] as SentRequest[], b: [] as SentRequest[] },
  answered: { a: [] as AnsweredRequest[], b: [] as AnsweredRequest[] },
  releaseMeA: () => releaseMeA(),
  async identity() {
    return {
      user: await resolveIdentity(),
      isAdmin: getCurrentIsAdmin(),
      serverIdentity: getOmnigentServerIdentity(),
    };
  },
};

declare global {
  interface Window {
    omnigentE2EHost: typeof transportLog;
  }
}
window.omnigentE2EHost = transportLog;

function hostConfigFor(server: HostServer): OmnigentHostConfig {
  return {
    serverIdentity: server.serverIdentity,
    fetcher: async (path, init) => {
      const headers = new Headers(init?.headers);
      transportLog.sent[server.key].push({ path, embedUser: headers.get("X-Forwarded-Email") });
      const response = await window.fetch(server.prefix + path, { ...init, headers });
      if (server.key === "a" && path.startsWith("/v1/me")) {
        if (holdMeA) await meGateA;
        if (meDelayA > 0) {
          await new Promise<void>((resolve) => {
            setTimeout(resolve, meDelayA);
          });
        }
      }
      transportLog.answered[server.key].push({ path });
      return response;
    },
    resolveWebSocketUrl: (path) => {
      const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
      return `${scheme}//${window.location.host}${server.prefix}${path}`;
    },
  };
}

const HOST_CONFIGS: Record<ServerKey, OmnigentHostConfig> = {
  a: hostConfigFor(SERVERS.a),
  b: hostConfigFor(SERVERS.b),
};

function HostApp() {
  const [serverKey, setServerKey] = useState<ServerKey>(initialServer);
  const server = SERVERS[serverKey];
  const connect = (key: ServerKey) => {
    setOmnigentHostConfig(HOST_CONFIGS[key]);
    setServerKey(key);
  };
  return (
    <>
      <header className="host-header" data-testid="host-header">
        <strong>Omnigent host (e2e)</strong>
        <span data-testid="host-status">
          {server.label} — signed in as {server.user}
        </span>
        <button type="button" data-testid="host-connect-a" onClick={() => connect("a")}>
          Connect Server A
        </button>
        <button type="button" data-testid="host-connect-b" onClick={() => connect("b")}>
          Connect Server B
        </button>
      </header>
      <main className="host-embed">
        <OmnigentApp key={serverKey} {...HOST_CONFIGS[serverKey]} />
      </main>
    </>
  );
}

setOmnigentHostConfig(HOST_CONFIGS[initialServer]);

const root = document.getElementById("host-root");
if (!root) throw new Error("e2e embed host: #host-root is missing");
createRoot(root).render(
  <StrictMode>
    <BrowserRouter>
      <HostApp />
    </BrowserRouter>
  </StrictMode>,
);
