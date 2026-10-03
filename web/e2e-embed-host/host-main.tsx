/**
 * E2E host page for the embedded Omnigent web UI. It plays the host's role —
 * router, transport (`/v1/...` rebased onto `/omnigent/<a|b>/`, see
 * tests/e2e_ui/embed/_embed_host_harness.py) and auth (asserting the signed-in
 * user per Server) — and can repoint the embed at another Server at runtime by
 * installing a new host config and remounting `OmnigentApp`. Query params:
 * `userA` / `userB` (asserted users), `meDelayA` (ms latency on A's `/v1/me`).
 * `window.omnigentE2EHost` logs what the embed sent per Server and probes its
 * resolved identity.
 */
import { type CSSProperties, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { OmnigentApp, setOmnigentHostConfig, type OmnigentHostConfig } from "@/embed";
import { getOmnigentServerIdentity } from "@/lib/host";
import { getCurrentIsAdmin, resolveIdentity } from "@/lib/identity";

type ServerKey = "a" | "b";

interface SentRequest {
  path: string;
  embedForwardedEmail: string | null;
}

interface IdentityProbe {
  userId: string | null;
  serverIdentity: string | null;
  isAdmin: boolean;
}

interface HostTestHooks {
  sent: Record<ServerKey, SentRequest[]>;
  identity: () => Promise<IdentityProbe>;
}

declare global {
  interface Window {
    omnigentE2EHost: HostTestHooks;
  }
}

window.omnigentE2EHost = {
  sent: { a: [], b: [] },
  identity: async () => ({
    userId: await resolveIdentity(),
    serverIdentity: getOmnigentServerIdentity(),
    isAdmin: getCurrentIsAdmin(),
  }),
};

const params = new URLSearchParams(window.location.search);
const users: Record<ServerKey, string> = {
  a: params.get("userA") ?? "alice@example.test",
  b: params.get("userB") ?? "bob@example.test",
};
const meDelayA = Number(params.get("meDelayA")) || 0;

function hostConfigFor(server: ServerKey): OmnigentHostConfig {
  const fetcher = async (path: string, init?: RequestInit): Promise<Response> => {
    const headers = new Headers(init?.headers);
    window.omnigentE2EHost.sent[server].push({
      path,
      embedForwardedEmail: headers.get("X-Forwarded-Email"),
    });
    headers.set("X-Forwarded-Email", users[server]);
    if (server === "a" && meDelayA > 0 && path.startsWith("/v1/me")) {
      await new Promise<void>((resolve) => {
        setTimeout(resolve, meDelayA);
      });
    }
    return window.fetch(`/omnigent/${server}${path}`, { ...init, headers });
  };
  return { serverIdentity: `server-${server}`, fetcher };
}

const hostConfigs: Record<ServerKey, OmnigentHostConfig> = {
  a: hostConfigFor("a"),
  b: hostConfigFor("b"),
};

// Installed eagerly before first render, as the production embed loader does.
setOmnigentHostConfig(hostConfigs.a);

const buttonStyle: CSSProperties = {
  padding: "4px 10px",
  borderRadius: 6,
  border: "1px solid #6b7280",
  background: "#374151",
  color: "#f9fafb",
  cursor: "pointer",
  font: "inherit",
};

function HostApp() {
  const [server, setServer] = useState<ServerKey>("a");
  const connect = (next: ServerKey) => {
    if (next === server) return;
    setOmnigentHostConfig(hostConfigs[next]);
    setServer(next);
  };
  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <header
        data-testid="host-chrome"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          padding: "8px 16px",
          background: "#1f2937",
          color: "#f9fafb",
          fontFamily: "system-ui, sans-serif",
          fontSize: 14,
          flex: "none",
          // Stay clickable above the app's fixed-position viewport-top elements.
          position: "relative",
          zIndex: 10000,
        }}
      >
        <strong>Host app</strong>
        <span data-testid="host-current-server">
          {server === "a"
            ? `Server A — signed in as ${users.a}`
            : `Server B — signed in as ${users.b}`}
        </span>
        <button
          type="button"
          data-testid="host-connect-a"
          onClick={() => connect("a")}
          style={buttonStyle}
        >
          Connect Server A
        </button>
        <button
          type="button"
          data-testid="host-connect-b"
          onClick={() => connect("b")}
          style={buttonStyle}
        >
          Connect Server B
        </button>
      </header>
      <main style={{ flex: 1, minHeight: 0 }}>
        <BrowserRouter>
          {/* The key swap remounts the embed on the new host config. */}
          <OmnigentApp
            key={server}
            serverIdentity={hostConfigs[server].serverIdentity}
            fetcher={hostConfigs[server].fetcher}
          />
        </BrowserRouter>
      </main>
    </div>
  );
}

const rootEl = document.getElementById("host-root");
if (!rootEl) {
  throw new Error("embed e2e host page is missing its #host-root element");
}
createRoot(rootEl).render(<HostApp />);
