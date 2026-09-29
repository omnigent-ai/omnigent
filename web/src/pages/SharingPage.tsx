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

import { type ReactNode, useEffect, useState } from "react";
import { PageScroll } from "@/components/PageScroll";
import { Switch } from "@/components/ui/switch";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { type SharingMode, isSingleUserMode } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { getCurrentIsAdmin, resolveIdentity } from "@/lib/identity";
import { cn } from "@/lib/utils";
import { type DefaultPublicSessions, useSetSharing, useSharing } from "@/hooks/useSharing";

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

/** Which new sessions start public, most-private first. */
const DEFAULT_PUBLIC_OPTIONS: { id: DefaultPublicSessions; label: string; description: string }[] =
  [
    {
      id: "off",
      label: "Private",
      description: "New sessions start private. Owners share them explicitly.",
    },
    {
      id: "sandbox",
      label: "Cloud sandbox sessions public",
      description:
        "Sessions running in a server-managed cloud sandbox start with public read access. Sessions on a user's own machine stay private.",
    },
    {
      id: "all",
      label: "All sessions public",
      description: "Every new session starts with public read access.",
    },
  ];

/**
 * Greys out a control another setting overrides and explains why on hover.
 * Renders children untouched when ``reason`` is null.
 */
function BlockedBy({ reason, children }: { reason: string | null; children: ReactNode }) {
  if (reason === null) return children;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <div tabIndex={0} className="cursor-not-allowed opacity-50" data-blocked="true">
          <span className="sr-only">{reason}</span>
          {children}
        </div>
      </TooltipTrigger>
      <TooltipContent side="top">{reason}</TooltipContent>
    </Tooltip>
  );
}

export function SharingPage() {
  const info = useServerInfo();
  // Plain header/single-user mode: no auth endpoints exist. The nav + route
  // already hide Sharing here; this stays as a fallback for a direct hit.
  const isSingleUser = isSingleUserMode(info);
  const [meIsAdmin, setMeIsAdmin] = useState<boolean | null>(null);

  // Wait for `/v1/me`: the server promotes a file-listed admin there, and
  // fetching first would 403 (and a non-admin needn't fetch at all).
  const { data: state, isLoading } = useSharing({ enabled: isSingleUser || meIsAdmin === true });
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
      <div className="flex min-h-full items-center justify-center text-ui text-muted-foreground">
        Loading...
      </div>
    );
  }

  if (!isSingleUser && meIsAdmin === false) {
    return (
      <PageScroll contentClassName="px-8" extraBottom="2.5rem">
        <h1 className="mb-2 text-2xl font-semibold">Session sharing</h1>
        <p className="text-ui text-muted-foreground">
          You don't have permission to manage session sharing.
        </p>
      </PageScroll>
    );
  }

  const current = state?.sharing_mode;
  const editable = state?.editable ?? false;
  const publicEnabled = state?.public_sharing_enabled ?? true;
  const publicEditable = state?.public_sharing_editable ?? false;
  const defaultPublic = state?.default_public_sessions ?? "off";
  const defaultPublicEditable = state?.default_public_sessions_editable ?? false;
  // Settings a higher-level one overrides are greyed out and show their
  // effective value; the saved value returns once the blocker is lifted.
  const sharingOff = current === "off";
  const publicBlockedReason = sharingOff ? "Turn sharing on to use public access." : null;
  const defaultPublicBlockedReason = sharingOff
    ? "Turn sharing on to change the default visibility."
    : !publicEnabled
      ? "Turn on public access to change the default visibility."
      : null;
  const effectivePublic = publicEnabled && !sharingOff;
  const effectiveDefaultPublic = defaultPublicBlockedReason ? "off" : defaultPublic;
  const publicDisabled = !publicEditable || setMode.isPending || publicBlockedReason !== null;
  const defaultPublicDisabled =
    !defaultPublicEditable || setMode.isPending || defaultPublicBlockedReason !== null;

  function choose(mode: SharingMode) {
    if (!editable || mode === current || setMode.isPending) return;
    setError(null);
    setMode.mutate({ sharing_mode: mode }, { onError: (err) => setError(err.message) });
  }

  function togglePublic(next: boolean) {
    if (publicDisabled) return;
    setError(null);
    setMode.mutate({ public_sharing: next }, { onError: (err) => setError(err.message) });
  }

  function chooseDefaultPublic(next: DefaultPublicSessions) {
    if (defaultPublicDisabled || next === defaultPublic) return;
    setError(null);
    setMode.mutate({ default_public_sessions: next }, { onError: (err) => setError(err.message) });
  }

  return (
    <PageScroll contentClassName="px-8" extraBottom="2.5rem">
      <div>
        <div className="mb-6">
          <h1 className="text-2xl font-semibold">Session sharing</h1>
          <p className="mt-1 text-ui text-muted-foreground">
            Control whether users on this server can share sessions with others. Applies server-wide
            and takes effect immediately. Changes affect only new shares — existing grants
            (including already-public sessions) keep working until revoked.
          </p>
        </div>

        {isLoading || current === undefined ? (
          <p className="text-ui text-muted-foreground">Loading…</p>
        ) : (
          <>
            {!editable && (
              <p className="mb-4 rounded-md border border-border bg-muted/40 px-3 py-2 text-ui text-muted-foreground">
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
                      <span className="block text-ui font-medium">{tier.label}</span>
                      <span className="mt-0.5 block text-sm text-muted-foreground">
                        {tier.description}
                      </span>
                    </span>
                  </label>
                );
              })}
            </fieldset>

            {/* Public access — a separate switch from the tiers above. */}
            <BlockedBy reason={publicBlockedReason}>
              <div className="mt-6 flex items-center justify-between rounded-lg border px-4 py-3">
                <div className="pr-4">
                  <p className="text-ui font-medium">Public access</p>
                  <p className="mt-0.5 text-sm text-muted-foreground">
                    Allow sharing a session with anyone who has the link (public read access). When
                    off, the Share dialog's "Public access" toggle is hidden and new public grants
                    are rejected; sessions already shared publicly stay public until revoked.
                  </p>
                  {!publicEditable && (
                    <p className="mt-1 text-sm text-muted-foreground">
                      Managed by this deployment and can't be changed here.
                    </p>
                  )}
                </div>
                <Switch
                  checked={effectivePublic}
                  onCheckedChange={togglePublic}
                  disabled={publicDisabled}
                  aria-label="Public access"
                  componentId="settings.sharing.public_access"
                />
              </div>
            </BlockedBy>
            {/* Default visibility of NEW sessions: writes a public read grant at creation. */}
            <BlockedBy reason={defaultPublicBlockedReason}>
              <div className="mt-6">
                <p className="text-ui font-medium">Default visibility for new sessions</p>
                <p className="mt-0.5 mb-2 text-sm text-muted-foreground">
                  Whether new sessions start with public read access. Owners can still revoke it
                  from the Share dialog. Existing sessions are unchanged.
                </p>
                {!defaultPublicEditable && (
                  <p className="mb-2 text-sm text-muted-foreground">
                    Managed by this deployment and can't be changed here.
                  </p>
                )}
                <fieldset
                  className="space-y-2"
                  disabled={defaultPublicDisabled}
                  aria-label="Default visibility for new sessions"
                >
                  {DEFAULT_PUBLIC_OPTIONS.map((option) => {
                    const selected = option.id === effectiveDefaultPublic;
                    return (
                      <label
                        key={option.id}
                        className={cn(
                          "flex cursor-pointer items-start gap-3 rounded-lg border px-4 py-3 transition-colors",
                          selected
                            ? "border-primary bg-primary/5"
                            : "border-border hover:bg-muted/50",
                          defaultPublicDisabled && "cursor-not-allowed opacity-70",
                        )}
                      >
                        <input
                          type="radio"
                          name="default-public-sessions"
                          value={option.id}
                          checked={selected}
                          onChange={() => chooseDefaultPublic(option.id)}
                          disabled={defaultPublicDisabled}
                          className="mt-1 size-4 accent-primary"
                        />
                        <span className="flex-1">
                          <span className="block text-ui font-medium">{option.label}</span>
                          <span className="mt-0.5 block text-sm text-muted-foreground">
                            {option.description}
                          </span>
                        </span>
                      </label>
                    );
                  })}
                </fieldset>
              </div>
            </BlockedBy>
            {error && <p className="mt-3 text-ui text-destructive">{error}</p>}
          </>
        )}
      </div>
    </PageScroll>
  );
}
