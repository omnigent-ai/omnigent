import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { withBasePath } from "@/lib/basePath";
import { registryRequest, type RegistryService } from "@/lib/mcpRegistry";

function useCatalog() {
  const [services, setServices] = useState<RegistryService[]>([]);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    try {
      const result = await registryRequest<{ data: RegistryService[] }>();
      setServices(result.data);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load MCP services");
    }
  }, []);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  return { services, error, refresh };
}

export function McpRegistryConnections() {
  const { services, error, refresh } = useCatalog();
  const callbackFailed = [...new URLSearchParams(window.location.search)].some(
    ([key, value]) => key.startsWith("mcp-") && value === "error",
  );
  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-sm font-medium">Managed MCP services</h3>
        <p className="text-sm text-muted-foreground">
          Connect once, then add these tools to any session. Your accounts are reused across
          sessions and sandboxes in this workspace.
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
        <ServiceConnection key={service.id} service={service} onChanged={refresh} />
      ))}
      {!services.length && !error && (
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
        {(service.auth === "github" || service.auth === "databricks") && !service.connected && (
          <p className="text-sm text-muted-foreground">
            Connect {service.auth === "github" ? "GitHub" : "Databricks"} using the account controls
            on this page.
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
                setNotice(`Connection works. Tools: ${result.tools.join(", ") || "none"}`);
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
  onAdd,
  busy,
}: {
  attached: string[];
  onAdd: (id: string) => void;
  busy: boolean;
}) {
  const { services, error } = useCatalog();
  return (
    <div className="space-y-2 rounded-md border border-border p-3">
      <p className="text-sm font-medium">Add a managed MCP service</p>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      {services
        .filter((s) => !attached.includes(s.id))
        .map((service) => (
          <div key={service.id} className="flex items-center justify-between gap-2">
            <span className="text-sm">{service.title}</span>
            <Button
              size="sm"
              variant="outline"
              disabled={busy || !service.connected}
              onClick={() => onAdd(service.id)}
            >
              {service.connected ? `Add ${service.title}` : "Connect in settings"}
            </Button>
          </div>
        ))}
      <a className="text-xs underline" href={withBasePath("/settings/integrations")}>
        Manage account connections
      </a>
    </div>
  );
}
