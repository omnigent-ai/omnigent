import { useCallback, useEffect, useState } from "react";
import type { ProjectLink } from "../../shared/projectLinks.ts";
import { roleTitle } from "../../shared/sdlcTheme.ts";
import { officeApi } from "../api.ts";

const LINKS_POLL_MS = 15_000;

/** The project's results: its app, served by the office, and links from the PO and the roles. */
export function ProjectLinks({ projectId, name }: { projectId: string; name: string }) {
  const [links, setLinks] = useState<ProjectLink[]>([]);
  const [app, setApp] = useState<{ url: string; folder: string } | null>(null);
  const [adding, setAdding] = useState(false);
  const [label, setLabel] = useState("");
  const [url, setUrl] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    () =>
      officeApi
        .roomLinks(projectId)
        .then((result) => {
          setLinks(result.links);
          setApp(result.app);
        })
        .catch(() => undefined),
    [projectId],
  );

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), LINKS_POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const add = async () => {
    setError(null);
    try {
      await officeApi.addRoomLink(projectId, label, url);
      setLabel("");
      setUrl("");
      setAdding(false);
      await load();
    } catch (caught) {
      setError((caught as Error).message);
    }
  };

  const remove = async (link: ProjectLink) => {
    setError(null);
    try {
      await officeApi.removeRoomLink(projectId, link.id);
      await load();
    } catch (caught) {
      setError((caught as Error).message);
    }
  };

  return (
    <section className="project-links" aria-label="Project links">
      {app ? (
        <a className="project-open" href={app.url} target="_blank" rel="noopener noreferrer" title={`Served by the office from ${app.folder || "the project folder"}`}>
          ▶ Open {name}
        </a>
      ) : null}
      {links.map((link) => (
        <span key={link.id} className="project-link">
          <a href={link.url} target="_blank" rel="noopener noreferrer" title={link.description ?? link.url}>
            {link.label}
          </a>
          <span className="muted small"> · {link.addedBy === "po" ? "you" : roleTitle(link.addedBy)}</span>
          <button type="button" className="link-remove" aria-label={`Remove the link ${link.label}`} onClick={() => void remove(link)}>
            ×
          </button>
        </span>
      ))}
      {!app && links.length === 0 && !adding ? (
        <span className="muted small">No links yet. Roles share results here, such as a deployment or a pull request.</span>
      ) : null}
      {adding ? (
        <form
          className="project-link-form"
          onSubmit={(event) => {
            event.preventDefault();
            void add();
          }}
        >
          <input aria-label="Link label" placeholder="Label" value={label} maxLength={80} onChange={(event) => setLabel(event.target.value)} required />
          <input aria-label="Link URL" placeholder="https://…" type="url" value={url} onChange={(event) => setUrl(event.target.value)} required />
          <button type="submit" className="primary">
            Add
          </button>
          <button type="button" onClick={() => setAdding(false)}>
            Cancel
          </button>
        </form>
      ) : (
        <button type="button" className="link-button" onClick={() => setAdding(true)}>
          + Add link
        </button>
      )}
      {error ? (
        <span className="error-text small" role="alert">
          {error}
        </span>
      ) : null}
    </section>
  );
}
