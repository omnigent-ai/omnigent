import { useEffect, useState } from "react";
import { officeApi } from "./api.ts";
import { useRuntimeOptions } from "./capabilities.ts";

interface HostFolderFieldsProps {
  hostId: string;
  workspace: string;
  onHostChange: (hostId: string) => void;
  onWorkspaceChange: (workspace: string) => void;
}

function parentOf(path: string): string {
  const trimmed = path.replace(/\/+$/, "");
  const cut = trimmed.lastIndexOf("/");
  return cut <= 0 ? "/" : trimmed.slice(0, cut);
}

/** Host select plus a folder field that can browse the host's directories. */
export function HostFolderFields({ hostId, workspace, onHostChange, onWorkspaceChange }: HostFolderFieldsProps) {
  const options = useRuntimeOptions();
  const hosts = (options?.hosts.hosts ?? []).filter((host) => !host.sandbox_provider);
  const [browsePath, setBrowsePath] = useState<string | null>(null);
  const [listing, setListing] = useState<{ path: string; directories: { name: string; path: string }[] } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (browsePath === null || !hostId) return;
    let cancelled = false;
    setError(null);
    officeApi
      .hostDirectories(hostId, browsePath)
      .then((result) => {
        if (cancelled) return;
        // Entry paths are absolute, so they also resolve "~" to the real folder.
        const resolved = result.directories[0] ? parentOf(result.directories[0].path) : browsePath;
        setListing({ path: resolved, directories: result.directories.filter((entry) => !entry.name.startsWith(".")) });
      })
      .catch((caught: Error) => !cancelled && setError(caught.message));
    return () => {
      cancelled = true;
    };
  }, [browsePath, hostId]);

  const closeBrowser = () => {
    setBrowsePath(null);
    setListing(null);
  };

  return (
    <>
      <label>
        Host{" "}
        <select
          value={hostId}
          onChange={(event) => {
            onHostChange(event.target.value);
            closeBrowser();
          }}
        >
          <option value="">{hosts.length ? "Choose…" : "No hosts connected"}</option>
          {hosts.map((host) => (
            <option key={host.host_id} value={host.host_id}>
              {host.name ?? host.host_id} ({host.status ?? "unknown"})
            </option>
          ))}
        </select>
      </label>
      <label>
        Folder on the host{" "}
        <input value={workspace} placeholder="/path/to/repo" disabled={!hostId} onChange={(event) => onWorkspaceChange(event.target.value)} />
      </label>
      <button type="button" disabled={!hostId} onClick={() => (browsePath === null ? setBrowsePath(workspace.trim() || "~") : closeBrowser())}>
        {browsePath === null ? "Browse folders…" : "Close folder browser"}
      </button>
      {browsePath !== null ? (
        <div className="folder-browser" role="group" aria-label="Folder browser">
          {error ? (
            <p className="error-text" role="alert">
              {error}
            </p>
          ) : null}
          {listing ? (
            <>
              <p className="small">
                <code>{listing.path}</code>
              </p>
              <div className="dialog-actions">
                <button type="button" disabled={listing.path === "/"} onClick={() => setBrowsePath(parentOf(listing.path))}>
                  Up one level
                </button>
                <button
                  type="button"
                  className="primary"
                  disabled={listing.path.startsWith("~")}
                  onClick={() => {
                    onWorkspaceChange(listing.path);
                    closeBrowser();
                  }}
                >
                  Use this folder
                </button>
              </div>
              <ul className="folder-list">
                {listing.directories.map((entry) => (
                  <li key={entry.path}>
                    <button type="button" onClick={() => setBrowsePath(entry.path)}>
                      📁 {entry.name}
                    </button>
                  </li>
                ))}
                {listing.directories.length === 0 ? <li className="muted small">No subfolders</li> : null}
              </ul>
            </>
          ) : error ? null : (
            <p className="muted small">Loading…</p>
          )}
        </div>
      ) : null}
    </>
  );
}
