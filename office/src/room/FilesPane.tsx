import { useEffect, useState } from "react";
import { type WorkspaceEntry, officeApi } from "../api.ts";
import { fileSize } from "./FileViewer.tsx";

interface Listing {
  workspace: string;
  path: string;
  entries: WorkspaceEntry[];
  hasMore: boolean;
}

/** Browse the room's workspace folder on its host; a file opens as a tab next to the office. */
export function FilesPane({
  projectId,
  hasFolder,
  onOpenFile,
}: {
  projectId: string;
  hasFolder: boolean;
  /** Opens the file as a tab next to the office. */
  onOpenFile: (path: string) => void;
}) {
  const [path, setPath] = useState("");
  const [listing, setListing] = useState<Listing | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    if (!hasFolder) return;
    let cancelled = false;
    setError(null);
    officeApi
      .roomFiles(projectId, path)
      .then((next) => !cancelled && setListing(next))
      .catch((caught: Error) => !cancelled && setError(caught.message));
    return () => {
      cancelled = true;
    };
  }, [projectId, path, hasFolder, reload]);

  if (!hasFolder) return <p className="muted">This room has no host folder, so there are no files to show.</p>;

  const crumbs = path ? path.split("/") : [];
  return (
    <div className="files-pane">
      <nav aria-label="Folder" className="file-crumbs">
        <button type="button" className="link-button" onClick={() => setPath("")}>
          {listing?.workspace.split("/").pop() || "workspace"}
        </button>
        {crumbs.map((crumb, index) => (
          <span key={`${index}:${crumb}`}>
            {" / "}
            <button type="button" className="link-button" onClick={() => setPath(crumbs.slice(0, index + 1).join("/"))}>
              {crumb}
            </button>
          </span>
        ))}
        <button type="button" className="file-refresh" aria-label="Refresh files" onClick={() => setReload((count) => count + 1)}>
          ↻
        </button>
      </nav>
      {listing ? <p className="muted small file-root">{listing.workspace}</p> : null}
      {error ? (
        <p className="error-text small" role="alert">
          {error}
        </p>
      ) : null}
      <ul className="file-list">
        {path ? (
          <li>
            <button type="button" onClick={() => setPath(crumbs.slice(0, -1).join("/"))}>
              <span aria-hidden="true">↰</span> ..
            </button>
          </li>
        ) : null}
        {(listing?.entries ?? []).map((entry) => (
          <li key={entry.path}>
            <button type="button" onClick={() => (entry.type === "directory" ? setPath(entry.path) : onOpenFile(entry.path))}>
              <span aria-hidden="true">{entry.type === "directory" ? "📁" : "📄"}</span> {entry.name}
              {entry.type === "file" ? <span className="muted small"> {fileSize(entry.bytes)}</span> : null}
            </button>
          </li>
        ))}
        {listing && listing.entries.length === 0 ? <li className="muted">Empty folder.</li> : null}
        {listing?.hasMore ? <li className="muted small">More entries not shown.</li> : null}
      </ul>
    </div>
  );
}
