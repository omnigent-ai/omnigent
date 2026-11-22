import { settingsEntry } from "./types";

export const ACCOUNT_PASSWORD_ENTRY = settingsEntry({
  id: "account-password",
  available: (availability) => availability.accountsEnabled,
  section: "account",
  label: "Change password",
  description: "Change the password you sign in to Omnigent with.",
  keywords: ["password", "account", "sign in", "security"],
  parts: {
    change: "Change password",
    currentPassword: "Current password",
    newPassword: "New password",
    confirmPassword: "Confirm new password",
    submit: "Change password",
    done: "Done",
  },
  // The dialog's form while editing or saving, then Done once the password changed.
  presence: {
    currentPassword: { overlay: "change", states: ["editing", "busy"] },
    newPassword: { overlay: "change", states: ["editing", "busy"] },
    confirmPassword: { overlay: "change", states: ["editing", "busy"] },
    submit: { overlay: "change", states: ["editing", "busy"] },
    done: { overlay: "change", states: ["password changed"] },
  },
  nameAlternatives: { submit: { busy: "Changing…" } },
  focus: "change",
});

export const ACCOUNT_SIGN_OUT_ENTRY = settingsEntry({
  id: "account-sign-out",
  section: "account",
  label: "Sign out",
  description: "Sign out of Omnigent in this browser.",
  keywords: ["sign out", "log out", "logout", "account"],
  parts: { signOut: "Sign out" },
  focus: "signOut",
});

export const ACCOUNT_SETTINGS = [ACCOUNT_PASSWORD_ENTRY, ACCOUNT_SIGN_OUT_ENTRY] as const;
