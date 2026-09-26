import { useCallback, useEffect, useRef, useState } from "react";
import { PlugIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { withBasePath } from "@/lib/basePath";
import { authorizeRegistryService, registryRequest, type RegistryService } from "@/lib/mcpRegistry";

function useCatalog() {
  const [services, setServices] = useState<RegistryService[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const refresh = useCallback(async () => {
    try {
      const result = await registryRequest<{ data: RegistryService[] }>();
      setServices(result.data);
      setError(null);
      return result.data;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load MCP services");
      return undefined;
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void refresh();
    const onFocus = () => void refresh();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [refresh]);
  return { services, error, loading, refresh };
}

export function McpRegistryConnections() {
  const { services, error, loading, refresh } = useCatalog();
  const callbackFailed = [...new URLSearchParams(window.location.search)].some(
    ([key, value]) => key.startsWith("mcp-") && value === "error",
  );
  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-sm font-medium">Managed MCP services</h3>
        <p className="text-sm text-muted-foreground">
          Connect once, then add these tools to any session. Your accounts are reused across
          sessions on local machines, remote hosts, and sandboxes in this workspace.
        </p>
      </div>
      {callbackFailed && (
        <p role="alert" className="text-sm text-destructive">
          Account connection failed. Try connecting again.
        </p>
      )}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      {services.map((service) => (
        <ServiceConnection
          key={service.id}
          service={service}
          onChanged={async () => {
            await refresh();
          }}
        />
      ))}
      {loading && <p className="text-sm text-muted-foreground">Loading MCP services…</p>}
      {!loading && !services.length && !error && (
        <p className="text-sm text-muted-foreground">
          No managed services are available for your account.
        </p>
      )}
    </div>
  );
}

function ServiceConnection({
  service,
  onChanged,
}: {
  service: RegistryService;
  onChanged: () => Promise<void>;
}) {
  const [token, setToken] = useState("");
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="space-y-2 rounded-md border border-border p-3">
      <div className="flex items-center justify-between gap-2">
        <div>
          <p className="text-sm font-medium">{service.title}</p>
          <p className="text-sm text-muted-foreground">{service.description}</p>
        </div>
        <span className="text-xs text-muted-foreground">
          {service.connected ? "Connected" : "Not connected"}
        </span>
      </div>
      <p className="text-xs text-muted-foreground">Available tools: {service.tools.join(", ")}</p>
      <div className="flex flex-wrap gap-2">
        {service.auth === "bearer" && (
          <Button size="sm" variant="outline" disabled={busy} onClick={() => setEditing(!editing)}>
            {service.connected ? "Update token" : "Connect"}
          </Button>
        )}
        {service.auth === "oauth" && (
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => {
              window.location.href = withBasePath(
                `/v1/connections/${service.connect_provider}/connect?return_to=${encodeURIComponent(window.location.pathname)}`,
              );
            }}
          >
            {service.connected ? "Reconnect" : "Connect"}
          </Button>
        )}
        {(service.auth === "github" || service.auth === "databricks") && (
          <p className="text-sm text-muted-foreground">
            Uses your shared {service.auth === "github" ? "GitHub" : "Databricks"} connection.{" "}
            <a className="underline" href={withBasePath("/settings/integrations")}>
              Manage account in Sandbox Integrations
            </a>
          </p>
        )}
        {service.connected && (
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                const result = await registryRequest<{ tools: string[] }>(`/${service.id}/test`, {
                  method: "POST",
                });
                setNotice(
                  `Tool discovery succeeded. Tools: ${result.tools.join(", ") || "none"}. Individual calls may require additional permissions.`,
                );
              })
            }
          >
            Test connection
          </Button>
        )}
        {service.connected && (service.auth === "bearer" || service.auth === "oauth") && (
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                await registryRequest(`/${service.id}/connection`, { method: "DELETE" });
                setToken("");
                setEditing(false);
                await onChanged();
              })
            }
          >
            Disconnect
          </Button>
        )}
      </div>
      {editing && (
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            void run(async () => {
              await registryRequest(`/${service.id}/connection`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ token }),
              });
              setToken("");
              setEditing(false);
              await onChanged();
            });
          }}
        >
          <Input
            type="password"
            aria-label={`Token for ${service.title}`}
            autoComplete="off"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="Personal access token"
          />
          <Button size="sm" disabled={busy || !token.trim()} type="submit">
            Save token
          </Button>
        </form>
      )}
      {notice && (
        <p role="status" className="text-sm">
          {notice}
        </p>
      )}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}

export function McpRegistryPicker({
  attached,
  reservedNames,
  onToggle,
  busy,
  onAuthorizingChange,
}: {
  attached: string[];
  reservedNames?: string[];
  onToggle: (id: string, enabled: boolean) => void;
  busy: boolean;
  onAuthorizingChange?: (pending: boolean) => void;
}) {
  const { services, error, loading, refresh } = useCatalog();
  const [authorizing, setAuthorizing] = useState<string | null>(null);
  const [connectionError, setConnectionError] = useState<{ id: string; message: string } | null>(
    null,
  );
  const authorization = useRef<AbortController | null>(null);
  const [workspaceService, setWorkspaceService] = useState<string | null>(null);
  const [workspace, setWorkspace] = useState("");
  useEffect(
    () => () => {
      authorization.current?.abort();
      onAuthorizingChange?.(false);
    },
    [onAuthorizingChange],
  );

  async function enable(service: RegistryService, workspaceUrl?: string) {
    setConnectionError(null);
    if (service.connected) {
      onToggle(service.id, true);
      return;
    }
    if (service.auth === "databricks" && !workspaceUrl) {
      setWorkspaceService(service.id);
      return;
    }
    const controller = new AbortController();
    authorization.current = controller;
    setAuthorizing(service.id);
    onAuthorizingChange?.(true);
    try {
      await authorizeRegistryService(service, controller.signal, workspaceUrl);
      setWorkspaceService(null);
      const updated = await refresh();
      if (!updated?.find((s) => s.id === service.id)?.connected) {
        throw new Error("Account is not connected. Please try again.");
      }
      if (!controller.signal.aborted && !attached.includes(service.id)) onToggle(service.id, true);
    } catch (e) {
      if (!controller.signal.aborted) {
        setConnectionError({
          id: service.id,
          message: e instanceof Error ? e.message : "Sign-in failed",
        });
      }
    } finally {
      if (!controller.signal.aborted) {
        setAuthorizing(null);
        onAuthorizingChange?.(false);
      }
    }
  }

  const unavailable = attached.filter((id) => !services.some((s) => s.id === id));
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">
        Choose the services this session can use. Your administrator manages the catalog. You’ll be
        asked to sign in when needed.
      </p>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      {loading && <p className="text-sm text-muted-foreground">Loading MCP services…</p>}
      {!loading && !error && !services.length && (
        <p className="text-sm text-muted-foreground">
          No managed services are available for your account.
        </p>
      )}
      <div className="divide-y divide-border rounded-lg border border-border empty:hidden">
        {services.map((service) => {
          const selected = attached.includes(service.id);
          const tokenNeeded = service.auth === "bearer" && !service.connected;
          const conflict = reservedNames?.includes(service.id);
          return (
            <div key={service.id} className="px-4 py-3">
              <label className="flex items-center gap-3">
                <PlugIcon className="size-4 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-medium">{service.title}</span>
                  {service.description && (
                    <span className="block text-xs text-muted-foreground">
                      {service.description}
                    </span>
                  )}
                  <span className="mt-1 block text-xs text-muted-foreground">
                    {conflict
                      ? "A custom server already uses this name"
                      : authorizing === service.id
                        ? "Waiting for sign-in…"
                        : service.connected
                          ? service.auth === "none"
                            ? "Ready"
                            : "Connected"
                          : tokenNeeded
                            ? "Connect your account in settings"
                            : "Sign in to connect"}
                  </span>
                </span>
                <input
                  type="checkbox"
                  aria-label={service.title}
                  className="size-4 shrink-0 cursor-pointer accent-primary disabled:cursor-not-allowed disabled:opacity-50"
                  checked={selected}
                  disabled={busy || authorizing !== null || conflict || (tokenNeeded && !selected)}
                  onChange={(e) =>
                    e.target.checked ? void enable(service) : onToggle(service.id, false)
                  }
                />
              </label>
              {selected && !service.connected && !tokenNeeded && (
                <Button
                  size="sm"
                  variant="link"
                  disabled={busy || authorizing !== null}
                  onClick={() => void enable(service)}
                >
                  Reconnect
                </Button>
              )}
              {workspaceService === service.id && (
                <div className="mt-3 space-y-2">
                  <Input
                    aria-label="Databricks workspace URL"
                    placeholder="https://your-workspace.cloud.databricks.com"
                    value={workspace}
                    onChange={(e) => setWorkspace(e.target.value)}
                    disabled={busy || authorizing !== null}
                  />
                  <Button
                    size="sm"
                    disabled={busy || authorizing !== null || !workspace.trim()}
                    onClick={() => void enable(service, workspace.trim())}
                  >
                    Connect workspace
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={authorizing !== null}
                    onClick={() => setWorkspaceService(null)}
                  >
                    Cancel
                  </Button>
                </div>
              )}
              {connectionError?.id === service.id && (
                <p role="alert" className="mt-2 text-sm text-destructive">
                  {connectionError.message}
                </p>
              )}
            </div>
          );
        })}
        {!loading &&
          !error &&
          unavailable.map((id) => (
            <label key={id} className="flex items-center justify-between gap-3 px-4 py-3 text-sm">
              <span>
                {id}
                <span className="block text-xs text-muted-foreground">
                  No longer available · uncheck to remove
                </span>
              </span>
              <input
                type="checkbox"
                aria-label={id}
                checked
                disabled={busy || authorizing !== null}
                onChange={() => onToggle(id, false)}
                className="size-4 accent-primary"
              />
            </label>
          ))}
      </div>
      <a
        className="text-xs underline"
        href={withBasePath("/settings/mcp")}
        target="_blank"
        rel="noopener noreferrer"
      >
        Manage MCP accounts
      </a>
    </div>
  );
}
