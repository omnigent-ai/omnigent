import { useRef, useState, useSyncExternalStore } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useHosts, type Host } from "@/hooks/useHosts";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { harnessWarningBadgeText } from "@/lib/harnessSetup";
import { getOmnigentServerIdentity } from "@/lib/host";
import { ApiError } from "@/lib/sessionsApi";
import { getCurrentUserId, subscribeIdentity } from "@/lib/identity";
import {
  hostPreferenceAction,
  preferenceKeyId,
  preferenceLabel,
  preferenceValueLabel,
  readSharedPreferences,
  writeSharedPreference,
  type HostPreferenceAction,
  type PreferenceEntry,
  type PreferenceKey,
  type PreferenceOperation,
  type PreferencePreview,
  type PreferenceValue,
} from "@/lib/sharedPreferences";

const fieldStyle = "w-full rounded-md border border-input bg-background px-3 py-2 text-sm";
const message = (error: unknown) => (error instanceof Error ? error.message : String(error));

export function SettingsPreferencesSection() {
  const info = useServerInfo();
  const user = useSyncExternalStore(subscribeIdentity, getCurrentUserId);
  const serverIdentity = getOmnigentServerIdentity();
  const enabled = info !== "loading" && info.shared_preferences === true && serverIdentity !== null;
  const hosts = useHosts({ enabled });
  const [hostId, setHostId] = useState("");
  const selectedHost = hosts.data?.find((host) => host.host_id === hostId) ?? hosts.data?.[0];
  return (
    <section className="mx-auto max-w-3xl space-y-6 pb-8">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">Shared preferences</h1>
        <p className="text-sm text-muted-foreground">
          Model, harness, startup arguments and plugin choices follow you within this workspace.
          Session changes affect only that session.
        </p>
      </header>
      {!enabled ? (
        <p>
          {info === "loading" ? "Loading…" : "Shared preferences are not enabled on this server."}
        </p>
      ) : (
        <>
          <label className="block space-y-2 text-sm font-medium">
            Machine
            <select
              aria-label="Machine"
              className={fieldStyle}
              value={selectedHost?.host_id ?? ""}
              onChange={(event) => setHostId(event.target.value)}
            >
              {!hosts.data?.length && <option value="">No connected machines</option>}
              {hosts.data?.map((host) => (
                <option key={host.host_id} value={host.host_id}>
                  {host.name} · {host.status}
                </option>
              ))}
            </select>
          </label>
          <PreferenceControls
            key={`${serverIdentity}:${user}:${selectedHost?.host_id ?? "web"}`}
            host={selectedHost}
          />
        </>
      )}
    </section>
  );
}

function PreferenceControls({ host }: { host?: Host }) {
  const queryClient = useQueryClient();
  const user = getCurrentUserId();
  const serverIdentity = getOmnigentServerIdentity();
  const sharedKey = ["shared-preferences", serverIdentity, user];
  const hostKey = ["host-preferences", serverIdentity, user, host?.host_id];
  const shared = useQuery({
    queryKey: sharedKey,
    queryFn: () => readSharedPreferences(),
    staleTime: Infinity,
    refetchOnMount: "always",
    refetchOnWindowFocus: false,
    retry: false,
  });
  const local = useQuery({
    queryKey: hostKey,
    queryFn: () => hostPreferenceAction(host!.host_id, { action: "status" }),
    enabled: host?.status === "online",
    staleTime: Infinity,
    refetchOnMount: "always",
    refetchOnWindowFocus: false,
    retry: false,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [scope, setScope] = useState<"shared" | "machine">("shared");
  const [preview, setPreview] = useState<{ plan: PreferencePreview; restore: boolean } | null>(
    null,
  );
  const [retryOperation, setRetryOperation] = useState<PreferenceOperation | null>(null);
  const [includeClaude, setIncludeClaude] = useState(true);
  const [includeCodex, setIncludeCodex] = useState(true);
  const editor = useRef<HTMLFormElement>(null);
  const [group, setGroup] = useState("model");
  const [name, setName] = useState("");
  const [ecosystem, setEcosystem] = useState("claude");
  const [marketplace, setMarketplace] = useState("");
  const [draft, setDraft] = useState("");
  const [argumentsDraft, setArgumentsDraft] = useState<{ id: string; value: string }[]>([]);
  const [pluginEnabled, setPluginEnabled] = useState(true);
  const key: PreferenceKey =
    group === "plugin"
      ? { kind: "plugin", name: { ecosystem, source: marketplace, name } }
      : group === "arguments"
        ? { kind: "harness_args", name }
        : group === "harness"
          ? { kind: "harness", name: "default" }
          : { kind: "model", name: group };
  const value: PreferenceValue =
    group === "plugin"
      ? pluginEnabled
      : group === "arguments"
        ? argumentsDraft.map((argument) => argument.value)
        : draft;
  const snapshot = shared.data;
  const machine = local.data?.machine ?? [];
  const rows = new Map<
    string,
    { key: PreferenceKey; shared?: PreferenceEntry; machine?: PreferenceEntry }
  >();
  for (const entry of snapshot?.entries ?? [])
    rows.set(preferenceKeyId(entry.key), { key: entry.key, shared: entry });
  for (const entry of machine)
    rows.set(preferenceKeyId(entry.key), {
      ...rows.get(preferenceKeyId(entry.key)),
      key: entry.key,
      machine: entry,
    });

  async function perform(work: () => Promise<void>) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await work();
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  async function hostAction(action: HostPreferenceAction) {
    if (!host) throw new Error("Select an online machine first.");
    const result = await hostPreferenceAction(host.host_id, action);
    queryClient.setQueryData(hostKey, result);
    if (result.warning) setNotice(result.warning);
    return result;
  }
  async function refresh() {
    await shared.refetch({ throwOnError: true });
    if (host?.status === "online") await local.refetch({ throwOnError: true });
  }
  async function publish(operation: PreferenceOperation) {
    const result = await writeSharedPreference(operation).catch((cause: unknown) => {
      const rejected =
        cause instanceof ApiError &&
        cause.status >= 400 &&
        cause.status < 500 &&
        cause.status !== 408;
      setRetryOperation(rejected ? null : operation);
      if (cause instanceof ApiError && cause.status === 422) {
        throw new Error("The server rejected this value. Check the setting and try again.");
      }
      throw cause;
    });
    setRetryOperation(null);
    await shared.refetch({ throwOnError: true });
    setNotice(
      result.status === "conflict"
        ? "A competing edit was saved for review below. Your choice was not substituted."
        : "Saved for future sessions in this workspace.",
    );
  }
  async function save(target: PreferenceKey, selected: PreferenceValue, resolves: string[] = []) {
    if (!snapshot) throw new Error("Load shared preferences before saving.");
    if (scope === "machine" && !resolves.length) {
      await hostAction({ action: "set_machine", key: target, value: selected });
      setNotice(
        selected === null
          ? "Machine override removed. This setting follows the workspace again."
          : "Saved on this machine for future sessions.",
      );
      return;
    }
    const current = snapshot.entries.find(
      (entry) => preferenceKeyId(entry.key) === preferenceKeyId(target),
    );
    await publish({
      protocol_version: 1,
      identity: snapshot.identity,
      installation_id: host?.host_id ?? "browser",
      operation_id: crypto.randomUUID(),
      key: target,
      expected_version: current?.version ?? 0,
      value: selected,
      resolves,
    });
  }
  function edit(entry: PreferenceEntry) {
    const { key: selected, value: selectedValue } = entry;
    setGroup(
      selected.kind === "harness_args"
        ? "arguments"
        : selected.kind === "model"
          ? String(selected.name)
          : selected.kind,
    );
    if (typeof selected.name === "object") {
      setEcosystem(selected.name.ecosystem);
      setMarketplace(selected.name.source);
      setName(selected.name.name);
    } else setName(selected.kind === "harness_args" ? selected.name : "");
    setDraft(typeof selectedValue === "string" ? selectedValue : "");
    setArgumentsDraft(
      Array.isArray(selectedValue)
        ? selectedValue.map((argument) => ({ id: crypto.randomUUID(), value: argument }))
        : [],
    );
    setPluginEnabled(selectedValue === true);
    editor.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }
  const editorDisabled =
    busy ||
    retryOperation !== null ||
    !snapshot ||
    (scope === "machine" && (!local.data?.connected || host?.status !== "online"));

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3 text-sm">
        <span className="text-muted-foreground">
          {snapshot
            ? `Workspace ${snapshot.identity.workspace_id} · ${snapshot.identity.user_id}`
            : "Loading workspace preferences…"}
        </span>
        <Button variant="outline" disabled={busy} onClick={() => void perform(refresh)}>
          Refresh
        </Button>
      </div>
      {(error || shared.error || local.error) && (
        <div role="alert" className="rounded-md border border-destructive/40 p-3 text-sm">
          {error ?? message(shared.error ?? local.error)}{" "}
          <Button variant="link" disabled={busy} onClick={() => void perform(refresh)}>
            Retry
          </Button>
        </div>
      )}
      {notice && (
        <p role="status" className="rounded-md border p-3 text-sm">
          {notice}
        </p>
      )}
      {retryOperation && (
        <div className="rounded-md border p-3 text-sm">
          <p>
            The save has no confirmed reply. Retry the same edit safely, or refresh to inspect
            current values.
          </p>
          <Button disabled={busy} onClick={() => void perform(() => publish(retryOperation))}>
            Retry save
          </Button>
        </div>
      )}
      <section className="space-y-3 rounded-lg border p-4" aria-label="Adoption and import">
        <h2 className="font-semibold">
          {local.data?.connected
            ? "This machine follows the workspace"
            : "Choose once, then follow automatically"}
        </h2>
        <p className="text-sm text-muted-foreground">
          Use existing workspace defaults or preview defaults from the selected machine. Existing
          sessions keep their choices. Installation and credentials stay on each machine.
        </p>
        {host?.status !== "online" ? (
          <p className="text-sm">
            Connect a machine to preview, adopt or manage its overrides. Any displayed machine
            values are from its last successful read. Shared values remain available below.
          </p>
        ) : (
          <>
            <div className="flex flex-wrap gap-3">
              <Button
                variant="outline"
                disabled={busy || !snapshot || snapshot.revision === 0}
                onClick={() =>
                  void perform(async () => {
                    await hostAction({ action: "connect" });
                    setNotice("Future sessions on this machine follow workspace preferences.");
                  })
                }
              >
                {local.data?.connected ? "Reconnect workspace" : "Use workspace defaults"}
              </Button>
              {snapshot?.revision === 0 && (
                <Button
                  variant="outline"
                  disabled={busy}
                  onClick={() =>
                    void perform(async () => {
                      await publish({
                        protocol_version: 1,
                        identity: snapshot.identity,
                        operation_id: crypto.randomUUID(),
                        installation_id: host.host_id,
                        key: { kind: "harness", name: "default" },
                        value: null,
                        expected_version: 0,
                        resolves: [],
                      });
                      await hostAction({ action: "connect" });
                    })
                  }
                >
                  Start with no shared defaults
                </Button>
              )}
              <Button
                variant="outline"
                disabled={busy}
                onClick={() =>
                  void perform(async () => {
                    const result = await hostAction({
                      action: "preview_import",
                      plugins: [
                        ...(includeClaude ? ["claude" as const] : []),
                        ...(includeCodex ? ["codex" as const] : []),
                      ],
                    });
                    if (result.preview) setPreview({ plan: result.preview, restore: false });
                  })
                }
              >
                Preview this machine
              </Button>
              {local.data?.connected && (
                <Button
                  variant="outline"
                  disabled={busy}
                  onClick={() =>
                    void perform(async () => {
                      const result = await hostAction({ action: "preview_restore" });
                      if (result.preview) setPreview({ plan: result.preview, restore: true });
                    })
                  }
                >
                  Review restore
                </Button>
              )}
            </div>
            <fieldset className="flex flex-wrap gap-4 text-sm">
              <legend className="mb-2 text-muted-foreground">
                Include installed-tool preferences in preview
              </legend>
              <label className="flex gap-2">
                <input
                  type="checkbox"
                  checked={includeClaude}
                  onChange={(event) => setIncludeClaude(event.target.checked)}
                />
                Claude plugin choices
              </label>
              <label className="flex gap-2">
                <input
                  type="checkbox"
                  checked={includeCodex}
                  onChange={(event) => setIncludeCodex(event.target.checked)}
                />
                Codex plugin choices
              </label>
            </fieldset>
          </>
        )}
      </section>
      {!!local.data?.pending.length && (
        <section aria-label="Pending edits" className="space-y-2 rounded-md border p-4">
          <h2 className="font-semibold">
            {local.data.pending.length} pending edits on this machine
          </h2>
          <p className="text-sm">
            Retry delivery before importing again. Discarding the first edit does not undo a
            possible server commit.
          </p>
          <Button
            disabled={busy}
            onClick={() =>
              void perform(async () => {
                await hostAction({ action: "sync" });
                await shared.refetch({ throwOnError: true });
              })
            }
          >
            Retry sync
          </Button>{" "}
          <Button
            variant="outline"
            disabled={busy}
            onClick={() =>
              void perform(async () => {
                await hostAction({
                  action: "discard",
                  operation_id: local.data!.pending[0].operation_id,
                });
              })
            }
          >
            Discard first pending edit
          </Button>
        </section>
      )}
      {!!snapshot?.conflicts.length && (
        <section aria-label="Competing edits" className="space-y-3">
          <h2 className="font-semibold">Competing edits need your choice</h2>
          {snapshot.conflicts.map((conflict) => {
            const accepted = snapshot.entries.find(
              (entry) => preferenceKeyId(entry.key) === preferenceKeyId(conflict.key),
            );
            return (
              <div key={conflict.operation_id} className="space-y-2 rounded-md border p-3 text-sm">
                <h3 className="font-medium">{preferenceLabel(conflict.key)}</h3>
                <p>Shared: {preferenceValueLabel(accepted?.value ?? null)}</p>
                <p>Proposed: {preferenceValueLabel(conflict.value)}</p>
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    disabled={busy || !!retryOperation}
                    onClick={() =>
                      void perform(() =>
                        save(conflict.key, accepted?.value ?? null, [conflict.operation_id]),
                      )
                    }
                  >
                    Keep shared
                  </Button>
                  <Button
                    disabled={busy || !!retryOperation}
                    onClick={() =>
                      void perform(() =>
                        save(conflict.key, conflict.value, [conflict.operation_id]),
                      )
                    }
                  >
                    Use proposed
                  </Button>
                </div>
              </div>
            );
          })}
        </section>
      )}
      <section className="space-y-3" aria-label="Saved preferences">
        <h2 className="font-semibold">Saved defaults</h2>
        <p className="text-sm text-muted-foreground">
          Machine overrides win here. Project rules, session choices and managed policy may take
          precedence at launch. Unavailable selections remain saved; startup reports what needs
          attention.
        </p>
        {!rows.size ? (
          <p className="text-sm">No saved defaults yet.</p>
        ) : (
          <div className="grid gap-3">
            {[...rows.values()].map((row) => {
              const effective = row.machine?.value ?? row.shared?.value;
              const readiness =
                row.key.kind === "harness" && typeof effective === "string"
                  ? host?.configured_harnesses?.[effective]
                  : undefined;
              return (
                <article
                  key={preferenceKeyId(row.key)}
                  aria-label={preferenceLabel(row.key)}
                  className="space-y-3 rounded-lg border p-4 text-sm"
                >
                  <h3 className="font-semibold">{preferenceLabel(row.key)}</h3>
                  <dl className="grid grid-cols-2 gap-4">
                    <div className="min-w-0">
                      <dt className="mb-1 text-xs text-muted-foreground">Follow me</dt>
                      <dd className="break-words">
                        {preferenceValueLabel(row.shared?.value ?? null)}
                      </dd>
                    </div>
                    <div className="min-w-0">
                      <dt className="mb-1 text-xs text-muted-foreground">This machine</dt>
                      <dd className="break-words">
                        {row.machine
                          ? preferenceValueLabel(row.machine.value)
                          : local.data
                            ? "Follow workspace"
                            : "Not loaded"}
                      </dd>
                    </div>
                  </dl>
                  {readiness !== undefined && readiness !== true && (
                    <p className="text-muted-foreground">
                      Selected, but{" "}
                      {readiness === false
                        ? "not configured on this machine"
                        : harnessWarningBadgeText(readiness)}
                      . Your choice is kept.
                    </p>
                  )}
                  <div className="flex flex-wrap gap-2">
                    <Button
                      variant="outline"
                      disabled={editorDisabled}
                      onClick={() =>
                        edit(
                          (scope === "machine" ? row.machine : row.shared) ?? {
                            key: row.key,
                            value: null,
                            version: 0,
                          },
                        )
                      }
                    >
                      Edit {scope === "machine" ? "machine override" : "shared default"}
                    </Button>
                    <Button
                      variant="ghost"
                      disabled={editorDisabled}
                      onClick={() => void perform(() => save(row.key, null))}
                    >
                      {scope === "machine" ? "Follow workspace" : "Remove shared default"}
                    </Button>
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </section>
      <form
        ref={editor}
        className="space-y-4 rounded-lg border p-4"
        onSubmit={(event) => {
          event.preventDefault();
          void perform(() => save(key, value));
        }}
      >
        <h2 className="font-semibold">Save a default</h2>
        <fieldset className="flex gap-5 text-sm" disabled={busy || !!retryOperation}>
          <legend className="mb-2 font-medium">Scope</legend>
          <label className="flex gap-2">
            <input
              type="radio"
              name="scope"
              checked={scope === "shared"}
              onChange={() => setScope("shared")}
            />
            Follow me
          </label>
          <label className="flex gap-2">
            <input
              type="radio"
              name="scope"
              checked={scope === "machine"}
              onChange={() => setScope("machine")}
            />
            This machine
          </label>
        </fieldset>
        <fieldset disabled={editorDisabled} className="space-y-3">
          <label className="block space-y-1 text-sm">
            Setting
            <select
              aria-label="Setting"
              className={fieldStyle}
              value={group}
              onChange={(event) => {
                setGroup(event.target.value);
                setDraft("");
                setName("");
              }}
            >
              <option value="model">Model</option>
              <option value="opencode_model">OpenCode model</option>
              <option value="harness">Default harness</option>
              <option value="arguments">Startup arguments</option>
              <option value="plugin">Plugin toggle</option>
            </select>
          </label>
          {group === "plugin" && (
            <>
              <label className="block space-y-1 text-sm">
                Plugin ecosystem
                <select
                  aria-label="Plugin ecosystem"
                  className={fieldStyle}
                  value={ecosystem}
                  onChange={(event) => setEcosystem(event.target.value)}
                >
                  <option value="claude">Claude</option>
                  <option value="codex">Codex</option>
                </select>
              </label>
              <label className="block space-y-1 text-sm">
                Marketplace name
                <Input
                  required
                  value={marketplace}
                  onChange={(event) => setMarketplace(event.target.value)}
                />
              </label>
            </>
          )}
          {(group === "plugin" || group === "arguments") && (
            <label className="block space-y-1 text-sm">
              {group === "plugin" ? "Plugin name" : "Harness for arguments"}
              <Input required value={name} onChange={(event) => setName(event.target.value)} />
            </label>
          )}
          {group === "plugin" ? (
            <label className="flex gap-2 text-sm">
              <input
                type="checkbox"
                checked={pluginEnabled}
                onChange={(event) => setPluginEnabled(event.target.checked)}
              />
              Enabled
            </label>
          ) : group === "arguments" ? (
            <fieldset className="space-y-3 text-sm">
              <legend className="font-medium">Ordered arguments</legend>
              <p className="text-muted-foreground">
                Each box is one argument. Spaces, line breaks and repeated flags are preserved.
                Remove all boxes for no default arguments.
              </p>
              {argumentsDraft.map((argument, index) => (
                <div key={argument.id} className="space-y-2 rounded-md border p-3">
                  <label className="block space-y-1">
                    Argument {index + 1}
                    <Textarea
                      rows={2}
                      value={argument.value}
                      onChange={(event) =>
                        setArgumentsDraft((current) =>
                          current.map((entry) =>
                            entry.id === argument.id
                              ? { ...entry, value: event.target.value }
                              : entry,
                          ),
                        )
                      }
                    />
                  </label>
                  <div className="flex flex-wrap gap-2">
                    <Button
                      type="button"
                      variant="outline"
                      disabled={index === 0}
                      onClick={() =>
                        setArgumentsDraft((current) => {
                          const next = [...current];
                          [next[index - 1], next[index]] = [next[index], next[index - 1]];
                          return next;
                        })
                      }
                    >
                      Move up
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      onClick={() =>
                        setArgumentsDraft((current) =>
                          current.filter((_, position) => position !== index),
                        )
                      }
                    >
                      Remove argument {index + 1}
                    </Button>
                  </div>
                </div>
              ))}
              <Button
                type="button"
                variant="outline"
                onClick={() =>
                  setArgumentsDraft((current) => [
                    ...current,
                    { id: crypto.randomUUID(), value: "" },
                  ])
                }
              >
                Add argument
              </Button>
            </fieldset>
          ) : (
            <label className="block space-y-1 text-sm">
              {group === "harness" ? "Harness ID" : "Model ID"}
              <Input required value={draft} onChange={(event) => setDraft(event.target.value)} />
            </label>
          )}
          <Button type="submit">
            {scope === "machine" ? "Save on this machine" : "Save shared default"}
          </Button>
        </fieldset>
        {scope === "machine" && !local.data?.connected && (
          <p className="text-sm text-muted-foreground">
            Connect this machine to workspace preferences first.
          </p>
        )}
      </form>
      <Dialog
        open={preview !== null}
        onOpenChange={(open) => {
          if (!open && !busy) setPreview(null);
        }}
      >
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              {preview?.restore ? "Review restore" : "Review imported defaults"}
            </DialogTitle>
            <DialogDescription>
              Only the values below change. Later competing edits are surfaced for review.
              Credentials and commands stay local.
            </DialogDescription>
          </DialogHeader>
          {!preview?.plan.changes.length ? (
            <p>No changes to apply.</p>
          ) : (
            <table className="w-full text-left text-sm">
              <thead>
                <tr>
                  <th className="p-2">Setting</th>
                  <th className="p-2">Before</th>
                  <th className="p-2">After</th>
                </tr>
              </thead>
              <tbody>
                {preview.plan.changes.map((change) => (
                  <tr key={change.operation.operation_id} className="border-t align-top">
                    <td className="p-2">{preferenceLabel(change.operation.key)}</td>
                    <td className="max-w-44 break-words p-2">
                      {preferenceValueLabel(change.previous.value)}
                    </td>
                    <td className="max-w-44 break-words p-2">
                      {preferenceValueLabel(change.operation.value)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button variant="outline" disabled={busy} onClick={() => setPreview(null)}>
              Cancel
            </Button>
            <Button
              disabled={busy || !preview?.plan.changes.length}
              onClick={() =>
                void perform(async () => {
                  await hostAction({ action: "apply_import", preview: preview!.plan });
                  setPreview(null);
                  await shared.refetch({ throwOnError: true });
                })
              }
            >
              {preview?.restore ? "Restore these values" : "Apply and follow"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
