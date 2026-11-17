import { settingsEntry } from "./types";

export const MEMBERS_INVITE_ENTRY = settingsEntry({
  id: "members-invite",
  available: (availability) => availability.accountsEnabled,
  section: "members",
  label: "Invite member",
  description:
    "Create a single-use link that lets someone make their own account on this server, as a member or an admin.",
  keywords: ["invite", "add member", "add user", "new account", "sign up", "link", "admin"],
  parts: {
    invite: "Invite member",
    grantAdmin: "Grant admin privileges",
    create: "Create invite",
    inviteUrl: "Invite URL",
    copyInviteUrl: "Copy invite URL",
  },
  // The invite form, then the link it made, which is shown only once.
  presence: {
    grantAdmin: { overlay: "invite", states: ["choosing"] },
    create: { overlay: "invite", states: ["choosing"] },
    inviteUrl: { overlay: "invite", states: ["invite created"] },
    copyInviteUrl: { overlay: "invite", states: ["invite created"] },
  },
  overlays: { invite: { dialogActions: ["Cancel", "Done"] } },
  focus: "invite",
  // The invite button shows only where accounts can be managed; the list is always there.
  revealedBy: "members-list",
});

export const MEMBERS_LIST_ENTRY = settingsEntry({
  id: "members-list",
  section: "members",
  label: "Members",
  description: "Everyone with an account on this server, their role, and when they last signed in.",
  keywords: ["members", "users", "accounts", "admins", "reset password", "remove", "last login"],
  parts: { refresh: "Refresh" },
  // Each member's own actions, and the dialog each opens; none of their data is searchable.
  regions: {
    members: {
      rowActions: { resetPassword: "Reset", remove: "Remove" },
      rowOverlays: {
        resetPassword: {
          rowActions: { copy: "Copy password", done: "Done" },
          rowFields: { password: { before: "New password for " } },
        },
        remove: { rowActions: { cancel: "Cancel", remove: "Remove" } },
      },
    },
  },
});

export const MEMBERS_SETTINGS = [MEMBERS_INVITE_ENTRY, MEMBERS_LIST_ENTRY] as const;
