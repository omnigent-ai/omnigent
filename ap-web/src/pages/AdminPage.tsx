/**
 * GTM control-plane Admin page (``/admin``).
 *
 * One page, four capability-gated sections, all backed by the
 * ``/v1/control-plane/*`` API (see ``lib/controlPlaneApi.ts``):
 *
 * 1. **Role** (everyone) — the caller's resolved role, groups, and
 *    capability flags. A consumer sees only this, with a note that
 *    publishing / management need contributor or admin.
 * 2. **Agent visibility** (``can_manage_visibility`` ‖ ``can_view_usage``)
 *    — the agent catalog with per-row "Edit visibility" → a dialog with
 *    an org/restricted toggle + an {@link AudienceEditor}.
 * 3. **Publish** (``can_publish``) — promote a session-scoped agent into
 *    the shared catalog. Hidden entirely for consumers.
 * 4. **Usage** (``can_view_usage``) — per-agent cost / tokens / sessions
 *    with a totals row; a row expands to its per-user breakdown.
 *
 * Admin-only **audit** log sits below the sections (admin sees it; the
 * server 403s for everyone else and we just hide it).
 *
 * Gating is client-side UX only — the server enforces every capability.
 * The control plane is OPTIONAL: it only exists on the Databricks Apps
 * deploy. When ``GET /me`` 404s or network-fails, we render a clean
 * "not available in this deployment" state, never a white screen.
 */

import { useCallback, useEffect, useState } from "react";
import {
  BarChart3Icon,
  CheckIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  EyeIcon,
  PlugZapIcon,
  ScrollTextIcon,
  ShieldCheckIcon,
  Trash2Icon,
  UploadIcon,
  XIcon,
} from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  type AgentVisibility,
  type Audience,
  type ControlPlaneMe,
  type ManagedAgent,
  type PublishableAgent,
  type UsageReport,
  type AuditEntry,
  type AgentTestResult,
  getControlPlaneMe,
  listControlPlaneAgents,
  setAgentVisibility,
  listPublishable,
  publishAgent,
  getUsage,
  getAudit,
  deleteControlPlaneAgent,
  testAgent,
} from "@/lib/controlPlaneApi";
import { cn } from "@/lib/utils";

// Shared error-banner classes (mirrors MembersPage).
const ALERT_CLASS =
  "rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive";

export function AdminPage() {
  // ``undefined`` = probe in flight; ``null`` = control plane absent
  // (404 / network); otherwise the resolved identity.
  const [me, setMe] = useState<ControlPlaneMe | null | undefined>(undefined);
  // Bumped to invalidate the data sections (Agents + Usage) — e.g. after a
  // publish or a delete — so they re-fetch without a full reload.
  const [refreshToken, setRefreshToken] = useState(0);
  const invalidate = useCallback(() => setRefreshToken((n) => n + 1), []);

  useEffect(() => {
    void (async () => {
      const result = await getControlPlaneMe();
      // 404 (not deployed) / network failure → graceful "unavailable".
      // A 401 also lands here; identity.ts already handles the redirect.
      setMe(result.ok ? result.me : null);
    })();
  }, []);

  // Probe in flight.
  if (me === undefined) {
    return (
      <div className="flex min-h-full items-center justify-center text-sm text-muted-foreground">
        Loading…
      </div>
    );
  }

  // Control plane not part of this deployment (OSS / header / OIDC) —
  // or the probe failed. Render a clean, friendly state.
  if (me === null) {
    return (
      <PageScroll contentClassName="px-6">
        <h1 className="mb-2 text-2xl font-semibold">Admin</h1>
        <p className="text-sm text-muted-foreground">
          The GTM control plane isn't available in this deployment.
        </p>
      </PageScroll>
    );
  }

  const caps = me.capabilities;
  // Visibility table shows for admin/contributor (manage OR view usage).
  const showAgents = caps.can_manage_visibility || caps.can_view_usage;

  return (
    <PageScroll contentClassName="px-6">
      <div className="mb-6 flex items-center gap-2">
        <ShieldCheckIcon className="size-6 text-muted-foreground" />
        <h1 className="text-2xl font-semibold">Admin</h1>
      </div>

      <div className="flex flex-col gap-10">
        <RoleSection me={me} />
        {showAgents && (
          <AgentsSection
            canManageVisibility={caps.can_manage_visibility}
            refreshToken={refreshToken}
            onChanged={invalidate}
          />
        )}
        {caps.can_publish && <PublishSection onPublished={invalidate} />}
        {caps.can_view_usage && <UsageSection refreshToken={refreshToken} />}
        {caps.can_manage_all && <AuditSection />}
      </div>
    </PageScroll>
  );
}

// ── Section shell ──────────────────────────────────────────────────

function SectionHeader({
  icon: Icon,
  title,
  action,
}: {
  icon: typeof ShieldCheckIcon;
  title: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="mb-3 flex items-center justify-between">
      <h2 className="flex items-center gap-2 text-lg font-semibold">
        <Icon className="size-4 text-muted-foreground" />
        {title}
      </h2>
      {action}
    </div>
  );
}

// Shared table chrome (mirrors MembersPage's hand-rolled table).
function Table({ children }: { children: React.ReactNode }) {
  return (
    <div className="overflow-hidden rounded-md border border-border">
      <table className="w-full text-sm">{children}</table>
    </div>
  );
}

// ── 1. Role ────────────────────────────────────────────────────────

function roleBadgeVariant(role: ControlPlaneMe["role"]) {
  if (role === "admin") return "default" as const;
  if (role === "contributor") return "secondary" as const;
  return "outline" as const;
}

function RoleSection({ me }: { me: ControlPlaneMe }) {
  const caps = me.capabilities;
  const capList: { key: keyof typeof caps; label: string }[] = [
    { key: "can_publish", label: "Publish agents" },
    { key: "can_manage_visibility", label: "Manage visibility" },
    { key: "can_view_usage", label: "View usage" },
    { key: "can_manage_all", label: "Manage all (admin)" },
  ];

  return (
    <section>
      <SectionHeader icon={ShieldCheckIcon} title="Your access" />
      <div className="flex flex-col gap-4 rounded-md border border-border p-4">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <span className="font-medium">{me.user_id}</span>
          <Badge variant={roleBadgeVariant(me.role)} className="capitalize">
            {me.role}
          </Badge>
          {me.is_platform_admin && <Badge variant="outline">Platform admin</Badge>}
        </div>

        <div>
          <div className="mb-1 text-xs uppercase text-muted-foreground">Groups</div>
          {me.groups.length === 0 ? (
            <span className="text-sm text-muted-foreground">No groups</span>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {me.groups.map((g) => (
                <Badge key={g} variant="outline">
                  {g}
                </Badge>
              ))}
            </div>
          )}
        </div>

        <div>
          <div className="mb-1 text-xs uppercase text-muted-foreground">Capabilities</div>
          <div className="flex flex-wrap gap-1.5">
            {capList.map(({ key, label }) => (
              <Badge key={key} variant={caps[key] ? "secondary" : "outline"}>
                <span className={cn(!caps[key] && "text-muted-foreground line-through")}>
                  {label}
                </span>
              </Badge>
            ))}
          </div>
        </div>

        {me.role === "consumer" && (
          <p className="text-sm text-muted-foreground">
            Publishing agents and managing visibility require the contributor or admin role. Ask a
            platform admin if you need access.
          </p>
        )}
      </div>
    </section>
  );
}

// ── Reusable audience editor ───────────────────────────────────────

/** Split a comma/newline-separated text blob into trimmed, non-empty values. */
function parseList(text: string): string[] {
  return text
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

/**
 * Two text areas (users by email, groups by name) that read/write the
 * {@link Audience} shape. Used by both the visibility dialog and the
 * publish dialog. Values are comma- or newline-separated. The parent
 * owns the parsed arrays; this is a controlled component over text
 * buffers so editing stays natural (trailing commas etc.).
 */
function AudienceEditor({
  users,
  groups,
  onChange,
  disabled,
}: {
  users: string;
  groups: string;
  onChange: (next: { users: string; groups: string }) => void;
  disabled?: boolean;
}) {
  return (
    <div className="flex flex-col gap-3">
      <label className="flex flex-col gap-1">
        <span className="text-xs font-medium text-muted-foreground">
          Users (emails, comma or newline separated)
        </span>
        <textarea
          value={users}
          disabled={disabled}
          onChange={(e) => onChange({ users: e.target.value, groups })}
          rows={2}
          placeholder="alice@databricks.com, bob@databricks.com"
          className="w-full rounded-lg border border-input bg-transparent px-2.5 py-1.5 text-sm outline-none transition-colors placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-50 dark:bg-input/30"
        />
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-xs font-medium text-muted-foreground">
          Groups (names, comma or newline separated)
        </span>
        <textarea
          value={groups}
          disabled={disabled}
          onChange={(e) => onChange({ users, groups: e.target.value })}
          rows={2}
          placeholder="fsi-team, field-eng"
          className="w-full rounded-lg border border-input bg-transparent px-2.5 py-1.5 text-sm outline-none transition-colors placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-50 dark:bg-input/30"
        />
      </label>
    </div>
  );
}

/** A small org/restricted segmented toggle, shared by both dialogs. */
function VisibilityToggle({
  value,
  onChange,
  disabled,
}: {
  value: AgentVisibility;
  onChange: (v: AgentVisibility) => void;
  disabled?: boolean;
}) {
  const options: { value: AgentVisibility; label: string }[] = [
    { value: "org", label: "Whole org" },
    { value: "restricted", label: "Restricted" },
  ];
  return (
    <div className="inline-flex rounded-lg border border-border p-0.5" role="radiogroup">
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          role="radio"
          aria-checked={value === opt.value}
          disabled={disabled}
          onClick={() => onChange(opt.value)}
          className={cn(
            "rounded-md px-3 py-1 text-sm transition-colors disabled:opacity-50",
            value === opt.value
              ? "bg-muted font-medium text-foreground"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}

/** One-line summary of an agent's audience for the table cell. */
function audienceSummary(agent: ManagedAgent): string {
  if (agent.visibility === "org") return "Everyone in org";
  const parts: string[] = [];
  if (agent.audience.users.length > 0) {
    parts.push(`${agent.audience.users.length} user${agent.audience.users.length === 1 ? "" : "s"}`);
  }
  if (agent.audience.groups.length > 0) {
    parts.push(
      `${agent.audience.groups.length} group${agent.audience.groups.length === 1 ? "" : "s"}`,
    );
  }
  return parts.length > 0 ? parts.join(", ") : "No one yet";
}

// ── 2. Agent visibility ────────────────────────────────────────────

function AgentsSection({
  canManageVisibility,
  refreshToken,
  onChanged,
}: {
  canManageVisibility: boolean;
  refreshToken: number;
  onChanged: () => void;
}) {
  const [agents, setAgents] = useState<ManagedAgent[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [editing, setEditing] = useState<ManagedAgent | null>(null);
  const [deleting, setDeleting] = useState<ManagedAgent | null>(null);
  const [testing, setTesting] = useState<ManagedAgent | null>(null);

  const refresh = useCallback(async () => {
    const result = await listControlPlaneAgents();
    if (!result.ok) {
      setLoadError(result.error);
      setAgents([]);
      return;
    }
    setLoadError(null);
    setAgents(result.agents);
  }, []);

  // Re-fetch on mount and whenever the page invalidates (publish/delete).
  useEffect(() => {
    void refresh();
  }, [refresh, refreshToken]);

  return (
    <section>
      <SectionHeader icon={EyeIcon} title="Agent visibility" />
      {loadError !== null && (
        <div role="alert" className={cn(ALERT_CLASS, "mb-3")}>
          {loadError}
        </div>
      )}
      {agents === null ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : agents.length === 0 && loadError === null ? (
        <p className="text-sm text-muted-foreground">No agents yet.</p>
      ) : agents.length > 0 ? (
        <Table>
          <thead className="bg-muted/40 text-left text-xs uppercase text-muted-foreground">
            <tr>
              <th className="px-3 py-2 font-medium">Agent</th>
              <th className="px-3 py-2 font-medium">Visibility</th>
              <th className="px-3 py-2 font-medium">Audience</th>
              <th className="px-3 py-2 font-medium">Owner</th>
              <th className="px-3 py-2 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {agents.map((agent) => (
              <tr key={agent.id} className="border-t border-border">
                <td className="px-3 py-2 align-middle">
                  <div className="font-medium">{agent.name}</div>
                  {agent.description && (
                    <div className="truncate text-xs text-muted-foreground" title={agent.description}>
                      {agent.description}
                    </div>
                  )}
                </td>
                <td className="px-3 py-2 align-middle">
                  <Badge variant={agent.visibility === "org" ? "secondary" : "outline"}>
                    {agent.visibility === "org" ? "Org" : "Restricted"}
                  </Badge>
                </td>
                <td className="px-3 py-2 align-middle text-muted-foreground">
                  {audienceSummary(agent)}
                </td>
                <td className="px-3 py-2 align-middle text-muted-foreground">{agent.owner_id}</td>
                <td className="px-3 py-2 text-right">
                  <div className="flex items-center justify-end gap-1">
                    {/* Anyone who can see the row can test it (server re-checks). */}
                    <Button variant="ghost" size="xs" onClick={() => setTesting(agent)}>
                      <PlugZapIcon /> Test
                    </Button>
                    {agent.viewer_can_manage && canManageVisibility && (
                      <Button variant="ghost" size="xs" onClick={() => setEditing(agent)}>
                        <EyeIcon /> Edit visibility
                      </Button>
                    )}
                    {agent.viewer_can_manage && (
                      <Button
                        variant="ghost"
                        size="xs"
                        className="text-destructive hover:text-destructive"
                        onClick={() => setDeleting(agent)}
                      >
                        <Trash2Icon /> Delete
                      </Button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      ) : null}

      <VisibilityDialog
        agent={editing}
        onClose={() => setEditing(null)}
        onSaved={() => {
          setEditing(null);
          void refresh();
        }}
      />
      <DeleteAgentDialog
        agent={deleting}
        onClose={() => setDeleting(null)}
        onDeleted={() => {
          setDeleting(null);
          // onChanged() bumps the page refreshToken, which re-runs this
          // section's fetch effect (and Usage's) — no separate refresh().
          onChanged();
        }}
      />
      <TestAgentDialog agent={testing} onClose={() => setTesting(null)} />
    </section>
  );
}

function DeleteAgentDialog({
  agent,
  onClose,
  onDeleted,
}: {
  agent: ManagedAgent | null;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (agent) setError(null);
  }, [agent]);

  return (
    <Dialog open={agent !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete agent</DialogTitle>
          <DialogDescription>
            Delete <span className="font-medium">{agent?.name}</span>? This removes it from the
            shared catalog and cannot be undone.
          </DialogDescription>
        </DialogHeader>
        {error !== null && (
          <div role="alert" className={ALERT_CLASS}>
            {error}
          </div>
        )}
        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            disabled={busy}
            onClick={async () => {
              if (!agent) return;
              setBusy(true);
              setError(null);
              const result = await deleteControlPlaneAgent(agent.id);
              setBusy(false);
              if (result.ok) onDeleted();
              else setError(result.error);
            }}
          >
            {busy ? "Deleting…" : "Delete"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function TestAgentDialog({ agent, onClose }: { agent: ManagedAgent | null; onClose: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AgentTestResult | null>(null);

  // Run the test whenever a new agent opens the dialog.
  useEffect(() => {
    if (!agent) return;
    setBusy(true);
    setError(null);
    setResult(null);
    void (async () => {
      const r = await testAgent(agent.id);
      setBusy(false);
      if (r.ok) setResult(r.result);
      else setError(r.error);
    })();
  }, [agent]);

  return (
    <Dialog open={agent !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Test connection — {agent?.name}</DialogTitle>
          <DialogDescription>
            Checks the saved agent is reachable and launchable: record resolves, bundle present and
            loadable, and the spec validates.
          </DialogDescription>
        </DialogHeader>
        {busy && <p className="text-sm text-muted-foreground">Testing…</p>}
        {error !== null && (
          <div role="alert" className={ALERT_CLASS}>
            {error}
          </div>
        )}
        {result !== null && (
          <div className="flex flex-col gap-2">
            <div className="text-sm font-medium">
              {result.ok ? "✓ Agent is reachable and launchable." : "✗ Some checks failed."}
            </div>
            {(result.harness || result.model) && (
              <div className="text-xs text-muted-foreground">
                harness: {result.harness ?? "—"} · model: {result.model ?? "unset"}
                {result.mcp_server_count != null ? ` · ${result.mcp_server_count} MCP server(s)` : ""}
              </div>
            )}
            <ul className="flex flex-col gap-1">
              {result.checks.map((c) => (
                <li key={c.name} className="flex items-start gap-2 text-sm">
                  {c.ok ? (
                    <CheckIcon className="mt-0.5 size-4 shrink-0 text-green-600" />
                  ) : (
                    <XIcon className="mt-0.5 size-4 shrink-0 text-destructive" />
                  )}
                  <span>
                    <span className="font-medium">{c.name}</span>
                    {c.detail ? <span className="text-muted-foreground"> — {c.detail}</span> : null}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function VisibilityDialog({
  agent,
  onClose,
  onSaved,
}: {
  agent: ManagedAgent | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [visibility, setVisibility] = useState<AgentVisibility>("org");
  const [users, setUsers] = useState("");
  const [groups, setGroups] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Seed the form from the agent whenever a new one opens.
  useEffect(() => {
    if (!agent) return;
    setVisibility(agent.visibility);
    setUsers(agent.audience.users.join(", "));
    setGroups(agent.audience.groups.join(", "));
    setError(null);
    setBusy(false);
  }, [agent]);

  async function onSave() {
    if (!agent) return;
    setBusy(true);
    setError(null);
    const audience: Audience =
      visibility === "restricted"
        ? { users: parseList(users), groups: parseList(groups) }
        : { users: [], groups: [] };
    const result = await setAgentVisibility(agent.id, visibility, audience);
    setBusy(false);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    onSaved();
  }

  return (
    <Dialog
      open={agent !== null}
      onOpenChange={(open) => {
        if (busy) return;
        if (!open) onClose();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Edit visibility{agent ? ` — ${agent.name}` : ""}</DialogTitle>
          <DialogDescription>
            Choose who can see and use this agent. Restricted agents are visible only to the named
            users and groups (plus the owner and platform admins).
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4">
          <VisibilityToggle value={visibility} onChange={setVisibility} disabled={busy} />
          {visibility === "restricted" && (
            <AudienceEditor
              users={users}
              groups={groups}
              disabled={busy}
              onChange={({ users: u, groups: g }) => {
                setUsers(u);
                setGroups(g);
              }}
            />
          )}
          {error !== null && (
            <div role="alert" className={ALERT_CLASS}>
              {error}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={() => void onSave()} disabled={busy}>
            {busy ? "Saving…" : "Save"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ── 3. Publish ─────────────────────────────────────────────────────

function PublishSection({ onPublished }: { onPublished: () => void }) {
  const [open, setOpen] = useState(false);
  const [publishable, setPublishable] = useState<PublishableAgent[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Form state.
  const [sourceId, setSourceId] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [visibility, setVisibility] = useState<AgentVisibility>("org");
  const [users, setUsers] = useState("");
  const [groups, setGroups] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [doneName, setDoneName] = useState<string | null>(null);
  const [doneAgentId, setDoneAgentId] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<AgentTestResult | null>(null);
  const [testBusy, setTestBusy] = useState(false);
  const [testError, setTestError] = useState<string | null>(null);

  const openDialog = useCallback(async () => {
    // Reset the form each time the dialog opens.
    setSourceId("");
    setName("");
    setDescription("");
    setVisibility("org");
    setUsers("");
    setGroups("");
    setError(null);
    setBusy(false);
    setDoneName(null);
    setDoneAgentId(null);
    setTestResult(null);
    setTestError(null);
    setTestBusy(false);
    setOpen(true);
    setPublishable(null);
    setLoadError(null);
    const result = await listPublishable();
    if (!result.ok) {
      setLoadError(result.error);
      setPublishable([]);
      return;
    }
    setPublishable(result.publishable);
  }, []);

  async function onPublish() {
    if (sourceId === "" || name.trim() === "") {
      setError("Pick a source session and enter a name.");
      return;
    }
    setBusy(true);
    setError(null);
    const audience: Audience =
      visibility === "restricted"
        ? { users: parseList(users), groups: parseList(groups) }
        : { users: [], groups: [] };
    const result = await publishAgent({
      source_session_id: sourceId,
      name: name.trim(),
      description: description.trim(),
      visibility,
      audience,
    });
    setBusy(false);
    if (!result.ok) {
      setError(
        result.status === 409
          ? `An agent named "${name.trim()}" already exists. Choose a different name.`
          : result.error,
      );
      return;
    }
    setDoneName(result.published.name);
    setDoneAgentId(result.published.agent_id);
    // Refresh the Agents + Usage sections so the new catalog agent appears.
    onPublished();
  }

  return (
    <section>
      <SectionHeader
        icon={UploadIcon}
        title="Publish agent"
        action={
          <Button onClick={() => void openDialog()}>
            <UploadIcon /> Publish agent
          </Button>
        }
      />
      <p className="text-sm text-muted-foreground">
        Promote one of your session agents into the shared catalog so others can use it.
      </p>

      <Dialog
        open={open}
        onOpenChange={(next) => {
          if (busy) return;
          setOpen(next);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Publish agent</DialogTitle>
            <DialogDescription>
              Promote one of your session-scoped agents into the shared template catalog.
            </DialogDescription>
          </DialogHeader>

          {doneName !== null ? (
            <>
              <p className="text-sm">
                Published <span className="font-medium">{doneName}</span> to the shared catalog.
              </p>
              {/* Let the user immediately verify the just-published agent connects. */}
              {testBusy && <p className="text-sm text-muted-foreground">Testing connection…</p>}
              {testError !== null && (
                <div role="alert" className={ALERT_CLASS}>
                  {testError}
                </div>
              )}
              {testResult !== null && (
                <div className="flex flex-col gap-1">
                  <div className="text-sm font-medium">
                    {testResult.ok
                      ? "✓ Agent is reachable and launchable."
                      : "✗ Some checks failed."}
                  </div>
                  <ul className="flex flex-col gap-1">
                    {testResult.checks.map((c) => (
                      <li key={c.name} className="flex items-start gap-2 text-sm">
                        {c.ok ? (
                          <CheckIcon className="mt-0.5 size-4 shrink-0 text-green-600" />
                        ) : (
                          <XIcon className="mt-0.5 size-4 shrink-0 text-destructive" />
                        )}
                        <span>
                          <span className="font-medium">{c.name}</span>
                          {c.detail ? (
                            <span className="text-muted-foreground"> — {c.detail}</span>
                          ) : null}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              <DialogFooter>
                {doneAgentId !== null && (
                  <Button
                    variant="ghost"
                    disabled={testBusy}
                    onClick={async () => {
                      setTestBusy(true);
                      setTestError(null);
                      setTestResult(null);
                      const r = await testAgent(doneAgentId);
                      setTestBusy(false);
                      if (r.ok) setTestResult(r.result);
                      else setTestError(r.error);
                    }}
                  >
                    <PlugZapIcon /> {testBusy ? "Testing…" : "Test connection"}
                  </Button>
                )}
                <Button onClick={() => setOpen(false)}>Done</Button>
              </DialogFooter>
            </>
          ) : (
            <>
              <div className="flex flex-col gap-4">
                <label className="flex flex-col gap-1">
                  <span className="text-xs font-medium text-muted-foreground">Source agent</span>
                  {loadError !== null ? (
                    <div role="alert" className={ALERT_CLASS}>
                      {loadError}
                    </div>
                  ) : publishable === null ? (
                    <span className="text-sm text-muted-foreground">Loading…</span>
                  ) : publishable.length === 0 ? (
                    <span className="text-sm text-muted-foreground">
                      You have no session agents eligible to publish.
                    </span>
                  ) : (
                    <select
                      value={sourceId}
                      disabled={busy}
                      onChange={(e) => setSourceId(e.target.value)}
                      className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none transition-colors focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-50 dark:bg-input/30"
                    >
                      <option value="">Select a session agent…</option>
                      {publishable.map((p) => (
                        <option key={p.session_id} value={p.session_id}>
                          {p.title || p.name} ({p.name})
                        </option>
                      ))}
                    </select>
                  )}
                </label>

                <label className="flex flex-col gap-1">
                  <span className="text-xs font-medium text-muted-foreground">Name</span>
                  <Input
                    value={name}
                    disabled={busy}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="deal-helper"
                  />
                </label>

                <label className="flex flex-col gap-1">
                  <span className="text-xs font-medium text-muted-foreground">Description</span>
                  <Input
                    value={description}
                    disabled={busy}
                    onChange={(e) => setDescription(e.target.value)}
                    placeholder="What this agent does"
                  />
                </label>

                <div className="flex flex-col gap-2">
                  <span className="text-xs font-medium text-muted-foreground">Visibility</span>
                  <VisibilityToggle value={visibility} onChange={setVisibility} disabled={busy} />
                </div>

                {visibility === "restricted" && (
                  <AudienceEditor
                    users={users}
                    groups={groups}
                    disabled={busy}
                    onChange={({ users: u, groups: g }) => {
                      setUsers(u);
                      setGroups(g);
                    }}
                  />
                )}

                {error !== null && (
                  <div role="alert" className={ALERT_CLASS}>
                    {error}
                  </div>
                )}
              </div>

              <DialogFooter>
                <Button variant="ghost" onClick={() => setOpen(false)} disabled={busy}>
                  Cancel
                </Button>
                <Button
                  onClick={() => void onPublish()}
                  disabled={busy || publishable === null || publishable.length === 0}
                >
                  {busy ? "Publishing…" : "Publish"}
                </Button>
              </DialogFooter>
            </>
          )}
        </DialogContent>
      </Dialog>
    </section>
  );
}

// ── 4. Usage ───────────────────────────────────────────────────────

function formatUsd(amount: number): string {
  return amount.toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatTokens(tokens: number): string {
  return tokens.toLocaleString();
}

function UsageSection({ refreshToken }: { refreshToken: number }) {
  const [report, setReport] = useState<UsageReport | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  // Re-fetch on mount and whenever the page invalidates (publish/delete).
  useEffect(() => {
    void (async () => {
      const result = await getUsage();
      if (!result.ok) {
        setLoadError(result.error);
        setReport(null);
        return;
      }
      setLoadError(null);
      setReport(result.report);
    })();
  }, [refreshToken]);

  const toggle = useCallback((agentId: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(agentId)) next.delete(agentId);
      else next.add(agentId);
      return next;
    });
  }, []);

  return (
    <section>
      <SectionHeader icon={BarChart3Icon} title="Usage" />
      {loadError !== null && (
        <div role="alert" className={cn(ALERT_CLASS, "mb-3")}>
          {loadError}
        </div>
      )}
      {report === null && loadError === null ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : report !== null && report.data.length === 0 ? (
        <p className="text-sm text-muted-foreground">No usage recorded yet.</p>
      ) : report !== null ? (
        <Table>
          <thead className="bg-muted/40 text-left text-xs uppercase text-muted-foreground">
            <tr>
              <th className="px-3 py-2 font-medium">Agent</th>
              <th className="px-3 py-2 text-right font-medium">Cost</th>
              <th className="px-3 py-2 text-right font-medium">Tokens</th>
              <th className="px-3 py-2 text-right font-medium">Sessions</th>
            </tr>
          </thead>
          <tbody>
            {report.data.map((row) => {
              const isOpen = expanded.has(row.agent_id);
              return (
                <UsageRows key={row.agent_id} row={row} isOpen={isOpen} onToggle={toggle} />
              );
            })}
          </tbody>
          <tfoot>
            <tr className="border-t border-border bg-muted/40 font-medium">
              <td className="px-3 py-2">Total</td>
              <td className="px-3 py-2 text-right tabular-nums">
                {formatUsd(report.totals.total_cost_usd)}
              </td>
              <td className="px-3 py-2 text-right tabular-nums">
                {formatTokens(report.totals.total_tokens)}
              </td>
              <td className="px-3 py-2 text-right tabular-nums">{report.totals.session_count}</td>
            </tr>
          </tfoot>
        </Table>
      ) : null}
    </section>
  );
}

function UsageRows({
  row,
  isOpen,
  onToggle,
}: {
  row: UsageReport["data"][number];
  isOpen: boolean;
  onToggle: (agentId: string) => void;
}) {
  const hasBreakdown = row.by_user.length > 0;
  return (
    <>
      <tr
        className={cn("border-t border-border", hasBreakdown && "cursor-pointer hover:bg-muted/40")}
        {...(hasBreakdown
          ? {
              role: "button",
              tabIndex: 0,
              "aria-expanded": isOpen,
              "aria-label": `Toggle per-user breakdown for ${row.agent_name}`,
              onClick: () => onToggle(row.agent_id),
              onKeyDown: (e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onToggle(row.agent_id);
                }
              },
            }
          : {})}
      >
        <td className="px-3 py-2 align-middle">
          <span className="flex items-center gap-1.5">
            {hasBreakdown ? (
              isOpen ? (
                <ChevronDownIcon className="size-3.5 text-muted-foreground" />
              ) : (
                <ChevronRightIcon className="size-3.5 text-muted-foreground" />
              )
            ) : (
              <span className="inline-block size-3.5" />
            )}
            <span className="font-medium">{row.agent_name}</span>
          </span>
        </td>
        <td className="px-3 py-2 text-right align-middle tabular-nums">
          {formatUsd(row.total_cost_usd)}
        </td>
        <td className="px-3 py-2 text-right align-middle tabular-nums">
          {formatTokens(row.total_tokens)}
        </td>
        <td className="px-3 py-2 text-right align-middle tabular-nums">{row.session_count}</td>
      </tr>
      {isOpen &&
        row.by_user.map((u) => (
          <tr key={u.user_id} className="border-t border-border bg-muted/20 text-muted-foreground">
            <td className="px-3 py-1.5 pl-9 align-middle">{u.user_id}</td>
            <td className="px-3 py-1.5 text-right align-middle tabular-nums">
              {formatUsd(u.cost_usd)}
            </td>
            <td className="px-3 py-1.5 text-right align-middle tabular-nums">
              {formatTokens(u.total_tokens)}
            </td>
            <td className="px-3 py-1.5 text-right align-middle tabular-nums">{u.session_count}</td>
          </tr>
        ))}
    </>
  );
}

// ── 5. Audit (admin only) ──────────────────────────────────────────

function formatEpoch(epoch: number): string {
  return new Date(epoch * 1000).toLocaleString();
}

function AuditSection() {
  const [entries, setEntries] = useState<AuditEntry[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      const result = await getAudit();
      if (!result.ok) {
        setLoadError(result.error);
        setEntries([]);
        return;
      }
      setLoadError(null);
      setEntries(result.entries);
    })();
  }, []);

  return (
    <section>
      <SectionHeader icon={ScrollTextIcon} title="Audit log" />
      {loadError !== null && (
        <div role="alert" className={cn(ALERT_CLASS, "mb-3")}>
          {loadError}
        </div>
      )}
      {entries === null && loadError === null ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : entries !== null && entries.length === 0 && loadError === null ? (
        <p className="text-sm text-muted-foreground">No governed actions recorded yet.</p>
      ) : entries !== null && entries.length > 0 ? (
        <Table>
          <thead className="bg-muted/40 text-left text-xs uppercase text-muted-foreground">
            <tr>
              <th className="px-3 py-2 font-medium">When</th>
              <th className="px-3 py-2 font-medium">Actor</th>
              <th className="px-3 py-2 font-medium">Action</th>
              <th className="px-3 py-2 font-medium">Agent</th>
              <th className="px-3 py-2 font-medium">Detail</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((entry) => (
              <tr key={entry.id} className="border-t border-border">
                <td className="px-3 py-2 align-middle text-muted-foreground">
                  {formatEpoch(entry.ts)}
                </td>
                <td className="px-3 py-2 align-middle">{entry.actor}</td>
                <td className="px-3 py-2 align-middle">
                  <Badge variant="secondary">{entry.action}</Badge>
                </td>
                <td className="px-3 py-2 align-middle text-muted-foreground">{entry.agent_id}</td>
                <td className="px-3 py-2 align-middle text-muted-foreground">{entry.detail}</td>
              </tr>
            ))}
          </tbody>
        </Table>
      ) : null}
    </section>
  );
}
