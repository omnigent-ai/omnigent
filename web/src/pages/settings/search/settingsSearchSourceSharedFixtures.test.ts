// The Settings source guard on shared components and what the walk reaches: how each shared
// control is owned, and which modules the guard follows to find a control's uses.

import fs from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

import type { GuardViolation } from "@/test/settingsSourceGuard";
import {
  CATALOG_FILE,
  IMPORTS,
  WEB_ROOT,
  createFixtureGuard,
  fixtureFile,
} from "@/test/settingsSourceGuardFixtures";

const OUTSIDE_FILE = path.join(WEB_ROOT, "src/components/guard-fixtures/raw.tsx");

// A component shared by Settings and other surfaces: it takes its owner from the Settings site.
const SHARED_FILE = path.join(WEB_ROOT, "src/components/guard-fixtures/Shared.tsx");

const SHARED_SOURCE = `
import { FIXTURE_COPY } from "@/pages/settings/search/catalog/guardFixture";
import { SettingsOwner, useSettingsOwnership } from "@/pages/settings/search/settingsOwnership";
export function SharedCopy({ command }: { command: string }) {
  const owned = useSettingsOwnership();
  return <button {...owned.control({ name: { runtime: command }, outside: { "aria-label": "Copy " + command } })} />;
}
export function SharedNoRuntime() {
  const owned = useSettingsOwnership();
  return <button {...owned.control({ outside: { "aria-label": "Copy" } })} />;
}
export function SharedOverride({ command }: { command: string }) {
  const owned = useSettingsOwnership();
  return <button {...owned.control({ name: { runtime: command } })} aria-label="Copy anything" />;
}
// Its hook reads the owner it's rendered under, not the one it renders around the control.
export function SharedNested({ command }: { command: string }) {
  const owned = useSettingsOwnership();
  return (
    <SettingsOwner entry={FIXTURE_COPY} part="open">
      <button {...owned.control({ outside: { "aria-label": "Copy " + command } })} />
      <SharedOpen />
    </SettingsOwner>
  );
}
export function SharedOpen() {
  const owned = useSettingsOwnership();
  return <button {...owned.control({ outside: { "aria-label": "Open" } })} />;
}
export function SharedTree({ depth }: { depth: number }) {
  const owned = useSettingsOwnership();
  return (
    <div>
      <button {...owned.control({ name: { runtime: "x" } })} />
      {depth > 0 && <SharedTree depth={depth - 1} />}
    </div>
  );
}
export function SharedRaw({ command }: { command: string }) {
  const owned = useSettingsOwnership();
  return (
    <div>
      <button {...owned.control({ name: { runtime: command } })} />
      <input aria-label="A control of its own" />
    </div>
  );
}
`;
const SHARED_IMPORTS = `${IMPORTS}
import { FIXTURE_COPY } from "@/pages/settings/search/catalog/guardFixture";
import { SettingsOwner, useSettingsOwnership } from "@/pages/settings/search/settingsOwnership";
import { SharedCopy, SharedNested, SharedNoRuntime, SharedOverride, SharedRaw, SharedTree } from "@/components/guard-fixtures/Shared";
`;

const WRAPPED_FILE = path.join(WEB_ROOT, "src/components/guard-fixtures/Wrapped.tsx");
const WRAPPED_SOURCE = `
import { Component, memo } from "react";
function Raw() {
  return <input aria-label="Through an imported alias" />;
}
function MemoTarget() {
  return <input aria-label="Through an imported memo" />;
}
export const Alias = Raw;
export const MemoRaw = memo(MemoTarget);
export class ClassRaw extends Component {
  render() {
    return <input aria-label="Class setting" />;
  }
}
`;

const OUTSIDE_SOURCE = `
export function AliasedRaw() {
  return <input aria-label="Through an alias" />;
}
export function MemoizedRaw() {
  return <input aria-label="Through memo" />;
}
function CalledRaw() {
  return <input aria-label="Through a helper" />;
}
export function getContent() {
  return CalledRaw();
}
export function renderRow(row: number) {
  return <input key={row} aria-label="Through a callback" />;
}
`;

// A never-rendered raw control added to the admin pages, which are scanned like any other.
function withRawControl(file: string, marker: RegExp): string {
  const source = fs.readFileSync(file, "utf8");
  const changed = source.replace(
    marker,
    (match) =>
      `${match}\n  const NEVER = false as boolean;\n  if (NEVER) return <input aria-label="Unregistered" />;`,
  );
  expect(changed).not.toBe(source);
  return changed;
}

const shared = (body: string) => `${SHARED_IMPORTS}
export function Row() {
  return <div>${body}</div>;
}
`;

const SHARING_PAGE = path.join(WEB_ROOT, "src/pages/SharingPage.tsx");
const POLICIES_PAGE = path.join(WEB_ROOT, "src/pages/PoliciesPage.tsx");

const FIXTURES: Record<string, string> = {
  aliasedComponent: `${IMPORTS}
const Picker = ThemeColorPicker;
export function AliasedPicker() {
  return <Picker label="Accent" value="#fff" testId="t" onChange={() => {}} />;
}
`,
  componentTable: `${IMPORTS}
const panels = [{ component: ThemeColorPicker }];
export function ComponentTable() {
  return panels.map(({ component: Control }) => (
    <Control label="Accent" value="#fff" testId="t" onChange={() => {}} />
  ));
}
`,
  memoComponent: `${IMPORTS}
import { memo } from "react";
const MemoPicker = memo(ThemeColorPicker);
export function MemoizedRow() {
  return <MemoPicker label="Accent" value="#fff" testId="t" onChange={() => {}} />;
}
`,
  contentLoose: `${IMPORTS}
import ReactMarkdown, { type Components } from "react-markdown";
import { SettingsContent } from "@/pages/settings/search/settingsOwnership";
// Not made by settingsContentComponents, so its link is a control like any other.
const LOOSE: Components = {
  a: ({ href, children }) => <a href={href}>{children}</a>,
};
export function LooseDocs({ text, plugins }: { text: string; plugins: [] }) {
  return (
    <SettingsItem entry={FIXTURE_SETTING}>
      <SettingsContent entry={FIXTURE_SETTING} area="doc">
        <ReactMarkdown skipHtml components={LOOSE}>{text}</ReactMarkdown>
      </SettingsContent>
    </SettingsItem>
  );
}
`,
  neverRendered: `${IMPORTS}
const SHOW = false;
export function Gated() {
  return <div>{SHOW && <input aria-label="Hidden setting" />}</div>;
}
function Unused() {
  return <Switch aria-label="Never rendered" />;
}
`,
  importsHelper: `${IMPORTS}
import { HelperRow } from "./helper";
export function UsesHelper() {
  return <HelperRow />;
}
`,
  helper: `${IMPORTS}
export function HelperRow() {
  return <Switch aria-label="From an imported module" />;
}
`,
  closedDialog: `${IMPORTS}
export function ClosedDialog() {
  return (
    <Dialog open={false}>
      <DialogContent {...settingsOverlayProps(FIXTURE_SETTING, "reset")}>
        <Switch aria-label="Setting inside a dialog" />
        <Button data-settings-dialog-action="Apply">Apply</Button>
      </DialogContent>
    </Dialog>
  );
}
`,
  unlinkedDialog: `${IMPORTS}
export function UnlinkedDialog() {
  return (
    <Dialog>
      <DialogContent>
        <p>No controls, but the portal names no owner.</p>
      </DialogContent>
    </Dialog>
  );
}
`,
  popoverOnly: `${IMPORTS}
import { PopoverBody } from "./popoverBody";
export function PopoverHost() {
  return (
    <Popover>
      <PopoverContent {...settingsOverlayProps(FIXTURE_SETTING)}>
        <PopoverBody />
      </PopoverContent>
    </Popover>
  );
}
`,
  popoverBody: `${IMPORTS}
export function PopoverBody() {
  return <Input aria-label="Only inside the popover" />;
}
`,
  aliases: `${IMPORTS}
import { Switch as Toggle } from "@/components/ui/switch";
import { ReexportedRow } from "./reexports";
export function Aliased() {
  return (
    <>
      <Toggle aria-label="Aliased" />
      <ReexportedRow />
    </>
  );
}
`,
  reexports: `export { HelperRow as ReexportedRow } from "./helper";\n`,
  indirection: `${IMPORTS}
import { memo } from "react";
import { AliasedRaw, MemoizedRaw, getContent, renderRow } from "@/components/guard-fixtures/raw";
const Alias = AliasedRaw;
const MemoRaw = memo(MemoizedRaw);
const SHOW = false as boolean;
export function Indirect() {
  if (!SHOW) return null;
  return (
    <div>
      <Alias />
      <MemoRaw />
      {getContent()}
      {[1, 2].map(renderRow)}
    </div>
  );
}
`,
  importedAlias: `${IMPORTS}
import { Alias } from "@/components/guard-fixtures/Wrapped";
const SHOW = false as boolean;
export function UsesAlias() {
  return SHOW ? <Alias /> : null;
}
`,
  importedMemo: `${IMPORTS}
import { MemoRaw } from "@/components/guard-fixtures/Wrapped";
const SHOW = false as boolean;
export function UsesMemo() {
  return SHOW ? <MemoRaw /> : null;
}
`,
  importedClass: `${IMPORTS}
import { ClassRaw } from "@/components/guard-fixtures/Wrapped";
const SHOW = false as boolean;
export function UsesClass() {
  return SHOW ? <ClassRaw /> : null;
}
`,
  unlistedEntry: `${IMPORTS}
import { FIXTURE_UNLISTED } from "@/pages/settings/search/catalog/guardFixture";
export function Unlisted() {
  return <input {...settingsControlProps(FIXTURE_UNLISTED)} />;
}
`,
  entries: `${IMPORTS}
import { settingsEntry } from "@/pages/settings/search/catalog/types";
const LOCAL = settingsEntry({ id: "x", section: "general", label: "X", description: "" });
export function Fabricated() {
  return (
    <>
      <Switch {...settingsControlProps({ id: "y", section: "general", label: "Y", description: "" })} />
      <Switch {...settingsControlProps(LOCAL)} />
    </>
  );
}
`,
  unknownPart: `${IMPORTS}
export function UnknownPart() {
  return <Button {...settingsControlProps(FIXTURE_SETTING, "missing" as never)}>Missing</Button>;
}
`,
  sharedOwned: shared(
    '<SettingsOwner entry={FIXTURE_COPY} part="copy"><SharedCopy command="ln -s a b" /></SettingsOwner>',
  ),
  sharedUnowned: shared('<SharedCopy command="ln -s a b" />'),
  sharedUndeclaredPart: shared(
    '<SettingsOwner entry={FIXTURE_COPY} part="paste"><SharedCopy command="x" /></SettingsOwner>',
  ),
  sharedNoRuntime: shared(
    '<SettingsOwner entry={FIXTURE_COPY} part="copy"><SharedNoRuntime /></SettingsOwner>',
  ),
  sharedOverride: shared(
    '<SettingsOwner entry={FIXTURE_COPY} part="copy"><SharedOverride command="x" /></SettingsOwner>',
  ),
  sharedRawControl: shared(
    '<SettingsOwner entry={FIXTURE_COPY} part="copy"><SharedRaw command="x" /></SettingsOwner>',
  ),
  templatedDirect: shared(
    '<button {...settingsControlProps(FIXTURE_COPY, "copy")} /><button {...settingsControlProps(FIXTURE_COPY, "copy", { runtime: "x" })} title="Copy" />',
  ),
  templatedDirectNamed: shared(
    '<button {...settingsControlProps(FIXTURE_COPY, "copy", { runtime: "x" })} />',
  ),
  templatedNamingSpreads: `${SHARED_IMPORTS}
const wrong = { "aria-label": "Copy wrong" };
const quiet = { className: "quiet" };
export function Row({ extra }: { extra: object }) {
  const direct = settingsControlProps(FIXTURE_COPY, "copy", { runtime: "x" });
  return (
    <div>
      <button {...direct} {...{ "aria-label": "Copy wrong" }} />
      <button {...direct} {...wrong} />
      <button {...direct} {...{ ...quiet, title: "Copy" }} />
      <button {...direct} {...extra} />
      <button {...direct} {...quiet} />
    </div>
  );
}
`,
  templatedSecondOwnerSpread: `${SHARED_IMPORTS}
export function Row() {
  const owned = useSettingsOwnership();
  const named = settingsControlProps(FIXTURE_COPY, "copy", { runtime: "x" });
  return (
    <div>
      <button {...named} {...owned.control({ outside: { "aria-label": "Copy wrong" } })} />
      <button {...named} {...settingsControlProps(FIXTURE_COPY, "copy")} />
    </div>
  );
}
`,
  unionParts: `${SHARED_IMPORTS}
type Action = "copy" | "open";
export function Row({ action, other, any, never, maybe }: {
  action: Action;
  other: "copy" | "paste";
  any: string;
  never: never;
  maybe: Action | undefined;
}) {
  const single = "open" as const;
  return (
    <div>
      <button {...settingsControlProps(FIXTURE_COPY, action, { runtime: "x" })} />
      <button {...settingsControlProps(FIXTURE_COPY, single)} />
      <button {...settingsControlProps(FIXTURE_COPY, action)} />
      <button {...settingsControlProps(FIXTURE_COPY, action, { runtime: "x" })} aria-label="Copy" />
      <button {...settingsControlProps(FIXTURE_COPY, other, { runtime: "x" })} />
      <button {...settingsControlProps(FIXTURE_COPY, any)} />
      <button {...settingsControlProps(FIXTURE_COPY, never)} />
      <button {...settingsControlProps(FIXTURE_COPY, maybe, { runtime: "x" })} />
    </div>
  );
}
`,
  runtimeChoices: `${SHARED_IMPORTS}
import { settingsRuntimeChoiceProps } from "@/pages/settings/search/settingsOwnership";
const openPart = "open";
export function Row({ apps }: { apps: string[] }) {
  return (
    <div>
      <Select>
        <SelectTrigger {...settingsControlProps(FIXTURE_COPY, "open")} />
        <SelectContent {...settingsOverlayProps(FIXTURE_COPY, "open")}>
          {apps.map((app) => (
            <SelectItem key={app} {...settingsRuntimeChoiceProps(FIXTURE_COPY, "open")} value={app}>{app}</SelectItem>
          ))}
          <SelectItem value="none">None</SelectItem>
          <SelectItem {...settingsRuntimeChoiceProps(FIXTURE_COPY, openPart)} value="p">P</SelectItem>
          <button {...settingsRuntimeChoiceProps(FIXTURE_COPY, "open")} />
        </SelectContent>
      </Select>
      <Select>
        <SelectTrigger {...settingsControlProps(FIXTURE_COPY)} />
        <SelectContent {...settingsOverlayProps(FIXTURE_COPY)}>
          <SelectItem {...settingsRuntimeChoiceProps(FIXTURE_COPY, "open")} value="a">A</SelectItem>
        </SelectContent>
      </Select>
      <Select>
        <SelectTrigger {...settingsControlProps(FIXTURE_COPY, "copy", { runtime: "x" })} />
        <SelectContent {...settingsOverlayProps(FIXTURE_COPY, "copy")}>
          <SelectItem {...settingsRuntimeChoiceProps(FIXTURE_COPY, "copy")} value="b">B</SelectItem>
        </SelectContent>
      </Select>
      <input {...settingsControlProps(FIXTURE_COPY, "open")} list="apps" />
      <datalist {...settingsOverlayProps(FIXTURE_COPY, "open")} id="apps">
        {apps.map((app) => (
          <option key={app} {...settingsRuntimeChoiceProps(FIXTURE_COPY, "open")} value={app} />
        ))}
      </datalist>
    </div>
  );
}
`,
  overlayUnionParts: `${SHARED_IMPORTS}
import { settingsRuntimeChoiceProps } from "@/pages/settings/search/settingsOwnership";
export function Row({ either, missing }: { either: "open" | "copy"; missing: "open" | "missing" }) {
  return (
    <div>
      <Select>
        <SelectTrigger {...settingsControlProps(FIXTURE_COPY, "open")} />
        <SelectContent {...settingsOverlayProps(FIXTURE_COPY, either)}>
          <SelectItem {...settingsRuntimeChoiceProps(FIXTURE_COPY, "open")} value="a">A</SelectItem>
        </SelectContent>
      </Select>
      <Select>
        <SelectTrigger {...settingsControlProps(FIXTURE_COPY, "open")} />
        <SelectContent {...settingsOverlayProps(FIXTURE_COPY, missing)}>
          <SelectItem {...settingsRuntimeChoiceProps(FIXTURE_COPY, "open")} value="b">B</SelectItem>
        </SelectContent>
      </Select>
    </div>
  );
}
`,
  entrySection: `${IMPORTS}
export function Section() {
  return <Row />;
}
function Row() {
  return <button aria-label="Raw in the section" />;
}
export function OtherSection() {
  return <button aria-label="Raw elsewhere" />;
}
`,
  fileInputs: `${IMPORTS}
import { settingsFileInputProps } from "@/pages/settings/search/settingsControl";
import type { InputHTMLAttributes } from "react";
const resetPart = "reset";
function shown(): InputHTMLAttributes<HTMLInputElement> {
  return { type: "text", hidden: false, tabIndex: 0 };
}
export function Row({ visible, extra }: { visible: boolean; extra: object }) {
  return (
    <SettingsItem entry={FIXTURE_SETTING}>
      <input type="file" hidden {...settingsFileInputProps(FIXTURE_SETTING, "reset")} />
      <input type="file" {...settingsFileInputProps(FIXTURE_SETTING, "reset")} />
      <input type="file" hidden={visible} {...settingsFileInputProps(FIXTURE_SETTING, "reset")} />
      <input type="text" hidden {...settingsFileInputProps(FIXTURE_SETTING, "reset")} />
      <div hidden {...settingsFileInputProps(FIXTURE_SETTING, "reset")} />
      <input type="file" hidden {...settingsFileInputProps(FIXTURE_SETTING, resetPart)} />
      <input type="file" hidden {...settingsFileInputProps(FIXTURE_SETTING, "upload")} />
      <input type="file" hidden {...settingsFileInputProps(FIXTURE_SETTING, "reset")} tabIndex={0} />
      <input type="file" hidden {...settingsFileInputProps(FIXTURE_SETTING, "reset")} {...extra} />
      <input type="file" hidden {...settingsFileInputProps(FIXTURE_SETTING, "reset")} {...shown()} />
      <input type="file" hidden className="block" {...settingsFileInputProps(FIXTURE_SETTING, "reset")} />
    </SettingsItem>
  );
}
export function Outside() {
  return <input type="file" hidden {...settingsFileInputProps(FIXTURE_SETTING, "reset")} />;
}
`,
  radioGroups: `${IMPORTS}
import { IconSegmentedControl } from "@/components/theme/IconSegmentedControl";
import { SettingsOwner } from "@/pages/settings/search/settingsOwnership";
const ITEMS = [{ value: "fast", label: "Fast", icon: () => null, testId: "fast" }] as const;
export function Owned() {
  return (
    <div>
      <div role="radiogroup" {...settingsControlProps(FIXTURE_SETTING)}>
        <button role="radio" aria-checked="true">Fast</button>
      </div>
      <SettingsOwner entry={FIXTURE_SETTING}>
        <IconSegmentedControl labelledBy="x" value="fast" onSelect={() => {}} items={ITEMS} />
      </SettingsOwner>
    </div>
  );
}
import type { HTMLAttributes } from "react";
function FakeGroup(_props: HTMLAttributes<HTMLDivElement>) {
  return <input aria-label="Not a group" />;
}
function asGroup(): HTMLAttributes<HTMLDivElement> {
  return { role: "group" };
}
export function Claimed() {
  return (
    <div>
      <FakeGroup role="radiogroup" {...settingsControlProps(FIXTURE_SETTING)} />
      <div role="radiogroup" {...settingsControlProps(FIXTURE_SETTING)} {...asGroup()}>
        <button role="radio" aria-checked="true">Fast</button>
      </div>
    </div>
  );
}
export function Unowned() {
  return (
    <div>
      <div role="radiogroup">
        <button role="radio" aria-checked="true">Fast</button>
      </div>
      <div role="group" {...settingsControlProps(FIXTURE_SETTING)}>
        <button role="radio" aria-checked="true">Fast</button>
      </div>
      <IconSegmentedControl labelledBy="x" value="fast" onSelect={() => {}} items={ITEMS} />
    </div>
  );
}
`,
  templatedUndefinedRuntime: shared(
    '<button {...settingsControlProps(FIXTURE_COPY, "copy", undefined)} /><button {...settingsControlProps(FIXTURE_COPY, "copy", { runtime: undefined })} />',
  ),
  ownerPartConstants: `${SHARED_IMPORTS}
const copyPart = "copy";
const unknownPart = "unknown";
export function Row({ part, owner }: { part: string; owner: object }) {
  return (
    <div>
      <SettingsOwner entry={FIXTURE_COPY} part={copyPart}><SharedCopy command="x" /></SettingsOwner>
      <SettingsOwner entry={FIXTURE_COPY} part={unknownPart}><SharedCopy command="x" /></SettingsOwner>
      <SettingsOwner entry={FIXTURE_COPY} part={part}><SharedCopy command="x" /></SettingsOwner>
      <SettingsOwner entry={FIXTURE_COPY} {...owner}><SharedCopy command="x" /></SettingsOwner>
    </div>
  );
}
`,
  hookAboveOwner: `${SHARED_IMPORTS}
export function Row() {
  const owned = useSettingsOwnership();
  return (
    <SettingsOwner entry={FIXTURE_COPY} part="copy">
      <button {...owned.control({ name: { runtime: "x" }, outside: { "aria-label": "Copy x" } })} />
    </SettingsOwner>
  );
}
`,
  hookCapturedGetter: `${SHARED_IMPORTS}
export function Row() {
  const owned = useSettingsOwnership();
  const Copy = () => <button {...owned.control({ name: { runtime: "x" } })} />;
  return (
    <SettingsOwner entry={FIXTURE_COPY} part="copy">
      <Copy />
    </SettingsOwner>
  );
}
`,
  sharedRecursive: shared(
    '<SettingsOwner entry={FIXTURE_COPY} part="copy"><SharedTree depth={2} /></SettingsOwner>',
  ),
  hookNestedOwners: shared(
    '<SettingsOwner entry={FIXTURE_COPY} part="copy"><SharedNested command="x" /></SettingsOwner>',
  ),
};

const guard = createFixtureGuard(
  FIXTURES,
  new Map<string, string>([
    [SHARED_FILE, SHARED_SOURCE],
    [WRAPPED_FILE, WRAPPED_SOURCE],
    [OUTSIDE_FILE, OUTSIDE_SOURCE],
    [SHARING_PAGE, withRawControl(SHARING_PAGE, /export function SharingPage\([^)]*\)[^{]*\{/)],
    [POLICIES_PAGE, withRawControl(POLICIES_PAGE, /export function PoliciesPage\([^)]*\)[^{]*\{/)],
  ]),
);
const { unlisted } = guard;

const check = guard.check;
/** The violations' sites, `file › function › element`, for one fixture module. */
const sites = (name: string) =>
  check([fixtureFile(name)]).map((violation) => violation.site.replace(/^.*\//, ""));
const siteReasons = (name: string) =>
  check([fixtureFile(name)]).map(
    (violation) => `${violation.site.replace(/^.*\//, "")}: ${violation.reason}`,
  );

describe("Settings source guard on shared components and the walk's reach", () => {
  it("rejects a markdown map the helper didn't make, as untrusted", () => {
    const markdown =
      "A content area holds one ReactMarkdown with skipHtml, no HTML plugins, and components made by settingsContentComponents.";
    expect(siteReasons("contentLoose")).toEqual([
      "contentLoose.tsx › <module> › a: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      `contentLoose.tsx › LooseDocs › SettingsContent: ${markdown}`,
    ]);
  });

  it("rejects raw controls in branches and functions nothing renders", () => {
    expect(sites("neverRendered")).toEqual([
      "neverRendered.tsx › Gated › input",
      "neverRendered.tsx › Unused › Switch",
    ]);
  });

  it("follows imported components, aliases and re-exports", () => {
    expect(sites("importsHelper")).toEqual(["helper.tsx › HelperRow › Switch"]);
    expect(sites("aliases")).toEqual([
      "aliases.tsx › Aliased › Toggle",
      "helper.tsx › HelperRow › Switch",
    ]);
  });

  it("checks overlay content, including closed dialogs and components only a popover renders", () => {
    expect(sites("closedDialog")).toEqual([
      "closedDialog.tsx › ClosedDialog › Button",
      "closedDialog.tsx › ClosedDialog › Switch",
    ]);
    expect(sites("unlinkedDialog")).toEqual([
      "unlinkedDialog.tsx › UnlinkedDialog › DialogContent",
    ]);
    expect(sites("popoverOnly")).toEqual(["popoverBody.tsx › PopoverBody › Input"]);
  });

  it("follows aliases, memo(), helpers without JSX and callbacks into never-rendered branches", () => {
    expect(sites("indirection")).toEqual([
      "raw.tsx › AliasedRaw › input",
      "raw.tsx › CalledRaw › input",
      "raw.tsx › MemoizedRaw › input",
      "raw.tsx › renderRow › input",
    ]);
  });

  it("follows imported aliases, memo() and class components rendered as tags", () => {
    expect(sites("importedAlias")).toEqual(["Wrapped.tsx › Raw › input"]);
    expect(sites("importedMemo")).toEqual(["Wrapped.tsx › MemoTarget › input"]);
    expect(sites("importedClass")).toEqual(["Wrapped.tsx › ClassRaw › input"]);
  });

  it("walks an aliased or memo() component when it renders unowned", () => {
    // Its own switch and buttons aren't under a setting, so they're rejected through each wrapper.
    expect(sites("aliasedComponent").length).toBeGreaterThan(0);
    expect(sites("aliasedComponent").every((site) => site.startsWith("ThemeColorPicker.tsx"))).toBe(
      true,
    );
    expect(sites("memoComponent").length).toBeGreaterThan(0);
    expect(sites("memoComponent").every((site) => site.startsWith("ThemeColorPicker.tsx"))).toBe(
      true,
    );
  });

  it("walks a component table's components", () => {
    expect(sites("componentTable").length).toBeGreaterThan(0);
    expect(sites("componentTable").every((site) => site.startsWith("ThemeColorPicker.tsx"))).toBe(
      true,
    );
  });

  it("owns nothing through an entry search doesn't list", () => {
    expect(unlisted).toEqual([`${CATALOG_FILE}#FIXTURE_UNLISTED`]);
    expect(sites("unlistedEntry")).toEqual(["unlistedEntry.tsx › Unlisted › input"]);
  });

  it("rejects entries declared outside the catalog and undeclared parts", () => {
    expect(sites("entries")).toEqual([
      "entries.tsx › <module> › entry literal",
      "entries.tsx › <module> › settingsEntry",
      "entries.tsx › Fabricated › Switch",
      "entries.tsx › Fabricated › Switch",
      "entries.tsx › Fabricated › entry literal",
    ]);
    expect(sites("unknownPart")).toEqual(["unknownPart.tsx › UnknownPart › Button"]);
  });

  it("checks never-rendered branches of the admin pages", () => {
    const admin = check([
      path.join(WEB_ROOT, "src/pages/SharingPage.tsx"),
      path.join(WEB_ROOT, "src/pages/PoliciesPage.tsx"),
    ]).filter((violation) => violation.reason.includes("settingsControlProps"));
    expect(admin.map((violation) => violation.site)).toEqual(
      expect.arrayContaining([
        "src/pages/PoliciesPage.tsx › PoliciesPage › input",
        "src/pages/SharingPage.tsx › SharingPage › input",
      ]),
    );
  });

  describe("a shared component's controls", () => {
    const reasons = (name: string) =>
      check([fixtureFile(name)]).map(
        (violation) => `${violation.site.replace(/^.*\//, "")}: ${violation.reason}`,
      );

    it("are owned under a SettingsOwner at the Settings site, by its entry and part", () => {
      expect(reasons("sharedOwned")).toEqual([]);
      // A component that renders itself is walked once per owner, not once per level.
      expect(reasons("sharedRecursive")).toEqual([]);
      expect(reasons("templatedDirectNamed")).toEqual([]);
    });

    it("are unowned without one, and an owner names a declared part", () => {
      expect(reasons("sharedUnowned")).toEqual([
        "Shared.tsx › SharedCopy › button: A shared component's control is owned only under a SettingsOwner at its Settings site.",
      ]);
      expect(reasons("sharedUndeclaredPart")).toEqual([
        "Shared.tsx › SharedCopy › button: A shared component's control is owned only under a SettingsOwner at its Settings site.",
        'sharedUndeclaredPart.tsx › Row › SettingsOwner: "paste" is not a declared part of fixture.copy.',
      ]);
    });

    it("name a templated part only through its formatter, with the runtime value", () => {
      expect(reasons("sharedNoRuntime")).toEqual([
        "Shared.tsx › SharedNoRuntime › button: fixture.copy:copy is named by a template, so its props need the runtime value.",
      ]);
      expect(reasons("sharedOverride")).toEqual([
        "Shared.tsx › SharedOverride › button: fixture.copy:copy is named by its template; drop the separate aria-label.",
      ]);
      expect(reasons("templatedDirect")).toEqual([
        "templatedDirect.tsx › Row › button: fixture.copy:copy is named by a template, so its props need the runtime value.",
        "templatedDirect.tsx › Row › button: fixture.copy:copy is named by its template; drop the separate title.",
      ]);
    });

    it("can't be renamed by another spread beside the owner's", () => {
      const named = "fixture.copy:copy is named by its template";
      expect(reasons("templatedNamingSpreads")).toEqual([
        `templatedNamingSpreads.tsx › Row › button: ${named}; drop the separate aria-label.`,
        `templatedNamingSpreads.tsx › Row › button: ${named}; drop the separate aria-label.`,
        `templatedNamingSpreads.tsx › Row › button: ${named}; drop the separate title.`,
        `templatedNamingSpreads.tsx › Row › button: ${named}; spread only object literals beside it, so they can be checked.`,
      ]);
    });

    it("can't be renamed by a second owner spread", () => {
      const one =
        "templatedSecondOwnerSpread.tsx › Row › button: An element takes one owner spread: settingsControlProps, settingsOverlayProps or one ownership getter.";
      expect(reasons("templatedSecondOwnerSpread")).toEqual([one, one]);
    });

    it("take a runtime-choice marker only on an option of the overlay it names", () => {
      const at = "runtimeChoices.tsx › Row";
      const misplaced =
        "A runtime choice is an option of the overlay its marker names, a part declaring runtimeChoices, by a string literal.";
      expect(reasons("runtimeChoices")).toEqual([
        // A constant part, the main control's overlay, an overlay without runtime choices, and
        // a non-option. A datalist's marked suggestions are accepted.
        `${at} › SelectItem: ${misplaced}`,
        `${at} › SelectItem: ${misplaced}`,
        `${at} › SelectItem: ${misplaced}`,
        `${at} › button: ${misplaced}`,
      ]);
    });

    it("open an overlay from one declared part, never a union of them", () => {
      const at = "overlayUnionParts.tsx › Row";
      const misplaced =
        "A runtime choice is an option of the overlay its marker names, a part declaring runtimeChoices, by a string literal.";
      expect(reasons("overlayUnionParts")).toEqual([
        `${at} › SelectContent: Overlay content names the one part that opens it, not a choice of parts.`,
        `${at} › SelectContent: Overlay content names the one part that opens it, not a choice of parts.`,
        // So neither overlay lends its options a runtime-choice owner.
        `${at} › SelectItem: ${misplaced}`,
        `${at} › SelectItem: ${misplaced}`,
      ]);
    });

    it("walk only from their entry functions when given, through what those render", () => {
      const file = fixtureFile("entrySection");
      const at = (violations: GuardViolation[]) =>
        violations.map((violation) => violation.site.replace(/^.*\//, ""));
      expect(at(check([file]))).toEqual([
        "entrySection.tsx › OtherSection › button",
        "entrySection.tsx › Row › button",
      ]);
      expect(at(check([file], [{ file, name: "Section" }]))).toEqual([
        "entrySection.tsx › Row › button",
      ]);
      expect(() => check([file], [{ file, name: "Missing" }])).toThrow(
        "has no top-level function Missing",
      );
    });

    it("bind a hidden file input to a declared part of its row, with nothing to override it", () => {
      const bad =
        '{0}: settingsFileInputProps binds a hidden <input type="file">, with only its own written attributes, to a declared part of the row it sits in, named by a string literal.';
      // Only Row's first input is valid. Outside sits in no row. Then, by site: not an input;
      // and inputs not hidden, hidden only sometimes, of another type, with a constant part, an
      // undeclared part, their own tabIndex, an unknown spread, a spread that shows them, and a
      // class that could.
      expect(reasons("fileInputs")).toEqual([
        bad.replace("{0}", "fileInputs.tsx › Outside › input"),
        bad.replace("{0}", "fileInputs.tsx › Row › div"),
        ...Array.from({ length: 9 }, () => bad.replace("{0}", "fileInputs.tsx › Row › input")),
      ]);
    });

    it("take a radio group with owner props as its row's control, and its radios as choices", () => {
      expect(
        check([fixtureFile("radioGroups")], [{ file: fixtureFile("radioGroups"), name: "Owned" }]),
      ).toEqual([]);
      // Without an owner, or as a plain group, the radios are controls of their own; the shared
      // component without a SettingsOwner is unowned where it's rendered.
      expect(
        check(
          [fixtureFile("radioGroups")],
          [{ file: fixtureFile("radioGroups"), name: "Unowned" }],
        ).map((violation) => `${violation.site.replace(/^.*\//, "")}: ${violation.reason}`),
      ).toEqual([
        "IconSegmentedControl.tsx › IconSegmentedControl › button: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
        "IconSegmentedControl.tsx › IconSegmentedControl › div: A shared component's control is owned only under a SettingsOwner at its Settings site.",
        "radioGroups.tsx › Unowned › button: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
        "radioGroups.tsx › Unowned › button: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      ]);
    });

    it("take only a DOM radio group whose role nothing else can change", () => {
      // A component claiming the role is still walked, and a later spread may change the role.
      expect(
        check(
          [fixtureFile("radioGroups")],
          [{ file: fixtureFile("radioGroups"), name: "Claimed" }],
        ).map((violation) => `${violation.site.replace(/^.*\//, "")}: ${violation.reason}`),
      ).toEqual([
        "radioGroups.tsx › Claimed › FakeGroup: Control props go on a control, not on a component.",
        "radioGroups.tsx › Claimed › button: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
        "radioGroups.tsx › FakeGroup › input: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      ]);
    });

    it("check every part a finite union of literals may be", () => {
      const at = "unionParts.tsx › Row › button";
      expect(reasons("unionParts")).toEqual([
        // The union may be the templated copy part, so it needs its value and only its name.
        `${at}: fixture.copy:copy is named by a template, so its props need the runtime value.`,
        `${at}: fixture.copy:copy is named by its template; drop the separate aria-label.`,
        `${at}: "paste" is not a declared part of fixture.copy.`,
        `${at}: "<any>" is not a declared part of fixture.copy.`,
        `${at}: "<never>" is not a declared part of fixture.copy.`,
        `${at}: "<maybe>" is not a declared part of fixture.copy.`,
      ]);
    });

    it("need a runtime value that's actually given", () => {
      const missing =
        "templatedUndefinedRuntime.tsx › Row › button: fixture.copy:copy is named by a template, so its props need the runtime value.";
      expect(reasons("templatedUndefinedRuntime")).toEqual([missing, missing]);
    });

    it("take an owner part only from a string literal or constant", () => {
      expect(reasons("ownerPartConstants")).toEqual([
        "Shared.tsx › SharedCopy › button: A shared component's control is owned only under a SettingsOwner at its Settings site.",
        'ownerPartConstants.tsx › Row › SettingsOwner: "unknown" is not a declared part of fixture.copy.',
        "ownerPartConstants.tsx › Row › SettingsOwner: A SettingsOwner names its part as a string literal or a string constant.",
        "ownerPartConstants.tsx › Row › SettingsOwner: A SettingsOwner takes its entry and part as attributes, not a spread.",
      ]);
    });

    it("read the owner the hook's component is rendered under, as React does", () => {
      const above =
        "useSettingsOwnership() runs above this SettingsOwner, so it doesn't see it; call it in a component rendered inside.";
      expect(reasons("hookAboveOwner")).toEqual([`hookAboveOwner.tsx › Row › button: ${above}`]);
      // A getter captured from Row reads Row's owner, none, wherever its control renders.
      expect(reasons("hookCapturedGetter")).toEqual([
        "hookCapturedGetter.tsx › Row › button: A shared component's control is owned only under a SettingsOwner at its Settings site.",
      ]);
      // SharedNested's own control is the outer copy part, so it needs a runtime value; the
      // component it renders inside its owner takes the open part.
      expect(reasons("hookNestedOwners")).toEqual([
        "Shared.tsx › SharedNested › button: fixture.copy:copy is named by a template, so its props need the runtime value.",
      ]);
    });

    it("can't add a control of their own beside the owned one", () => {
      expect(reasons("sharedRawControl")).toEqual([
        "Shared.tsx › SharedRaw › input: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      ]);
    });
  });
});
