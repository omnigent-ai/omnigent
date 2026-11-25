// What the new page says about where a pull request is read: the header's trigger, the
// three choices its menu and ⌘K share, and the tooltip an action adds when its changes
// go elsewhere. Pure, so the wording and checks are tested in one place.

import type { ReadChoice, ReadRoute } from "@/lib/pullRequestReadRoute";

export const SESSION_MACHINE_LABEL = "Session’s machine";

/** The menu's heading: the choice decides where the PR is read. */
export const READ_SOURCE_MENU_HEADING = "Read this pull request with";

/**
 * The menu's last line: where changes go, which Settings and the app decide apart from where
 * the PR is read. `macWriteLogin` is who makes them on this Mac, or null where they don't.
 */
export function readSourceChangesNote(macWriteLogin: string | null): string {
  return macWriteLogin
    ? `Changes are made on this Mac as @${macWriteLogin}.`
    : "Changes are made through the session’s machine.";
}

/** One way to read the PR, as the menu and ⌘K offer it. */
export interface ReadSourceChoice {
  choice: ReadChoice;
  /** The menu's line. */
  label: string;
  /** The quieter line under it: what the choice uses, and what it needs. */
  description: string;
  /** ⌘K's row. */
  paletteLabel: string;
  checked: boolean;
}

/** "This Mac", with the account it reads the host as once that's known. */
export function thisMacLabel(login: string | null | undefined): string {
  return login ? `This Mac · @${login}` : "This Mac";
}

/**
 * The menu's and ⌘K's three choices. Automatic names what the policy would pick right
 * now, even while another choice is checked.
 */
export function readSourceChoices({
  choice,
  automaticPick,
  login,
  host = null,
}: {
  choice: ReadChoice;
  automaticPick: "mac" | "host";
  login: string | null | undefined;
  /** The GitHub host this Mac would read the PR from. */
  host?: string | null;
}): ReadSourceChoice[] {
  const automaticTarget = automaticPick === "mac" ? "this Mac" : "session’s machine";
  const hostName = host ?? "GitHub";
  const account = login ? `@${login}` : null;
  const macAccount = account ?? "its signed-in account";
  return [
    {
      choice: "automatic",
      label: `Automatic (${automaticTarget})`,
      description: `${automaticPick === "mac" ? "This Mac" : "The session’s machine"} now. Follows Settings and whether this Mac can read ${hostName}.`,
      paletteLabel: "Read This Pull Request Automatically",
      checked: choice === "automatic",
    },
    {
      choice: "mac",
      label: thisMacLabel(login),
      description: `Uses gh on this Mac as ${macAccount}. Works while the session’s machine is off.`,
      paletteLabel: "Read This Pull Request on This Mac",
      checked: choice === "mac",
    },
    {
      choice: "session",
      label: SESSION_MACHINE_LABEL,
      description:
        "Uses gh on the session’s machine, as the account signed in there. Needs it running.",
      paletteLabel: "Read This Pull Request on the Session’s Machine",
      checked: choice === "session",
    },
  ];
}

/** The header trigger's text: where the PR is read, and the account the Mac reads it as. */
export interface ReadSourceTriggerText {
  place: string;
  /** `@login`, shown after the place and the first to give way in a narrow header. */
  account: string | null;
  /** Both together, for the trigger's tooltip and accessible name. */
  full: string;
}

/** The header trigger's text for the effective route; a blocked one names this Mac alone. */
export function readSourceTriggerText(
  route: ReadRoute,
  login: string | null | undefined,
): ReadSourceTriggerText {
  if (route.kind === "host") {
    return { place: SESSION_MACHINE_LABEL, account: null, full: SESSION_MACHINE_LABEL };
  }
  // A repository whose chosen login can't be used still names it, so it's clear who's waited on.
  const shownLogin =
    route.kind === "mac" || (route.kind === "blocked" && route.reason === "repo_account")
      ? login
      : null;
  return {
    place: "This Mac",
    account: shownLogin ? `@${shownLogin}` : null,
    full: thisMacLabel(shownLogin),
  };
}
