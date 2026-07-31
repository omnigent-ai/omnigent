import { useEffect, useMemo, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { importAgentFromGit, type AgentObject } from "@/lib/agentsApi";
import { useHosts } from "@/hooks/useHosts";

/**
 * Dialog for importing a custom agent from a git repository.
 *
 * Accepts a Repo URL (required), Host (required — git clone runs on the
 * selected host), Branch (optional), and Path in repo (optional). No
 * "Display name" field — the agent name is derived server-side from the
 * spec found at the given path.
 */
export function ImportAgentFromGitDialog({
  open,
  onOpenChange,
  onImported,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onImported: (agent: AgentObject) => void;
}) {
  const [repoUrl, setRepoUrl] = useState("");
  const [branch, setBranch] = useState("");
  const [subpath, setSubpath] = useState("");
  const [hostId, setHostId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: hosts } = useHosts();
  // Memoized so the array reference is stable across renders — otherwise the
  // preselect effect below re-runs every render (its dep would change each time).
  const onlineHosts = useMemo(() => (hosts ?? []).filter((h) => h.status === "online"), [hosts]);

  // Preselect the only online host when the list loads and no choice has been made.
  useEffect(() => {
    if (onlineHosts.length === 1 && !hostId) {
      setHostId(onlineHosts[0]!.host_id);
    }
  }, [onlineHosts, hostId]);

  function handleOpenChange(next: boolean): void {
    if (!next) {
      setRepoUrl("");
      setBranch("");
      setSubpath("");
      setHostId("");
      setError(null);
      setSubmitting(false);
    }
    onOpenChange(next);
  }

  async function handleSubmit(): Promise<void> {
    const trimmedUrl = repoUrl.trim();
    if (!trimmedUrl || !hostId) return;

    setSubmitting(true);
    setError(null);
    try {
      const agent = await importAgentFromGit({
        gitUrl: trimmedUrl,
        gitRef: branch.trim() || undefined,
        gitSubpath: subpath.trim() || undefined,
        hostId,
      });
      onImported(agent);
      handleOpenChange(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to import agent. Try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        data-testid="import-agent-dialog"
        className="flex max-h-[85vh] flex-col gap-4 sm:max-w-lg"
      >
        <DialogHeader>
          <DialogTitle>Import agent from git</DialogTitle>
        </DialogHeader>

        <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto">
          <div className="flex flex-col gap-1.5">
            <label
              htmlFor="import-agent-host"
              className="text-xs font-medium text-muted-foreground"
            >
              Host
            </label>
            {onlineHosts.length === 0 ? (
              <p className="text-xs text-muted-foreground">Connect a host to import from git</p>
            ) : (
              <select
                id="import-agent-host"
                value={hostId}
                onChange={(e) => setHostId(e.target.value)}
                className="flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
              >
                <option value="">Select a host…</option>
                {onlineHosts.map((h) => (
                  <option key={h.host_id} value={h.host_id}>
                    {h.name}
                  </option>
                ))}
              </select>
            )}
          </div>

          <div className="flex flex-col gap-1.5">
            <label
              htmlFor="import-agent-repo-url"
              className="text-xs font-medium text-muted-foreground"
            >
              Repository URL
            </label>
            <Input
              id="import-agent-repo-url"
              type="url"
              value={repoUrl}
              onChange={(e) => setRepoUrl(e.target.value)}
              placeholder="https://github.com/org/repo"
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <label
              htmlFor="import-agent-branch"
              className="text-xs font-medium text-muted-foreground"
            >
              Branch
            </label>
            <Input
              id="import-agent-branch"
              type="text"
              value={branch}
              onChange={(e) => setBranch(e.target.value)}
              placeholder="default branch"
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <label
              htmlFor="import-agent-subpath"
              className="text-xs font-medium text-muted-foreground"
            >
              Path in repo
            </label>
            <Input
              id="import-agent-subpath"
              type="text"
              value={subpath}
              onChange={(e) => setSubpath(e.target.value)}
              placeholder="path in repo (optional)"
            />
          </div>

          {error !== null && (
            <p role="alert" aria-live="assertive" className="text-xs text-destructive">
              {error}
            </p>
          )}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => handleOpenChange(false)} disabled={submitting}>
            Cancel
          </Button>
          <Button
            data-testid="import-agent-submit"
            onClick={handleSubmit}
            disabled={!repoUrl.trim() || !hostId || submitting}
          >
            {submitting ? "Importing…" : "Import"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
