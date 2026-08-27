// Editor for a project's stored default session settings (`config`), reached
// from the project-folder kebab menu. Writing a project's config is what lets
// the new-chat composer pre-fill host / working directory / agent and the
// isolated-worktree default when starting a session in the project.
//
// Scope mirrors what the composer prefills today: host, workspace, agent,
// whether new sessions start in a fresh git worktree, and the base branch a
// worktree forks from (which overrides the user-global default in Settings ›
// Git). Model / reasoning-effort / harness are per-agent run config,
// deliberately out of scope here. The host and agent pickers reuse the
// composer's components; the working directory reuses its filesystem browser
// (inline, so it scrolls inside the modal).
// Fields are optional: an unset one stores no default (an absent key), and an
// all-default dialog stores an empty config.

import { ChevronDownIcon } from "lucide-react";
import { type FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useProjectConfig, useUpdateProjectConfig } from "@/hooks/useConversations";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useHosts } from "@/hooks/useHosts";
import { sortAgentsForDisplay } from "@/lib/agentGrouping";
import { sandboxOptionLabel } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { readAlwaysUseWorktree } from "@/lib/worktreeDefaultPreferences";
import { SANDBOX_HOST_CHOICE } from "@/lib/hostPreferences";
import { isNativeCodingAgent } from "@/lib/nativeCodingAgents";
import type { ProjectConfig } from "@/lib/projectsApi";
import { shouldGuardDialogDismiss } from "@/lib/dialogDismissGuard";
import { AgentHarnessPicker } from "./NewChatDialog";
import { isNavigablePath, WorkspacePicker } from "./WorkspacePicker";

/** Select sentinel for "no default" — Radix Select can't hold an empty value. */
const NONE = "__none__";

/** A labeled row: label + optional hint on the left, the control on the right. */
function Field({
  label,
  hint,
  htmlFor,
  children,
}: {
  label: string;
  hint?: string;
  htmlFor?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
      <label htmlFor={htmlFor} className="flex flex-col pt-1.5">
        <span className="font-medium text-ui">{label}</span>
        {hint && <span className="text-muted-foreground text-sm">{hint}</span>}
      </label>
      <div className="sm:w-64">{children}</div>
    </div>
  );
}

/** Trim a text input to `undefined` when blank, so an empty field stores no
 *  default (an unset key) rather than an empty string. */
function trimOrUndef(value: string): string | undefined {
  const trimmed = value.trim();
  return trimmed === "" ? undefined : trimmed;
}

export function ProjectSettingsDialog({
  open,
  onOpenChange,
  projectId,
  projectName,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** First-class project id, or `null` for a label-only folder (promoted on
   *  save — a row is created under `projectName` so its config can be stored). */
  projectId: string | null;
  projectName: string;
}) {
  // A label-only folder has no row to read; its config starts empty and the
  // save promotes it. Fetch only runs for a first-class project.
  const { data: stored, isLoading, isError } = useProjectConfig(open ? projectId : null);
  // A failed config GET for a first-class project must NOT be treated as "no
  // config" — saving the resulting blank draft would send `{}` and wipe the
  // project's stored defaults. Block Save (and surface the error) until the
  // fetch succeeds. A label-only folder (no id) has nothing to load, so it's
  // never in this state.
  const loadFailed = projectId !== null && isError;
  const updateConfig = useUpdateProjectConfig();
  const hosts = useHosts();
  const { data: agents } = useAvailableAgents();
  const info = useServerInfo();
  // Sandbox is only a real default when the server can provision managed
  // sandbox hosts — mirror the composer's gate so we don't offer a target that
  // can only fail on create.
  const managedSandboxesEnabled = info !== "loading" && info.managed_sandboxes_enabled;
  const sandboxProvider = info !== "loading" ? info.sandbox_provider : null;

  // Draft fields. Seeded from the stored config each time the dialog opens (or
  // the fetched config arrives); local until saved.
  const [hostId, setHostId] = useState<string>(NONE);
  const [workspace, setWorkspace] = useState("");
  // Worktree default for the project. The toggle seeds from the project's
  // stored value when set, else from the user-global "always use a worktree"
  // default (Settings › Git). On save it stays "inherit" (stores nothing) while
  // it matches the global default, and stores an explicit true/false only to
  // override it — so a project can force a worktree on or opt out of a global on.
  const [useWorktree, setUseWorktree] = useState(false);
  // Base branch a new worktree forks from; blank stores no default (falls
  // through to the user-global default in Settings › Git). Only meaningful
  // alongside the worktree default, but kept independent so it survives toggling.
  const [baseBranch, setBaseBranch] = useState("");
  const [agentId, setAgentId] = useState<string | null>(null);
  const [workspaceOpen, setWorkspaceOpen] = useState(false);

  // The agent picker and the host Select portal their dropdowns OUTSIDE
  // DialogContent, so their dismiss (pick an option / click the body while
  // open) can bubble up as an outside-interaction and close the whole modal.
  // Track open dropdowns + a grace window and swallow only those dismisses —
  // real backdrop clicks and Escape still close. (Mirrors the scheduled-task
  // dialog, whose guard helper we reuse.) Making the picker's dropdown modal is
  // what lets it scroll inside the Dialog's scroll-lock; this guard is the
  // trade-off that keeps that modal dropdown from closing the settings dialog.
  const dropdownOpenCountRef = useRef(0);
  const dropdownClosedAtRef = useRef(0);
  const onDropdownOpenChange = (isOpen: boolean) => {
    if (isOpen) {
      dropdownOpenCountRef.current += 1;
    } else {
      dropdownOpenCountRef.current = Math.max(0, dropdownOpenCountRef.current - 1);
      dropdownClosedAtRef.current = Date.now();
    }
  };
  const guardDialogDismiss = (event: {
    target: EventTarget | null;
    preventDefault: () => void;
  }) => {
    if (
      shouldGuardDialogDismiss(event.target, {
        selectOpen: dropdownOpenCountRef.current > 0,
        msSinceSelectClose: Date.now() - dropdownClosedAtRef.current,
      })
    ) {
      event.preventDefault();
    }
  };

  useEffect(() => {
    if (!open) return;
    // Don't seed a blank draft from a failed load — Save is blocked anyway, and
    // clobbering the fields would risk sending `{}` if the guard ever regressed.
    if (loadFailed) return;
    const c: ProjectConfig = stored ?? {};
    setHostId(c.host_id ?? NONE);
    setWorkspace(c.workspace ?? "");
    setUseWorktree(c.use_worktree ?? readAlwaysUseWorktree());
    setBaseBranch(c.base_branch ?? "");
    setAgentId(c.agent_id ?? null);
    setWorkspaceOpen(false);
  }, [open, stored, loadFailed]);

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    // Guard against submitting a blank draft seeded from a failed load, which
    // the server would read as "clear the stored defaults".
    if (loadFailed) return;
    // Build the config from set fields only — an unset slot is an absent key,
    // so the whole object is `{}` when nothing is configured (the server treats
    // that as "clear the stored defaults").
    const config: ProjectConfig = {};
    if (hostId !== NONE) config.host_id = hostId;
    // A workspace is host-relative and only meaningful with a concrete host —
    // don't persist a stale path from a since-cleared host, and drop it for the
    // sandbox (a sandbox create provisions its own workspace and ignores this).
    const ws =
      hostId !== NONE && hostId !== SANDBOX_HOST_CHOICE ? trimOrUndef(workspace) : undefined;
    if (ws) config.workspace = ws;
    if (agentId) config.agent_id = agentId;
    // Store the worktree choice only when it overrides the user-global default;
    // while it matches, leave the key unset so the project keeps inheriting
    // (and an all-default dialog still clears to {}).
    if (useWorktree !== readAlwaysUseWorktree()) config.use_worktree = useWorktree;
    // A base branch only forks a worktree, so it's only meaningful when the
    // worktree default is on — drop it otherwise so it can't linger as a stale,
    // invisible default after the toggle is turned off.
    const base = useWorktree ? trimOrUndef(baseBranch) : undefined;
    if (base) config.base_branch = base;

    updateConfig.mutate(
      { id: projectId, name: projectName, config },
      { onSuccess: () => onOpenChange(false) },
    );
  };

  // Offer only online hosts (an offline host can only fail on create), plus the
  // sandbox when the server supports it. If the project's stored host is no
  // longer online, keep it as a labeled fallback item so opening + saving the
  // dialog doesn't silently drop the saved default.
  const onlineHosts = (hosts.data ?? []).filter((h) => h.status === "online");
  const storedHostMissing =
    hostId !== NONE &&
    hostId !== SANDBOX_HOST_CHOICE &&
    !onlineHosts.some((h) => h.host_id === hostId);
  // The filesystem browser needs a concrete, online host to list against — so
  // it's only offered when a real host is the default (not sandbox / no-default
  // / an offline stored host). Otherwise the field is a plain path input.
  const browsableHostId =
    hostId !== NONE && hostId !== SANDBOX_HOST_CHOICE && !storedHostMissing ? hostId : null;

  // Agent picker groups, mirroring the composer's split (native harness CLIs vs
  // SDK / bundle agents). The picker takes both lists and a selection.
  const agentList = useMemo(() => sortAgentsForDisplay(agents ?? []), [agents]);
  const harnessEntries = useMemo(() => agentList.filter(isNativeCodingAgent), [agentList]);
  const agentEntries = useMemo(() => agentList.filter((a) => !isNativeCodingAgent(a)), [agentList]);
  const selectedAgent = agentList.find((a) => a.id === agentId) ?? null;
  const agentLabel = selectedAgent ? selectedAgent.display_name : "No default";
  // The host the agent picker's readiness badges check against (its config
  // hints show whether a harness is set up there). Null when no concrete host.
  const warningHost = onlineHosts.find((h) => h.host_id === browsableHostId) ?? null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        onClick={(e) => e.stopPropagation()}
        className="sm:max-w-lg"
        // Keep a nested dropdown's dismiss (pick an option, or click the modal
        // body while it's open) from closing the whole Dialog. See
        // `guardDialogDismiss`; real backdrop clicks and Escape still close.
        onPointerDownOutside={guardDialogDismiss}
        onInteractOutside={guardDialogDismiss}
      >
        <DialogHeader>
          <DialogTitle>Project settings</DialogTitle>
          <DialogDescription>
            Defaults for new sessions in <span className="font-medium">{projectName}</span>. Each is
            a starting point you can change per session; leave a field blank for no default.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={onSubmit} className="flex flex-col gap-4">
          <Field label="Host" hint="Where new sessions run by default">
            <Select
              value={hostId}
              onValueChange={setHostId}
              onOpenChange={onDropdownOpenChange}
              disabled={isLoading}
            >
              <SelectTrigger className="w-full" data-testid="project-settings-host">
                <SelectValue placeholder="No default" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NONE}>No default</SelectItem>
                {managedSandboxesEnabled && (
                  <SelectItem value={SANDBOX_HOST_CHOICE}>
                    {sandboxOptionLabel(sandboxProvider)}
                  </SelectItem>
                )}
                {onlineHosts.map((h) => (
                  <SelectItem key={h.host_id} value={h.host_id}>
                    {h.name}
                  </SelectItem>
                ))}
                {storedHostMissing && (
                  <SelectItem value={hostId}>
                    {stored?.host_id === hostId ? `${hostId} (unavailable)` : hostId}
                  </SelectItem>
                )}
              </SelectContent>
            </Select>
          </Field>

          <Field
            label="Working directory"
            hint={
              hostId === NONE
                ? "Pick a host first"
                : browsableHostId
                  ? "Browse the host or type a path"
                  : "Absolute path on the host"
            }
            htmlFor="project-settings-workspace"
          >
            {hostId === NONE ? (
              // Mirror the new-session composer: the working directory can only
              // be chosen once a host is selected (the file browser lists
              // against a concrete host, and a path is host-relative anyway).
              <p
                className="rounded-md border border-dashed px-3 py-2 text-muted-foreground text-ui"
                data-testid="project-settings-workspace"
              >
                Pick a host first
              </p>
            ) : browsableHostId ? (
              // A compact trigger showing the current path; clicking expands
              // the filesystem browser as an overlay anchored to the trigger.
              // The browser is rendered inside DialogContent (not a portaled
              // popover) so it scrolls — a modal Dialog's scroll-lock blocks
              // wheel events on portaled content — but positioned `absolute`
              // so it floats over the fields below instead of stretching the
              // modal. onNavigate updates the field live as you browse.
              <div
                className="relative flex flex-col gap-1.5"
                data-testid="project-settings-workspace"
              >
                <button
                  type="button"
                  onClick={() => setWorkspaceOpen((v) => !v)}
                  aria-expanded={workspaceOpen}
                  disabled={isLoading}
                  className="flex h-8 w-full items-center justify-between gap-2 rounded-md border border-input bg-transparent px-3 text-ui outline-none disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <span className={workspace ? "truncate" : "truncate text-muted-foreground"}>
                    {workspace || "Browse…"}
                  </span>
                  <ChevronDownIcon
                    className={`size-4 shrink-0 opacity-50 transition-transform ${
                      workspaceOpen ? "rotate-180" : ""
                    }`}
                  />
                </button>
                {workspaceOpen && (
                  <>
                    {/* Click-away: a transparent full-modal backdrop that
                          closes the browser (keeping the current path) on any
                          click outside it. */}
                    <button
                      type="button"
                      aria-label="Close directory browser"
                      className="fixed inset-0 z-10 cursor-default"
                      onClick={() => setWorkspaceOpen(false)}
                    />
                    <div className="absolute top-full right-0 left-0 z-20 mt-1 rounded-[12px] border border-border bg-popover p-2 shadow-menu dark:border-white/10 dark:backdrop-blur-xl dark:backdrop-saturate-150 [&>[data-testid=workspace-picker]]:border-0">
                      <WorkspacePicker
                        hostId={browsableHostId}
                        initialPath={isNavigablePath(workspace) ? workspace : undefined}
                        onNavigate={setWorkspace}
                      />
                    </div>
                  </>
                )}
              </div>
            ) : (
              // A host is picked but not browsable (sandbox / offline stored
              // host) — fall back to typing an absolute path.
              <input
                id="project-settings-workspace"
                data-testid="project-settings-workspace"
                className="w-full rounded-md border bg-transparent px-3 py-2 text-ui outline-none disabled:cursor-not-allowed disabled:opacity-50"
                placeholder="/path/to/repo"
                value={workspace}
                onChange={(e) => setWorkspace(e.target.value)}
                disabled={isLoading}
              />
            )}
          </Field>

          <Field
            label="Random worktree"
            hint="Start each new session in a fresh randomly-named git worktree (vs. directly in the workspace). Overrides the global default in Settings › Git."
          >
            <div className="flex sm:justify-end">
              <Switch
                data-testid="project-settings-worktree"
                checked={useWorktree}
                onCheckedChange={setUseWorktree}
                disabled={isLoading}
              />
            </div>
          </Field>

          {useWorktree && (
            <Field
              label="Base branch"
              hint="Branch new worktrees fork from; blank uses the current branch"
              htmlFor="project-settings-base-branch"
            >
              <input
                id="project-settings-base-branch"
                data-testid="project-settings-base-branch"
                className="w-full rounded-md border bg-transparent px-3 py-2 text-ui outline-none disabled:cursor-not-allowed disabled:opacity-50"
                placeholder="e.g. main"
                value={baseBranch}
                onChange={(e) => setBaseBranch(e.target.value)}
                disabled={isLoading}
              />
            </Field>
          )}

          <Field label="Agent" hint="Default agent / harness for new sessions">
            <div className="flex flex-col items-end gap-1" data-testid="project-settings-agent">
              <AgentHarnessPicker
                agentEntries={agentEntries}
                harnessEntries={harnessEntries}
                effectiveAgentId={agentId}
                agentLabel={agentLabel}
                hasAgents={agentList.length > 0}
                host={warningHost}
                onSelectAgent={(a) => setAgentId(a.id)}
                pendingAgent={null}
                pendingAgentId="__unused_pending_agent__"
                onSelectPending={() => {}}
                // No interactive create flow here, so hide the "Create custom
                // agent" action and leave the handler inert.
                onCreateCustomAgent={() => {}}
                allowCreateCustomAgent={false}
                sandboxSelected={hostId === SANDBOX_HOST_CHOICE}
                // Modal so the menu establishes its own scroll context and can
                // scroll inside the Dialog's scroll-lock (a non-modal dropdown
                // portals outside that lock and can't scroll). The dismiss
                // guard on DialogContent keeps this modal dropdown's own close
                // from bubbling up and closing the settings dialog.
                dropdownModal
                onOpenChange={onDropdownOpenChange}
                // Bound the menu height so it scrolls inside the modal instead
                // of running off the bottom; fixed width matches the composer.
                contentClassName="max-h-80 w-80"
                contentAlign="end"
                // Fill the field column and match the sibling <Select> triggers
                // (full width, bordered, h-8) so the control right-aligns with
                // the host / effort dropdowns instead of floating mid-row.
                triggerClassName="h-8 w-full justify-between rounded-md border border-input bg-transparent px-3 text-foreground hover:bg-transparent hover:text-foreground"
                triggerLabelClassName="max-w-none text-ui"
              />
              {agentId && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-auto p-0 text-muted-foreground text-sm hover:bg-transparent"
                  onClick={() => setAgentId(null)}
                >
                  Clear
                </Button>
              )}
            </div>
          </Field>

          {loadFailed && (
            <p
              className="text-destructive text-ui"
              role="alert"
              data-testid="project-settings-load-error"
            >
              Couldn't load this project's settings. Close and reopen to try again — saving is
              disabled so your existing defaults aren't overwritten.
            </p>
          )}
          {updateConfig.isError && (
            <p className="text-destructive text-ui" role="alert">
              {(updateConfig.error as Error).message}
            </p>
          )}

          <DialogFooter className="border-t-0 bg-transparent">
            <Button
              type="button"
              variant="ghost"
              onClick={() => onOpenChange(false)}
              disabled={updateConfig.isPending}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              data-testid="project-settings-save"
              loading={updateConfig.isPending}
              disabled={isLoading || loadFailed}
            >
              Save
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
