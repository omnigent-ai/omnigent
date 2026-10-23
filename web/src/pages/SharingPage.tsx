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
import { SettingsGroup } from "@/components/SettingsGroup";
import { SettingsLabel } from "@/components/SettingsLabel";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
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

/** Scope of automatic sharing, most-private first. */
const DEFAULT_PUBLIC_OPTIONS: {
  id: Exclude<DefaultPublicSessions, "off">;
  label: string;
  description: string;
}[] = [
  {
    id: "sandbox",
    label: "Managed hosts only",
    description:
      "Share sessions on server-managed sandboxes. Sessions on local hosts start private.",
  },
  {
    id: "all",
    label: "All sessions",
    description: "Share sessions on both managed and local hosts.",
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
      <PageScroll contentClassName="px-4 md:px-8" extraBottom="2.5rem">
        <h1 className="settings-page-title mb-2 text-2xl font-semibold">Session sharing</h1>
        <p className="text-ui text-muted-foreground">
          You don't have permission to manage session sharing.
        </p>
      </PageScroll>
    );
  }

  const current = state?.sharing_mode;
  const currentTier = TIERS.find((tier) => tier.id === current);
  const editable = state?.editable ?? false;
  const publicEnabled = state?.public_sharing_enabled ?? true;
  const publicEditable = state?.public_sharing_editable ?? false;
  const publicMaxLevel = state?.public_sharing_max_level ?? "read";
  const publicMaxEditable = state?.public_sharing_max_level_editable ?? false;
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
    <PageScroll contentClassName="px-4 md:px-8" extraBottom="2.5rem">
      <div>
        <div className="mb-6">
          <h1 className="settings-page-title text-2xl font-semibold">Session sharing</h1>
          <p className="mt-1 text-ui text-muted-foreground max-md:hidden">
            Control whether users on this server can share sessions with others. Applies server-wide
            and takes effect immediately. Sharing mode and visibility defaults affect new shares.
            The public permission ceiling also applies to existing public grants.
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
            <div className="flex flex-col gap-6">
              <SettingsGroup title="Sharing mode" testId="settings-group-sharing-mode">
                <div className="flex flex-col gap-3 md:flex-row-reverse md:items-start md:gap-6">
                  <Select
                    value={current}
                    onValueChange={(value) => choose(value as SharingMode)}
                    disabled={!editable || setMode.isPending}
                    componentId="settings.sharing.mode"
                    valueHasNoPii
                  >
                    <SelectTrigger
                      className="w-full shrink-0 md:w-52"
                      aria-label="Session sharing mode"
                      aria-describedby="sharing-mode-description"
                    >
                      <SelectValue>{currentTier?.label}</SelectValue>
                    </SelectTrigger>
                    <SelectContent
                      position="popper"
                      align="end"
                      className="w-[28rem] max-w-[calc(100vw-2rem)]"
                    >
                      {TIERS.map((tier) => (
                        <SelectItem
                          key={tier.id}
                          value={tier.id}
                          textValue={tier.label}
                          aria-labelledby={`sharing-mode-${tier.id}-label`}
                          aria-describedby={`sharing-mode-${tier.id}-description`}
                          className="py-2"
                        >
                          <span className="flex flex-col gap-1">
                            <span id={`sharing-mode-${tier.id}-label`} className="font-medium">
                              {tier.label}
                            </span>
                            <span
                              id={`sharing-mode-${tier.id}-description`}
                              className="text-sm leading-normal whitespace-normal text-muted-foreground"
                            >
                              {tier.description}
                            </span>
                          </span>
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <p
                    id="sharing-mode-description"
                    className="min-w-0 flex-1 text-sm leading-normal text-muted-foreground md:pt-1"
                  >
                    {currentTier?.description}
                  </p>
                </div>
              </SettingsGroup>

              <SettingsGroup title="Public sharing" testId="settings-group-public-sharing">
                <BlockedBy reason={publicBlockedReason}>
                  <div className="flex items-center justify-between gap-4">
                    <SettingsLabel
                      label="Public access"
                      className="flex-1"
                      description={
                        <>
                          Allow sharing a session with anyone who has the link and can sign in. When
                          off, new public grants are rejected; existing public grants remain
                          accessible and can be revoked.
                          {!publicEditable && (
                            <span className="mt-1 block">
                              Managed by this deployment and can&apos;t be changed here.
                            </span>
                          )}
                        </>
                      }
                    />
                    <Switch
                      checked={effectivePublic}
                      onCheckedChange={togglePublic}
                      disabled={publicDisabled}
                      aria-label="Public access"
                      componentId="settings.sharing.public_access"
                    />
                  </div>
                </BlockedBy>

                <div className="mt-4 flex items-center justify-between gap-4 border-t border-border pt-4">
                  <SettingsLabel
                    label="Maximum public permission"
                    className="min-w-0 flex-1"
                    description={
                      <>
                        The maximum access a public link can grant. Lowering this to Read also
                        limits existing public Edit grants. Raising it does not upgrade Read grants.
                        {!publicMaxEditable && (
                          <span className="mt-1 block">Managed by this deployment.</span>
                        )}
                      </>
                    }
                  />
                  <Select
                    value={publicMaxLevel}
                    onValueChange={(value) => {
                      setError(null);
                      setMode.mutate(
                        { public_sharing_max_level: value as "read" | "edit" },
                        { onError: (err) => setError(err.message) },
                      );
                    }}
                    disabled={!publicMaxEditable || setMode.isPending}
                    componentId="settings.sharing.public_max_level"
                    valueHasNoPii
                  >
                    <SelectTrigger
                      className="h-8 w-28 shrink-0"
                      aria-label="Maximum public permission"
                    >
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="read">Read</SelectItem>
                      {state?.public_sharing_max_level_options?.includes("edit") && (
                        <SelectItem value="edit">Edit</SelectItem>
                      )}
                    </SelectContent>
                  </Select>
                </div>

                <div className="mt-4 border-t border-border pt-4">
                  <BlockedBy reason={defaultPublicBlockedReason}>
                    <div className="flex items-center justify-between gap-4">
                      <SettingsLabel
                        label="Share new sessions by default"
                        labelId="default-public-label"
                        descriptionId="default-public-description"
                        descriptionClassName="max-md:block"
                        className="flex-1"
                        description={
                          <>
                            New sessions start with public read access for anyone with the link who
                            can sign in. Owners can revoke it in Share. Existing sessions are
                            unchanged.
                            {!defaultPublicEditable && (
                              <span className="mt-1 block">
                                Managed by this deployment and can&apos;t be changed here.
                              </span>
                            )}
                          </>
                        }
                      />
                      <Switch
                        checked={effectiveDefaultPublic !== "off"}
                        onCheckedChange={(enabled) =>
                          chooseDefaultPublic(enabled ? "sandbox" : "off")
                        }
                        disabled={defaultPublicDisabled}
                        aria-labelledby="default-public-label"
                        aria-describedby="default-public-description"
                        componentId="settings.sharing.default_public"
                      />
                    </div>
                    {effectiveDefaultPublic !== "off" && (
                      <fieldset
                        className="mt-4 ml-1 border-l-2 border-border pl-4"
                        disabled={defaultPublicDisabled}
                      >
                        <legend className="mb-3 text-ui font-medium">Automatically share</legend>
                        <div className="flex flex-col gap-4">
                          {DEFAULT_PUBLIC_OPTIONS.map((option) => (
                            <label
                              key={option.id}
                              className={cn(
                                "flex cursor-pointer items-start gap-3",
                                defaultPublicDisabled && "cursor-not-allowed opacity-70",
                              )}
                            >
                              <input
                                type="radio"
                                name="default-public-sessions"
                                value={option.id}
                                checked={option.id === defaultPublic}
                                onChange={() => chooseDefaultPublic(option.id)}
                                disabled={defaultPublicDisabled}
                                aria-labelledby={`default-public-${option.id}-label`}
                                aria-describedby={`default-public-${option.id}-description`}
                                className="mt-1 size-4 accent-primary"
                              />
                              <SettingsLabel
                                label={option.label}
                                labelId={`default-public-${option.id}-label`}
                                description={option.description}
                                descriptionId={`default-public-${option.id}-description`}
                                descriptionClassName="max-md:block"
                                className="flex-1"
                              />
                            </label>
                          ))}
                        </div>
                      </fieldset>
                    )}
                  </BlockedBy>
                </div>
              </SettingsGroup>
            </div>
            {error && <p className="mt-3 text-ui text-destructive">{error}</p>}
          </>
        )}
      </div>
    </PageScroll>
  );
}
