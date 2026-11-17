// Fixture modules for the Settings source guard tests: the catalog and shared dialog they import,
// and a guard built over any set of fixture sources.

import path from "node:path";
import { fileURLToPath } from "node:url";

import { type SettingsEntry, settingsEntryFamily } from "@/pages/settings/search/catalog/types";

import {
  catalogFromModules,
  createSettingsProgram,
  type GuardViolation,
  guardSettingsSource,
} from "./settingsSourceGuard";

export const WEB_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
export const FIXTURE_DIR = path.join(WEB_ROOT, "src/pages/settings/guard-fixtures");
export const CATALOG_FILE = path.join(
  WEB_ROOT,
  "src/pages/settings/search/catalog/guardFixture.ts",
);

/** A fixture module's path, as the guard sees it. */
export const fixtureFile = (name: string) => path.join(FIXTURE_DIR, `${name}.tsx`);

export const FIXTURE_UNLISTED: SettingsEntry = {
  id: "fixture.unlisted",
  section: "general",
  label: "Unlisted",
  description: "Declared, but missing from the searchable list.",
};

export const FIXTURE_SETTING: SettingsEntry = {
  id: "fixture.sync",
  section: "general",
  label: "Sync",
  description: "Keeps things in sync.",
  parts: { reset: "Reset sync" },
  choices: ["Fast", "Slow"],
  overlays: { reset: { dialogActions: ["Cancel", "Reset"] } },
  regions: {
    items: {
      rowActions: { delete: "Delete" },
      rowFields: { token: { before: "Token for " } },
      rowOverlays: {
        delete: {
          rowActions: { cancel: "Cancel", confirm: "Delete" },
          rowFields: { reason: { before: "Why delete " } },
        },
      },
    },
    hosts: { rowActions: { signIn: "Sign in…" } },
    accounts: { rowActions: { signOut: "Sign out" } },
    servers: { rowActions: { open: { dataName: true } } },
    tools: { rowActions: { expand: { dataName: true } } },
  },
  contentAreas: ["doc", "notes"],
};

export const IMPORTS = `
import { Fragment, useMemo } from "react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent } from "@/components/ui/popover";
import { Select, SelectContent, SelectItem, SelectTrigger } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { ThemeColorPicker } from "@/components/theme/ThemeColorPicker";
import { FIXTURE_SETTING } from "@/pages/settings/search/catalog/guardFixture";
import { settingsControlProps } from "@/pages/settings/search/settingsControl";
import {
  SettingsDataRegion,
  settingsOverlayProps,
  settingsRowOverlayProps,
} from "@/pages/settings/search/settingsOwnership";
import { SettingsItem } from "@/pages/settings/search/SettingsItem";
`;

/** A row whose Copy part is named around the command it copies. */
export const FIXTURE_COPY: SettingsEntry = {
  id: "fixture.copy",
  section: "general",
  label: "Link command",
  description: "A command to run.",
  parts: { copy: "Copy the link command", open: "Open the config folder" },
  nameTemplates: { copy: { before: "Copy ", variants: { copied: { before: "Copied " } } } },
  // The open part's list is of apps the shell finds, plus None.
  overlays: { open: { runtimeChoices: { fixed: ["None"] } } },
};

/** A family of two rows built alike; only alpha has the extra part. */
export const FAMILY_BUILD = (member: "alpha" | "beta"): SettingsEntry => ({
  id: `fixture.family-${member}`,
  section: "general",
  label: `Family ${member}`,
  description: "A family member.",
  parts: member === "alpha" ? { open: "Open", extra: "Extra" } : { open: "Open" },
  overlays: { open: { runtimeChoices: { fixed: ["None"] } } },
  regions: {
    same: { rowActions: { remove: "Remove" } },
    differs: { rowActions: { remove: member === "alpha" ? "Remove" : "Delete" } },
  },
});
export const FIXTURE_FAMILY = settingsEntryFamily({
  key: "fixture-family",
  members: ["alpha", "beta"],
  build: FAMILY_BUILD,
});
export const FIXTURE_ALPHA = FIXTURE_FAMILY.subset("fixture-alpha", ["alpha"]);
export const FIXTURE_BETA = FIXTURE_FAMILY.subset("fixture-beta", ["beta"]);
export const FIXTURE_REVERSED = FIXTURE_FAMILY.subset("fixture-reversed", ["beta", "alpha"]);
// Another family over the same members, with its own entries.
export const FIXTURE_OTHER = settingsEntryFamily({
  key: "fixture-other",
  members: ["alpha", "beta"],
  build: (member) => ({ ...FAMILY_BUILD(member), id: `fixture.other-${member}` }),
});
// The family's own entries, each under the other member.
export const FIXTURE_SWAPPED = settingsEntryFamily({
  key: "fixture-swapped",
  members: ["alpha", "beta"],
  build: (member) => FAMILY_BUILD(member === "alpha" ? "beta" : "alpha"),
});
// A copy that keeps the family's members and entries but looks up something else.
export const FORGED_FAMILY = { ...FIXTURE_FAMILY, entry: () => FIXTURE_SETTING };

/**
 * A fix dialog's three openers, each a family over three members. Its parts exist only where a
 * member can reach them: Install on alpha and beta, a one-time code on beta, a switch
 * confirmation on alpha and beta from the credential opener only, and an API key on alpha and
 * gamma. Try again is everywhere.
 */
export const FIX_BUILD =
  (opener: "setUp" | "fixAction" | "credentialAction") =>
  (member: "alpha" | "beta" | "gamma"): SettingsEntry => {
    const inside = { overlay: opener };
    const parts: Record<string, string> = {
      [opener]: "Fix it",
      retry: "Try again",
      copyLink: "Copy link",
    };
    const presence: Record<string, { overlay: string }> = { retry: inside, copyLink: inside };
    if (member !== "gamma") {
      parts.installAction = "Install";
      presence.installAction = inside;
    }
    if (member === "beta") {
      parts.copyCode = "Copy one-time code";
      presence.copyCode = inside;
    }
    if (opener === "credentialAction" && member !== "gamma") {
      parts.switchConfirm = "Sign out and switch";
      presence.switchConfirm = inside;
    }
    if (member !== "beta") {
      parts.apiKey = "API key";
      presence.apiKey = inside;
    }
    return {
      id: `fixture.fix-${opener}-${member}`,
      section: "general",
      label: `Fix ${member}`,
      description: "A fix dialog.",
      parts,
      presence,
      // The link a sign-in copies is named around the link itself.
      nameTemplates: { copyLink: { before: "Copy " } },
      overlays: { [opener]: {} },
    };
  };
export const FIX_MEMBERS = ["alpha", "beta", "gamma"] as const;
export const FIX_CARD = settingsEntryFamily({
  key: "fix-card",
  members: FIX_MEMBERS,
  build: FIX_BUILD("setUp"),
});
export const FIX_PAGE = settingsEntryFamily({
  key: "fix-page",
  members: FIX_MEMBERS,
  build: FIX_BUILD("fixAction"),
});
export const FIX_CREDENTIAL = settingsEntryFamily({
  key: "fix-credential",
  members: FIX_MEMBERS,
  build: FIX_BUILD("credentialAction"),
});

export const CATALOG_SOURCE = `import { settingsEntry, settingsEntryFamily } from "./types";
export const FAMILY_BUILD = ${FAMILY_BUILD.toString()};
export const FIXTURE_FAMILY = settingsEntryFamily({ key: "fixture-family", members: ["alpha", "beta"], build: FAMILY_BUILD });
export const FIXTURE_ALPHA = FIXTURE_FAMILY.subset("fixture-alpha", ["alpha"]);
export const FIXTURE_BETA = FIXTURE_FAMILY.subset("fixture-beta", ["beta"]);
export const FIXTURE_REVERSED = FIXTURE_FAMILY.subset("fixture-reversed", ["beta", "alpha"]);
export const FIXTURE_OTHER = settingsEntryFamily({ key: "fixture-other", members: ["alpha", "beta"], build: (member) => ({ ...FAMILY_BUILD(member), id: "fixture.other-" + member }) });
export const FIXTURE_SWAPPED = settingsEntryFamily({ key: "fixture-swapped", members: ["alpha", "beta"], build: (member) => FAMILY_BUILD(member === "alpha" ? "beta" : "alpha") });
export const FORGED_FAMILY = { ...FIXTURE_FAMILY, entry: (_member: "alpha" | "beta") => FIXTURE_SETTING };
// Looks like a family, but settingsEntryFamily didn't make it.
export const FAKE_FAMILY = { entry: (member: string) => FIXTURE_SETTING };
export const FIX_BUILD = ${FIX_BUILD.toString()};
export const FIX_MEMBERS = ["alpha", "beta", "gamma"] as const;
export const FIX_CARD = settingsEntryFamily({ key: "fix-card", members: FIX_MEMBERS, build: FIX_BUILD("setUp") });
export const FIX_PAGE = settingsEntryFamily({ key: "fix-page", members: FIX_MEMBERS, build: FIX_BUILD("fixAction") });
export const FIX_CREDENTIAL = settingsEntryFamily({ key: "fix-credential", members: FIX_MEMBERS, build: FIX_BUILD("credentialAction") });
export const FIX_INSTALLABLE = ["alpha", "beta"] as const;
export const FIX_DEVICE = ["beta"] as const;
export const FIX_SWITCH = ["alpha", "beta"] as const;
export const FIX_FORM = ["alpha", "gamma"] as const;
export const FIXTURE_SETTING = settingsEntry(${JSON.stringify(FIXTURE_SETTING)});
export const FIXTURE_COPY = settingsEntry(${JSON.stringify(FIXTURE_COPY)});
export const FIXTURE_UNLISTED = settingsEntry(${JSON.stringify(FIXTURE_UNLISTED)});
`;

// Outside pages/settings, reached only through an alias, memo(), a helper without JSX and a callback.
// Outside pages/settings and the roots, reached only as JSX tags of exported wrappers.
// A shared fix dialog, outside pages/settings: it takes its parts from the overlay owner the
// Settings site renders it under, narrowing each section to the members that can show it.
export const FIX_DIALOG_FILE = path.join(WEB_ROOT, "src/components/guard-fixtures/FixDialog.tsx");
export const FIX_DIALOG_SOURCE = `
import { Button } from "@/components/ui/button";
import { DialogContent } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  FIX_DEVICE,
  FIX_FORM,
  FIX_INSTALLABLE,
  FIX_SWITCH,
} from "@/pages/settings/search/catalog/guardFixture";
import { useSettingsOverlayParts } from "@/pages/settings/search/settingsOwnership";
export function FixDialog({ phase }: { phase: string }) {
  const parts = useSettingsOverlayParts();
  const install = parts.within(FIX_INSTALLABLE);
  const device = parts.within(FIX_DEVICE);
  const confirm = parts.within(FIX_SWITCH, "credentialAction");
  const form = parts.within(FIX_FORM);
  return (
    <DialogContent {...parts.overlay()}>
      {install && phase === "install" && (
        <Button {...install.control("installAction", { outside: {} })}>Install</Button>
      )}
      {device ? <Button {...device.control("copyCode", { outside: {} })}>Copy</Button> : null}
      {confirm && (
        <confirm.Provider>
          <SwitchConfirm />
        </confirm.Provider>
      )}
      {form && (
        <form.Provider>
          <KeyForm />
        </form.Provider>
      )}
      <Button {...parts.control("retry", { outside: {} })}>Try again</Button>
      <Button {...parts.control("copyLink", { name: { runtime: phase }, outside: {} })} />
    </DialogContent>
  );
}
// A templated part named without its runtime value.
export function MissingRuntime() {
  const parts = useSettingsOverlayParts();
  return (
    <DialogContent {...parts.overlay()}>
      <Button {...parts.control("copyLink", { outside: {} })} />
    </DialogContent>
  );
}
export function SwitchConfirm() {
  const parts = useSettingsOverlayParts();
  return <Button {...parts.control("switchConfirm", { outside: {} })}>Sign out and switch</Button>;
}
export function KeyForm() {
  const parts = useSettingsOverlayParts();
  return <Input {...parts.control("apiKey", { outside: {} })} />;
}
// The credential form is only on alpha and gamma, so rendering it under every member is refused.
export function UnnarrowedForm() {
  const parts = useSettingsOverlayParts();
  return (
    <DialogContent {...parts.overlay()}>
      <KeyForm />
    </DialogContent>
  );
}
// Install is only on alpha and beta, so offering it to every member is refused.
export function UnnarrowedInstall() {
  const parts = useSettingsOverlayParts();
  return (
    <DialogContent {...parts.overlay()}>
      <Button {...parts.control("installAction", { outside: {} })}>Install</Button>
    </DialogContent>
  );
}
// The confirmation's narrowing is empty under the card's owner: used ungated, it's refused.
export function UngatedConfirm() {
  const parts = useSettingsOverlayParts();
  const confirm = parts.within(FIX_SWITCH, "credentialAction");
  return (
    <DialogContent {...parts.overlay()}>
      <Button {...confirm!.control("switchConfirm", { outside: {} })}>Sign out</Button>
    </DialogContent>
  );
}
export function UngatedProvider() {
  const parts = useSettingsOverlayParts();
  const confirm = parts.within(FIX_SWITCH, "credentialAction");
  return (
    <DialogContent {...parts.overlay()}>
      <confirm.Provider>
        <SwitchConfirm />
      </confirm.Provider>
    </DialogContent>
  );
}
// Gated by another getter, which isn't empty: the confirmation under it is still checked.
export function OtherGate() {
  const parts = useSettingsOverlayParts();
  const install = parts.within(FIX_INSTALLABLE);
  const confirm = parts.within(FIX_SWITCH, "credentialAction");
  return (
    <DialogContent {...parts.overlay()}>
      {install && <Button {...confirm!.control("switchConfirm", { outside: {} })}>Sign out</Button>}
    </DialogContent>
  );
}
// A ternary's other arm runs where its getter is empty, so it's checked as usual.
export function OtherArm() {
  const parts = useSettingsOverlayParts();
  const confirm = parts.within(FIX_SWITCH, "credentialAction");
  return (
    <DialogContent {...parts.overlay()}>
      {confirm ? (
        <Button {...confirm.control("switchConfirm", { outside: {} })}>Sign out</Button>
      ) : (
        <Button>Raw</Button>
      )}
    </DialogContent>
  );
}
// A part passed in as a finite union: every candidate is checked on every member it may be.
export function UnionParts({ part }: { part: "retry" | "installAction" }) {
  const parts = useSettingsOverlayParts();
  const install = parts.within(FIX_INSTALLABLE);
  return (
    <DialogContent {...parts.overlay()}>
      {install && <Button {...install.control(part, { outside: {} })}>Act</Button>}
    </DialogContent>
  );
}
export function UnionMissing({ part }: { part: "retry" | "copyCode" }) {
  const parts = useSettingsOverlayParts();
  const install = parts.within(FIX_INSTALLABLE);
  return (
    <DialogContent {...parts.overlay()}>
      {install && <Button {...install.control(part, { outside: {} })}>Act</Button>}
    </DialogContent>
  );
}
export function UnionOutsideOverlay({ part }: { part: "retry" | "setUp" }) {
  const parts = useSettingsOverlayParts();
  return (
    <DialogContent {...parts.overlay()}>
      <Button {...parts.control(part, { outside: {} })}>Act</Button>
    </DialogContent>
  );
}
// One of the union is templated, so it needs the runtime value.
export function UnionTemplated({ part }: { part: "retry" | "copyLink" }) {
  const parts = useSettingsOverlayParts();
  return (
    <DialogContent {...parts.overlay()}>
      <Button {...parts.control(part, { outside: {} })} />
    </DialogContent>
  );
}
// The opener is a part of the entry, but not one of the overlay it opens.
export function OpenerInside() {
  const parts = useSettingsOverlayParts();
  return (
    <DialogContent {...parts.overlay()}>
      <Button {...parts.control("setUp", { outside: {} })}>Fix it</Button>
    </DialogContent>
  );
}
// Members from a variable aren't a literal list, so nothing proves this narrowing empty.
export function LooseMembers({ members }: { members: string[] }) {
  const parts = useSettingsOverlayParts();
  const some = parts.within(members);
  return (
    <DialogContent {...parts.overlay()}>
      {some && <Button {...some.control("copyCode", { outside: {} })}>Copy</Button>}
    </DialogContent>
  );
}
`;

export interface FixtureGuard {
  /** The violations of walking the given modules, or only the given functions in them. */
  check: (
    rootFiles: string[],
    entryFunctions?: readonly { file: string; name: string }[],
  ) => GuardViolation[];
  /** Catalog entries declared but missing from the searchable list. */
  unlisted: string[];
}

/**
 * The guard over fixture sources, with the catalog and the shared dialog as virtual modules.
 * `extraFiles` adds more virtual modules, by absolute path.
 */
export function createFixtureGuard(
  fixtures: Readonly<Record<string, string>>,
  extraFiles: ReadonlyMap<string, string> = new Map(),
): FixtureGuard {
  const virtualFiles = new Map<string, string>([
    [CATALOG_FILE, CATALOG_SOURCE],
    [FIX_DIALOG_FILE, FIX_DIALOG_SOURCE],
    ...extraFiles,
    ...Object.entries(fixtures).map(
      ([name, source]) =>
        [
          name === "reexports" ? path.join(FIXTURE_DIR, "reexports.ts") : fixtureFile(name),
          source,
        ] as [string, string],
    ),
  ]);
  const program = createSettingsProgram(WEB_ROOT, [...virtualFiles.keys()], virtualFiles);
  const { catalog, families, unlisted } = catalogFromModules(
    {
      [CATALOG_FILE]: {
        FIXTURE_SETTING,
        FIXTURE_COPY,
        FIXTURE_UNLISTED,
        FIXTURE_FAMILY,
        FIXTURE_ALPHA,
        FIXTURE_BETA,
        FIXTURE_REVERSED,
        FIXTURE_OTHER,
        FIXTURE_SWAPPED,
        FORGED_FAMILY,
        FAKE_FAMILY: { entry: () => FIXTURE_SETTING },
        FIX_CARD,
        FIX_PAGE,
        FIX_CREDENTIAL,
      },
    },
    [
      FIXTURE_SETTING,
      FIXTURE_COPY,
      ...FIXTURE_FAMILY.entries,
      ...FIXTURE_OTHER.entries,
      ...FIXTURE_SWAPPED.entries,
      ...FIX_CARD.entries,
      ...FIX_PAGE.entries,
      ...FIX_CREDENTIAL.entries,
    ],
  );
  return {
    unlisted,
    check: (rootFiles, entryFunctions) =>
      guardSettingsSource({
        program,
        webRoot: WEB_ROOT,
        rootFiles,
        entryFunctions,
        catalog,
        families,
      }),
  };
}
