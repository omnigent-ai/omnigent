import type { DefaultPublicSessions } from "@/hooks/useSharing";
import type { SharingMode } from "@/lib/capabilities";

import { settingsEntry } from "./types";

/** The four sharing tiers, most-permissive first, as the mode menu names and explains them. */
export const SHARING_TIERS: readonly { id: SharingMode; label: string; description: string }[] = [
  {
    id: "on",
    label: "On",
    description:
      "Users with manage access can share at read, edit, or manage level and enable public or workspace read access.",
  },
  {
    id: "read_only",
    label: "Read only",
    description: "New shares allow Read access only; Edit and Manage grants are rejected.",
  },
  {
    id: "restricted_read_only",
    label: "Read only (restricted)",
    description: "Read-only. Sessions in a home directory or the filesystem root cannot be shared.",
  },
  { id: "off", label: "Off", description: "Block new shares and hide the Share control." },
];

/** Where new sessions are shared automatically, most-private first. */
export const DEFAULT_PUBLIC_SCOPES: readonly {
  id: Exclude<DefaultPublicSessions, "off">;
  label: string;
  description: string;
}[] = [
  {
    id: "sandbox",
    label: "Managed hosts only",
    description: "Share sessions on managed sandboxes. Local sessions start private.",
  },
  {
    id: "all",
    label: "All sessions",
    description: "Share sessions on both managed and local hosts.",
  },
];

export const SHARING_MODE_ENTRY = settingsEntry({
  id: "sharing-mode",
  section: "sharing",
  label: "Sharing mode",
  description:
    "Whether users can share sessions server-wide, and at which levels. Changes apply to new shares.",
  keywords: ["share", "sharing", "server", "read only", "restricted", "off", "permissions"],
  // The group's title names the row; its menu is named on its own, with each tier explained.
  parts: { mode: "Session sharing mode" },
  partChoices: { mode: SHARING_TIERS.map((tier) => tier.label) },
  focus: "mode",
});

export const PUBLIC_ACCESS_ENTRY = settingsEntry({
  id: "public-access",
  section: "sharing",
  label: "Public access",
  description:
    "Anyone with the link can access after signing in. Turning this off blocks new public grants; existing grants stay accessible and revocable.",
  keywords: ["public", "link", "anyone", "share", "access", "sign in"],
  // While sharing is off, the switch is greyed out behind its reason, which focus reaches.
  parts: { blocked: "Turn sharing on to use public access." },
  presence: { blocked: { states: ["sharing off"] } },
});

export const PUBLIC_MAX_PERMISSION_ENTRY = settingsEntry({
  id: "public-max-permission",
  section: "sharing",
  label: "Maximum public permission",
  description:
    "Limit access through public links. Lowering to Read limits existing Edit grants; raising it does not upgrade Read grants.",
  keywords: ["public", "permission", "read", "edit", "link", "limit"],
  choices: ["Read"],
});

export const DEFAULT_PUBLIC_SESSIONS_ENTRY = settingsEntry({
  id: "default-public-sessions",
  section: "sharing",
  label: "Share new sessions by default",
  description:
    "New sessions allow public read access with a link and sign-in. Owners can revoke it in Share. Existing sessions stay unchanged.",
  keywords: ["default", "public", "new sessions", "share", "automatic", "managed hosts"],
  // While another setting overrides it, the row is greyed out behind that reason, which focus
  // reaches.
  parts: {
    scope: "Automatically share",
    blockedBySharing: "Turn sharing on to change the default visibility.",
    blockedByPublicAccess: "Turn on public access to change the default visibility.",
  },
  partChoices: { scope: DEFAULT_PUBLIC_SCOPES.map((scope) => scope.label) },
  presence: {
    scope: { states: ["shared by default"] },
    blockedBySharing: { states: ["sharing off"] },
    blockedByPublicAccess: { states: ["public access off"] },
  },
});

export const PEER_SESSION_MESSAGING_ENTRY = settingsEntry({
  id: "peer-session-messaging",
  section: "sharing",
  label: "Message other sessions",
  description:
    "Let agents send messages to other sessions owned by the same user. Changes apply immediately; child-session messaging is unchanged.",
  keywords: ["peer messaging", "agent messaging", "send message", "sessions", "server"],
});

export const SHARING_SETTINGS = [
  SHARING_MODE_ENTRY,
  PUBLIC_ACCESS_ENTRY,
  PUBLIC_MAX_PERMISSION_ENTRY,
  DEFAULT_PUBLIC_SESSIONS_ENTRY,
  PEER_SESSION_MESSAGING_ENTRY,
] as const;
