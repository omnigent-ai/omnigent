/**
 * Admin session-sharing settings page (``/settings/sharing``). Rendered as a
 * Settings sub-category, alongside Members and Policies.
 *
 * Lets an admin pick the server-wide sharing tier (on / read only / read only
 * restricted / off). Gated on the client by an admin check (non-admins see a
 * "no permission" message) AND on the server by the route handler — client-
 * side gating is just UX. When the deployment injects its own sharing policy
 * (``editable: false``), the control is read-only.
 */

import { useEffect, useState } from "react";
import { PageScroll } from "@/components/PageScroll";
import { Switch } from "@/components/ui/switch";
import type { SharingMode } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { getCurrentIsAdmin, resolveIdentity } from "@/lib/identity";
import { cn } from "@/lib/utils";
import { useSetSharing, useSharing } from "@/hooks/useSharing";

/** The four tiers, most-permissive first, with human-readable copy. */
const TIERS: { id: SharingMode; label: string; description: string }[] = [
  {
    id: "on",
    label: "On",
    description:
      "Anyone with manage access can share a session at any level (read, edit, or manage) and toggle public / workspace read.",
  },
  {
    id: "read_only",
    label: "Read only",
    description:
      "New shares are capped at read (view) access. Edit and manage grants are rejected.",
  },
  {
    id: "restricted_read_only",
    label: "Read only (restricted)",
    description:
      "Read-only, and sessions whose working directory is a home directory or the filesystem root cannot be shared at all — not even read.",
  },
  {
    id: "off",
    label: "Off",
    description:
      "Sharing is disabled. No new grants can be created and the Share control is hidden.",
  },
];

export function SharingPage() {
  const info = useServerInfo();
  // Plain header/single-user mode: no auth endpoints exist. server_version
  // distinguishes a live single-user server from a failed /v1/info probe.
  const isSingleUser =
    info !== "loading" &&
    !info.accounts_enabled &&
    info.login_url === null &&
    info.server_version !== null;
  const [meIsAdmin, setMeIsAdmin] = useState<boolean | null>(null);

  const { data: state, isLoading } = useSharing();
  const setMode = useSetSharing();
  const [error, setError] = useState<string | null>(null);

  // Admin probe via the mode-agnostic `/v1/me` identity (works under OIDC
  // too). Skipped in single-user mode where no auth endpoints exist.
  useEffect(() => {
    if (isSingleUser) return;
    void (async () => {
      const userId = await resolveIdentity();
      if (userId === null) return;
      setMeIsAdmin(getCurrentIsAdmin());
    })();
  }, [isSingleUser]);

  if (!isSingleUser && meIsAdmin === null) {
    return (
      <div className="flex min-h-full items-center justify-center text-sm text-muted-foreground">
        Loading...
      </div>
    );
  }

  if (!isSingleUser && meIsAdmin === false) {
    return (
      <div className="mx-auto w-full max-w-2xl px-6 py-12">
        <h1 className="mb-2 text-2xl font-semibold">Session sharing</h1>
        <p className="text-sm text-muted-foreground">
          You don't have permission to manage session sharing.
        </p>
      </div>
    );
  }

  const current = state?.sharing_mode;
  const editable = state?.editable ?? false;
  const publicEnabled = state?.public_sharing_enabled ?? true;
  const publicEditable = state?.public_sharing_editable ?? false;

  function choose(mode: SharingMode) {
    if (!editable || mode === current || setMode.isPending) return;
    setError(null);
    setMode.mutate({ sharing_mode: mode }, { onError: (err) => setError(err.message) });
  }

  function togglePublic(next: boolean) {
    if (!publicEditable || setMode.isPending) return;
    setError(null);
    setMode.mutate({ public_sharing: next }, { onError: (err) => setError(err.message) });
  }

  return (
    <PageScroll contentClassName="px-6">
      <div className="mx-auto w-full max-w-2xl py-2">
        <div className="mb-6">
          <h1 className="text-2xl font-semibold">Session sharing</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Control whether users on this server can share sessions with others. Applies server-wide
            and takes effect immediately. Changes affect only new shares — existing grants
            (including already-public sessions) keep working until revoked.
          </p>
        </div>

        {isLoading || current === undefined ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : (
          <>
            {!editable && (
              <p className="mb-4 rounded-md border border-border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">
                The sharing mode is managed by this deployment and can't be changed here.
              </p>
            )}
            <fieldset
              className="space-y-2"
              disabled={!editable || setMode.isPending}
              aria-label="Session sharing mode"
            >
              {TIERS.map((tier) => {
                const selected = tier.id === current;
                return (
                  <label
                    key={tier.id}
                    className={cn(
                      "flex cursor-pointer items-start gap-3 rounded-lg border px-4 py-3 transition-colors",
                      selected ? "border-primary bg-primary/5" : "border-border hover:bg-muted/50",
                      (!editable || setMode.isPending) && "cursor-not-allowed opacity-70",
                    )}
                  >
                    <input
                      type="radio"
                      name="sharing-mode"
                      value={tier.id}
                      checked={selected}
                      onChange={() => choose(tier.id)}
                      disabled={!editable || setMode.isPending}
                      className="mt-1 size-4 accent-primary"
                    />
                    <span className="flex-1">
                      <span className="block text-sm font-medium">{tier.label}</span>
                      <span className="mt-0.5 block text-xs text-muted-foreground">
                        {tier.description}
                      </span>
                    </span>
                  </label>
                );
              })}
            </fieldset>

            {/* Public access — a separate switch from the tiers above. */}
            <div className="mt-6 flex items-center justify-between rounded-lg border px-4 py-3">
              <div className="pr-4">
                <p className="text-sm font-medium">Public access</p>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  Allow sharing a session with anyone who has the link (public read access). When
                  off, the Share dialog's "Public access" toggle is hidden and new public grants are
                  rejected; sessions already shared publicly stay public until revoked.
                </p>
                {!publicEditable && (
                  <p className="mt-1 text-xs text-muted-foreground">
                    Managed by this deployment and can't be changed here.
                  </p>
                )}
              </div>
              <Switch
                checked={publicEnabled}
                onCheckedChange={togglePublic}
                disabled={!publicEditable || setMode.isPending}
                aria-label="Public access"
              />
            </div>
            {error && <p className="mt-3 text-sm text-destructive">{error}</p>}
          </>
        )}
      </div>
    </PageScroll>
  );
}
