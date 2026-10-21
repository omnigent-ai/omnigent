import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { BoxIcon, PlusIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
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
import { sandboxOptionLabel } from "@/lib/capabilities";
import {
  disableSandboxProfile,
  fetchSandboxProfiles,
  SANDBOX_PROFILES_QUERY_KEY,
  saveSandboxProfile,
  type SandboxProfile,
} from "@/lib/sandboxProfilesApi";

export function SettingsSandboxesSection() {
  const queryClient = useQueryClient();
  const profiles = useQuery({
    queryKey: SANDBOX_PROFILES_QUERY_KEY,
    queryFn: fetchSandboxProfiles,
  });
  const [editing, setEditing] = useState<SandboxProfile | "new" | null>(null);
  const [disabling, setDisabling] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function disable(profile: SandboxProfile) {
    setDisabling(profile.id);
    setError(null);
    try {
      await disableSandboxProfile(profile.id);
      await queryClient.invalidateQueries({ queryKey: SANDBOX_PROFILES_QUERY_KEY });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not disable the profile.");
    } finally {
      setDisabling(null);
    }
  }

  const canManage = profiles.data?.can_manage === true;
  const providers = profiles.data?.providers ?? [];
  return (
    <section>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="settings-page-title text-2xl font-semibold">Sandboxes</h1>
          <p className="mt-1 text-ui text-muted-foreground">
            Reusable configurations for new sandbox sessions.
          </p>
        </div>
        {canManage && (
          <Button onClick={() => setEditing("new")} disabled={providers.length === 0}>
            <PlusIcon className="size-4" />
            New profile
          </Button>
        )}
      </div>
      <div className="mt-6 flex flex-col gap-3">
        {profiles.isPending && <p className="text-sm text-muted-foreground">Loading profiles…</p>}
        {(profiles.error || error) && (
          <p role="alert" className="text-sm text-destructive">
            {error ?? profiles.error?.message}
          </p>
        )}
        {profiles.isError && (
          <Button variant="outline" className="self-start" onClick={() => void profiles.refetch()}>
            Try again
          </Button>
        )}
        {profiles.data && !canManage && (
          <p className="text-sm text-muted-foreground">
            Only administrators can manage sandbox profiles.
          </p>
        )}
        {profiles.data?.profiles.length === 0 && (
          <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed px-6 py-12 text-center">
            <BoxIcon className="mb-1 size-7 text-muted-foreground" />
            <h2 className="font-medium">No sandbox profiles yet</h2>
            <p className="max-w-sm text-sm text-muted-foreground">
              Sessions use the server default. Add a profile to offer another configuration when
              starting a chat.
            </p>
          </div>
        )}
        {profiles.data?.profiles.map((profile) => (
          <div
            key={profile.id}
            className="flex flex-wrap items-center justify-between gap-4 rounded-xl border bg-card p-4"
          >
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="font-medium">{profile.name}</h2>
                <Badge variant="secondary">{sandboxOptionLabel(profile.provider)}</Badge>
                {!profile.enabled && <Badge variant="outline">Disabled</Badge>}
              </div>
              {profile.description && (
                <p className="mt-1 text-sm text-muted-foreground">{profile.description}</p>
              )}
              <p className="mt-2 font-mono text-xs text-muted-foreground">{profile.id}</p>
            </div>
            {canManage && (
              <div className="flex shrink-0 gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setEditing(profile)}
                  aria-label={`Edit ${profile.name}`}
                >
                  Edit
                </Button>
                {profile.enabled && (
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={disabling !== null}
                    onClick={() => void disable(profile)}
                    aria-label={`Disable ${profile.name}`}
                  >
                    {disabling === profile.id ? "Disabling…" : "Disable"}
                  </Button>
                )}
              </div>
            )}
          </div>
        ))}
        {canManage && (
          <p className="text-xs text-muted-foreground">
            Existing sessions keep their saved profile overrides; omitted settings use deployment
            defaults. Disabling a profile blocks new sandbox starts and wakes.
          </p>
        )}
      </div>
      {editing !== null && (
        <ProfileEditor
          profile={editing === "new" ? null : editing}
          providers={providers}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            await queryClient.invalidateQueries({ queryKey: SANDBOX_PROFILES_QUERY_KEY });
            setEditing(null);
          }}
        />
      )}
    </section>
  );
}

function ProfileEditor({
  profile,
  providers,
  onClose,
  onSaved,
}: {
  profile: SandboxProfile | null;
  providers: string[];
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const [id, setId] = useState(profile?.id ?? "");
  const [name, setName] = useState(profile?.name ?? "");
  const [description, setDescription] = useState(profile?.description ?? "");
  const [provider, setProvider] = useState(profile?.provider ?? providers[0] ?? "");
  const [config, setConfig] = useState(JSON.stringify(profile?.config ?? {}, null, 2));
  const [enabled, setEnabled] = useState(profile?.enabled ?? true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  let parsedConfig: Record<string, unknown> | null = null;
  let configError: string | null = null;
  try {
    const parsed: unknown = JSON.parse(config);
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      configError = "Configuration must be a JSON object.";
    } else {
      parsedConfig = parsed as Record<string, unknown>;
    }
  } catch {
    configError = "Enter valid JSON, using double quotes for field names and text.";
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!parsedConfig) return;
    setSaving(true);
    setError(null);
    try {
      await saveSandboxProfile(id.trim(), {
        name: name.trim(),
        description: description.trim(),
        provider,
        config: parsedConfig,
        enabled,
      });
      await onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the profile.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !saving) onClose();
      }}
    >
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>{profile ? "Edit sandbox profile" : "New sandbox profile"}</DialogTitle>
          <DialogDescription>
            Choose a provider and configuration for new sandbox sessions.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={(event) => void save(event)} className="flex flex-col gap-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="flex flex-col gap-2">
              <label className="text-sm font-medium" htmlFor="sandbox-profile-name">
                Name
              </label>
              <Input
                id="sandbox-profile-name"
                value={name}
                onChange={(event) => setName(event.target.value)}
                required
                maxLength={100}
                placeholder="Large workspace"
                disabled={saving}
              />
            </div>
            <div className="flex flex-col gap-2">
              <label className="text-sm font-medium" htmlFor="sandbox-profile-id">
                Profile ID
              </label>
              <Input
                id="sandbox-profile-id"
                value={id}
                onChange={(event) => setId(event.target.value)}
                required
                maxLength={64}
                pattern={"[a-z0-9][a-z0-9_\\-]*"}
                title="Lowercase letters, numbers, hyphens, and underscores; start with a letter or number."
                placeholder="large-workspace"
                disabled={!!profile || saving}
              />
            </div>
          </div>
          <div className="flex flex-col gap-2">
            <label className="text-sm font-medium" htmlFor="sandbox-profile-description">
              Description
            </label>
            <Input
              id="sandbox-profile-description"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              maxLength={500}
              placeholder="When to use this profile"
              disabled={saving}
            />
          </div>
          <div className="flex flex-col gap-2">
            <label className="text-sm font-medium" htmlFor="sandbox-profile-provider">
              Provider
            </label>
            <Select value={provider} onValueChange={setProvider} disabled={saving}>
              <SelectTrigger id="sandbox-profile-provider">
                <SelectValue placeholder="Choose a provider" />
              </SelectTrigger>
              <SelectContent>
                {[...new Set([...providers, ...(profile ? [profile.provider] : [])])].map(
                  (value) => (
                    <SelectItem key={value} value={value}>
                      {sandboxOptionLabel(value)}
                    </SelectItem>
                  ),
                )}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-2">
            <label className="text-sm font-medium" htmlFor="sandbox-profile-config">
              Sandbox configuration
            </label>
            <Textarea
              id="sandbox-profile-config"
              value={config}
              onChange={(event) => setConfig(event.target.value)}
              className="min-h-44 font-mono text-sm"
              spellCheck={false}
              disabled={saving}
              aria-invalid={!!configError}
              aria-describedby="sandbox-profile-config-help"
            />
            <p
              id="sandbox-profile-config-help"
              className={`text-xs ${configError ? "text-destructive" : "text-muted-foreground"}`}
              role={configError ? "alert" : undefined}
            >
              {configError ??
                "JSON with optional image, resources, and egress fields. Use {} for the deployment defaults."}
            </p>
            <details className="text-xs text-muted-foreground">
              <summary className="cursor-pointer py-1 font-medium">Configuration example</summary>
              <pre className="mt-2 overflow-x-auto rounded-lg bg-muted p-3">
                {JSON.stringify(
                  {
                    resources: {
                      requests: { cpu: "1", memory: "2Gi" },
                      limits: { cpu: "2", memory: "4Gi" },
                    },
                  },
                  null,
                  2,
                )}
              </pre>
              <p className="mt-2">
                Resources support CPU, memory, and ephemeral-storage requests and limits. Egress
                uses Kubernetes IP and port rules and requires network-policy enforcement in the
                cluster; other allow policies also apply.
              </p>
              <p className="mt-2">
                Omitting egress keeps the deployment policy. An empty egress list denies all
                outbound traffic, including DNS, the Omnigent server, and model gateways, unless
                another policy allows it.
              </p>
            </details>
          </div>
          <div className="flex items-center justify-between gap-4 rounded-lg border p-3">
            <div>
              <label className="text-sm font-medium" htmlFor="sandbox-profile-enabled">
                Enabled
              </label>
              <p className="mt-1 text-xs text-muted-foreground">
                Disabling blocks new sandbox starts and wakes.
              </p>
            </div>
            <Switch
              id="sandbox-profile-enabled"
              checked={enabled}
              onCheckedChange={setEnabled}
              disabled={saving}
            />
          </div>
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose} disabled={saving}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={saving || !!configError || !id.trim() || !name.trim() || !provider}
            >
              {saving ? "Saving…" : "Save profile"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
