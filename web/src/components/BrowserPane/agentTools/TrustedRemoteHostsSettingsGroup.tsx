import { useCallback, useEffect, useState } from "react";
import { SettingsGroup } from "@/components/SettingsGroup";
import { SettingsLabel } from "@/components/SettingsLabel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { TRUSTED_REMOTE_HOSTS_ENTRY } from "@/pages/settings/search/catalog/general";
import { SettingsItem } from "@/pages/settings/search/SettingsItem";
import {
  settingsControlProps,
  settingsDescriptionId,
  settingsLabelId,
} from "@/pages/settings/search/settingsControl";
import {
  getDesktopFeatures,
  supportsBrowser,
  trustedRemoteHostsBridge,
  type TrustedRemoteHost,
} from "@/lib/nativeBridge";
import { UPDATE_DESKTOP_NOTE } from "./BrowserAgentToolsSettingsGroup";

/** Settings > General > Browser: remote hosts trusted for localhost previews.
 *  Desktop apps with the embedded browser only; the shell owns the list in
 *  settings.json, so this reads and writes it through the bridge. A host id
 *  is stable across restarts, so a grant normally needs no renewal; removing
 *  one revokes it on the session's next browser operation. */
export function TrustedRemoteHostsSettingsGroup() {
  const bridge = trustedRemoteHostsBridge();
  const [supported, setSupported] = useState<boolean | null>(null);
  const [hosts, setHosts] = useState<TrustedRemoteHost[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [serverUrl, setServerUrl] = useState("");
  const [hostId, setHostId] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!bridge) {
      setSupported(false);
      return;
    }
    let cancelled = false;
    void getDesktopFeatures()
      .then(async (features) => {
        if (cancelled) return;
        if (features?.trustedRemoteLocalhostPreview !== true) {
          setSupported(false);
          return;
        }
        setSupported(true);
        const result = await bridge.list();
        if (!cancelled) {
          if (result.ok) setHosts(result.hosts ?? []);
          else setError(result.error ?? "Could not load trusted hosts.");
        }
      })
      .catch(() => {
        if (!cancelled) setSupported(false);
      });
    return () => {
      cancelled = true;
    };
    // The bridge is a fresh object per render; the shell behind it is stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const remove = useCallback(
    async (host: TrustedRemoteHost) => {
      if (!bridge) return;
      setError(null);
      const result = await bridge.remove(host.server_url, host.host_id);
      if (result.ok) setHosts(result.hosts ?? []);
      else setError(result.error ?? "Could not remove that trusted host.");
    },
    [bridge],
  );

  const add = useCallback(async () => {
    if (!bridge || busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await bridge.add(serverUrl.trim(), hostId.trim());
      if (result.ok) {
        setHosts(result.hosts ?? []);
        setServerUrl("");
        setHostId("");
      } else {
        setError(result.error ?? "Could not trust that host.");
      }
    } finally {
      setBusy(false);
    }
  }, [bridge, busy, serverUrl, hostId]);

  if (!supportsBrowser()) return null;
  return (
    <SettingsGroup title="Trusted remote hosts" testId="settings-group-trusted-remote-hosts">
      <SettingsItem entry={TRUSTED_REMOTE_HOSTS_ENTRY}>
        <SettingsLabel
          label={TRUSTED_REMOTE_HOSTS_ENTRY.label}
          labelId={settingsLabelId(TRUSTED_REMOTE_HOSTS_ENTRY)}
          descriptionId={settingsDescriptionId(TRUSTED_REMOTE_HOSTS_ENTRY)}
          description={
            supported === false
              ? UPDATE_DESKTOP_NOTE
              : "Sessions on a trusted remote host can open localhost pages in the in-app browser. Trust stays until removed, and a host id is stable across restarts — only a brand-new host needs a new entry. The host's port must already be forwarded to this computer."
          }
        />
        {supported === true && (
          <div className="mt-4 flex flex-col gap-4">
            {hosts.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No remote hosts trusted yet. Trust one from a blocked localhost preview, or add it
                here.
              </p>
            ) : (
              <ul className="divide-y divide-border">
                {hosts.map((host) => (
                  <li
                    key={`${host.server_url}:${host.host_id}`}
                    className="flex items-start justify-between gap-4 py-2.5"
                  >
                    <div className="min-w-0">
                      <p className="break-all text-sm font-medium">{host.server_url}</p>
                      <p className="mt-0.5 break-all font-mono text-xs text-muted-foreground">
                        {host.host_id}
                      </p>
                    </div>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      {...settingsControlProps(TRUSTED_REMOTE_HOSTS_ENTRY, "remove")}
                      aria-label={`Remove trusted host ${host.server_url} ${host.host_id}`}
                      onClick={() => void remove(host)}
                      className="shrink-0 text-muted-foreground"
                      componentId="settings.general.trusted_remote_hosts.remove"
                    >
                      Remove
                    </Button>
                  </li>
                ))}
              </ul>
            )}
            <div className="flex flex-col gap-2 border-t border-border pt-4">
              <div className="flex flex-col gap-2 sm:flex-row">
                <Input
                  type="url"
                  placeholder="Server URL, e.g. https://server.example.com"
                  {...settingsControlProps(TRUSTED_REMOTE_HOSTS_ENTRY, "serverUrl")}
                  data-testid="trusted-remote-host-server-url"
                  className="min-w-0 flex-1"
                  value={serverUrl}
                  onChange={(event) => setServerUrl(event.target.value)}
                />
                <Input
                  placeholder="Host ID"
                  {...settingsControlProps(TRUSTED_REMOTE_HOSTS_ENTRY, "hostId")}
                  data-testid="trusted-remote-host-id"
                  className="min-w-0 flex-1 font-mono"
                  value={hostId}
                  onChange={(event) => setHostId(event.target.value)}
                />
                <Button
                  type="button"
                  {...settingsControlProps(TRUSTED_REMOTE_HOSTS_ENTRY, "add")}
                  disabled={!serverUrl.trim() || !hostId.trim() || busy}
                  onClick={() => void add()}
                  data-testid="trusted-remote-host-add"
                  componentId="settings.general.trusted_remote_hosts.add"
                >
                  Trust host
                </Button>
              </div>
              {error && (
                <p role="alert" className="text-sm text-destructive">
                  {error}
                </p>
              )}
            </div>
          </div>
        )}
      </SettingsItem>
    </SettingsGroup>
  );
}
