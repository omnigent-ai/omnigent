// The Settings source guard's verdict on fixture modules: what it accepts and, mostly, what it
// rejects, including controls in branches and components nothing renders. A rule that stops
// firing fails here, not only in the page walk.

import { describe, expect, it } from "vitest";

import { catalogFromModules } from "@/test/settingsSourceGuard";
import {
  FIXTURE_FAMILY,
  IMPORTS,
  createFixtureGuard,
  fixtureFile,
} from "@/test/settingsSourceGuardFixtures";

/** Ways a parameter can be written after a lookup read it, each in its own fixture function. */
const WRITE_SHAPES: Record<string, string> = {
  Direct: "member = other;",
  Shorthand: "({ member } = { member: other });",
  Renamed: "({ value: member } = { value: other });",
  Defaulted: "({ member = other } = {});",
  ArrayPattern: "[member] = [other];",
  Parenthesized: "(member) = other;",
  ForOf: "for (member of [other]) break;",
  ForOfPattern: "for ({ member } of [{ member: other }]) break;",
};

/** A row that saved one lookup, then a control that looks it up again after `write`. */
const writtenLookup = (
  name: string,
  write: string,
  parameters = "member: Member, other: Member",
) => `
export function ${name}(${parameters}) {
  const entry = FIXTURE_FAMILY.entry(member);
  ${write}
  return (
    <SettingsItem entry={entry}>
      <input type="file" hidden {...settingsFileInputProps(FIXTURE_FAMILY.entry(member), "open")} />
    </SettingsItem>
  );
}`;

const FIXTURES: Record<string, string> = {
  unownedButton: `${IMPORTS}
export function UnownedButton() {
  return <button type="button">Save</button>;
}
`,
  spreadsOwnEntry: `${IMPORTS}
export function OwnedButton() {
  return <button type="button" {...settingsControlProps(FIXTURE_SETTING)}>Save</button>;
}
`,
  overlaySite: `${IMPORTS}
import { FIX_CARD, FIX_CREDENTIAL, FIX_PAGE } from "@/pages/settings/search/catalog/guardFixture";
import { SettingsOverlayOwner } from "@/pages/settings/search/settingsOwnership";
import { FixDialog } from "@/components/guard-fixtures/FixDialog";
export function Site({ opener, member }: { opener: string; member: "alpha" | "beta" | "gamma" }) {
  return (
    <>
      {opener === "setUp" && (<SettingsOverlayOwner entry={FIX_CARD.entry(member)} opener="setUp"><FixDialog phase="install" /></SettingsOverlayOwner>)}
      {opener === "fixAction" && (<SettingsOverlayOwner entry={FIX_PAGE.entry(member)} opener="fixAction"><FixDialog phase="install" /></SettingsOverlayOwner>)}
      {opener === "credentialAction" && (<SettingsOverlayOwner entry={FIX_CREDENTIAL.entry(member)} opener="credentialAction"><FixDialog phase="install" /></SettingsOverlayOwner>)}
    </>
  );
}
`,
  overlaySiteReversed: `${IMPORTS}
import { FIX_CARD, FIX_CREDENTIAL, FIX_PAGE } from "@/pages/settings/search/catalog/guardFixture";
import { SettingsOverlayOwner } from "@/pages/settings/search/settingsOwnership";
import { FixDialog } from "@/components/guard-fixtures/FixDialog";
export function Site({ opener, member }: { opener: string; member: "alpha" | "beta" | "gamma" }) {
  return (
    <>
      {opener === "credentialAction" && (<SettingsOverlayOwner entry={FIX_CREDENTIAL.entry(member)} opener="credentialAction"><FixDialog phase="install" /></SettingsOverlayOwner>)}
      {opener === "fixAction" && (<SettingsOverlayOwner entry={FIX_PAGE.entry(member)} opener="fixAction"><FixDialog phase="install" /></SettingsOverlayOwner>)}
      {opener === "setUp" && (<SettingsOverlayOwner entry={FIX_CARD.entry(member)} opener="setUp"><FixDialog phase="install" /></SettingsOverlayOwner>)}
    </>
  );
}
`,
  overlayUnnarrowed: `${IMPORTS}
import { FIX_CARD, FIX_CREDENTIAL, FIX_PAGE } from "@/pages/settings/search/catalog/guardFixture";
import { SettingsOverlayOwner } from "@/pages/settings/search/settingsOwnership";
import { UnnarrowedInstall } from "@/components/guard-fixtures/FixDialog";
export function Site({ opener, member }: { opener: string; member: "alpha" | "beta" | "gamma" }) {
  return (
    <>
      {opener === "setUp" && (<SettingsOverlayOwner entry={FIX_CARD.entry(member)} opener="setUp"><UnnarrowedInstall phase="install" /></SettingsOverlayOwner>)}
    </>
  );
}
`,
  overlayFormUnnarrowed: `${IMPORTS}
import { FIX_CARD, FIX_CREDENTIAL, FIX_PAGE } from "@/pages/settings/search/catalog/guardFixture";
import { SettingsOverlayOwner } from "@/pages/settings/search/settingsOwnership";
import { UnnarrowedForm } from "@/components/guard-fixtures/FixDialog";
export function Site({ opener, member }: { opener: string; member: "alpha" | "beta" | "gamma" }) {
  return (
    <>
      {opener === "credentialAction" && (<SettingsOverlayOwner entry={FIX_CREDENTIAL.entry(member)} opener="credentialAction"><UnnarrowedForm /></SettingsOverlayOwner>)}
    </>
  );
}
`,
  overlayUngated: `${IMPORTS}
import { FIX_CARD, FIX_CREDENTIAL, FIX_PAGE } from "@/pages/settings/search/catalog/guardFixture";
import { SettingsOverlayOwner } from "@/pages/settings/search/settingsOwnership";
import { UngatedConfirm } from "@/components/guard-fixtures/FixDialog";
export function Site({ opener, member }: { opener: string; member: "alpha" | "beta" | "gamma" }) {
  return (
    <>
      {opener === "setUp" && (<SettingsOverlayOwner entry={FIX_CARD.entry(member)} opener="setUp"><UngatedConfirm phase="install" /></SettingsOverlayOwner>)}
      {opener === "credentialAction" && (<SettingsOverlayOwner entry={FIX_CREDENTIAL.entry(member)} opener="credentialAction"><UngatedConfirm phase="install" /></SettingsOverlayOwner>)}
    </>
  );
}
`,
  overlayUngatedProvider: `${IMPORTS}
import { FIX_CARD, FIX_CREDENTIAL, FIX_PAGE } from "@/pages/settings/search/catalog/guardFixture";
import { SettingsOverlayOwner } from "@/pages/settings/search/settingsOwnership";
import { UngatedProvider } from "@/components/guard-fixtures/FixDialog";
export function Site({ opener, member }: { opener: string; member: "alpha" | "beta" | "gamma" }) {
  return (
    <>
      {opener === "setUp" && (<SettingsOverlayOwner entry={FIX_CARD.entry(member)} opener="setUp"><UngatedProvider phase="install" /></SettingsOverlayOwner>)}
    </>
  );
}
`,
  overlayOtherGate: `${IMPORTS}
import { FIX_CARD, FIX_CREDENTIAL, FIX_PAGE } from "@/pages/settings/search/catalog/guardFixture";
import { SettingsOverlayOwner } from "@/pages/settings/search/settingsOwnership";
import { OtherGate } from "@/components/guard-fixtures/FixDialog";
export function Site({ opener, member }: { opener: string; member: "alpha" | "beta" | "gamma" }) {
  return (
    <>
      {opener === "setUp" && (<SettingsOverlayOwner entry={FIX_CARD.entry(member)} opener="setUp"><OtherGate phase="install" /></SettingsOverlayOwner>)}
    </>
  );
}
`,
  overlayOtherArm: `${IMPORTS}
import { FIX_CARD, FIX_CREDENTIAL, FIX_PAGE } from "@/pages/settings/search/catalog/guardFixture";
import { SettingsOverlayOwner } from "@/pages/settings/search/settingsOwnership";
import { OtherArm } from "@/components/guard-fixtures/FixDialog";
export function Site({ opener, member }: { opener: string; member: "alpha" | "beta" | "gamma" }) {
  return (
    <>
      {opener === "setUp" && (<SettingsOverlayOwner entry={FIX_CARD.entry(member)} opener="setUp"><OtherArm phase="install" /></SettingsOverlayOwner>)}
    </>
  );
}
`,
  overlayLoose: `${IMPORTS}
import { FIX_CARD, FIX_CREDENTIAL, FIX_PAGE } from "@/pages/settings/search/catalog/guardFixture";
import { SettingsOverlayOwner } from "@/pages/settings/search/settingsOwnership";
import { LooseMembers } from "@/components/guard-fixtures/FixDialog";
export function Site({ opener, member }: { opener: string; member: "alpha" | "beta" | "gamma" }) {
  return (
    <>
      {opener === "setUp" && (<SettingsOverlayOwner entry={FIX_CARD.entry(member)} opener="setUp"><LooseMembers phase="install" /></SettingsOverlayOwner>)}
    </>
  );
}
`,
  overlayOpenerInside: `${IMPORTS}
import { FIX_CARD } from "@/pages/settings/search/catalog/guardFixture";
import { SettingsOverlayOwner } from "@/pages/settings/search/settingsOwnership";
import { OpenerInside } from "@/components/guard-fixtures/FixDialog";
export function Site({ member }: { member: "alpha" | "beta" | "gamma" }) {
  return (
    <SettingsOverlayOwner entry={FIX_CARD.entry(member)} opener="setUp">
      <OpenerInside />
    </SettingsOverlayOwner>
  );
}
`,
  overlayMissingRuntime: `${IMPORTS}
import { FIX_CARD } from "@/pages/settings/search/catalog/guardFixture";
import { SettingsOverlayOwner } from "@/pages/settings/search/settingsOwnership";
import { MissingRuntime } from "@/components/guard-fixtures/FixDialog";
export function Site({ member }: { member: "alpha" | "beta" | "gamma" }) {
  return (
    <SettingsOverlayOwner entry={FIX_CARD.entry(member)} opener="setUp">
      <MissingRuntime />
    </SettingsOverlayOwner>
  );
}
`,
  overlayUnion: `${IMPORTS}
import { FIX_CARD } from "@/pages/settings/search/catalog/guardFixture";
import { SettingsOverlayOwner } from "@/pages/settings/search/settingsOwnership";
import { UnionParts } from "@/components/guard-fixtures/FixDialog";
export function Site({ member, part }: { member: "alpha" | "beta" | "gamma"; part: never }) {
  return (
    <SettingsOverlayOwner entry={FIX_CARD.entry(member)} opener="setUp">
      <UnionParts part={part} />
    </SettingsOverlayOwner>
  );
}
`,
  overlayUnionMissing: `${IMPORTS}
import { FIX_CARD } from "@/pages/settings/search/catalog/guardFixture";
import { SettingsOverlayOwner } from "@/pages/settings/search/settingsOwnership";
import { UnionMissing } from "@/components/guard-fixtures/FixDialog";
export function Site({ member, part }: { member: "alpha" | "beta" | "gamma"; part: never }) {
  return (
    <SettingsOverlayOwner entry={FIX_CARD.entry(member)} opener="setUp">
      <UnionMissing part={part} />
    </SettingsOverlayOwner>
  );
}
`,
  overlayUnionOutside: `${IMPORTS}
import { FIX_CARD } from "@/pages/settings/search/catalog/guardFixture";
import { SettingsOverlayOwner } from "@/pages/settings/search/settingsOwnership";
import { UnionOutsideOverlay } from "@/components/guard-fixtures/FixDialog";
export function Site({ member, part }: { member: "alpha" | "beta" | "gamma"; part: never }) {
  return (
    <SettingsOverlayOwner entry={FIX_CARD.entry(member)} opener="setUp">
      <UnionOutsideOverlay part={part} />
    </SettingsOverlayOwner>
  );
}
`,
  overlayUnionTemplated: `${IMPORTS}
import { FIX_CARD } from "@/pages/settings/search/catalog/guardFixture";
import { SettingsOverlayOwner } from "@/pages/settings/search/settingsOwnership";
import { UnionTemplated } from "@/components/guard-fixtures/FixDialog";
export function Site({ member, part }: { member: "alpha" | "beta" | "gamma"; part: never }) {
  return (
    <SettingsOverlayOwner entry={FIX_CARD.entry(member)} opener="setUp">
      <UnionTemplated part={part} />
    </SettingsOverlayOwner>
  );
}
`,
  overlayUnowned: `${IMPORTS}
import { FixDialog } from "@/components/guard-fixtures/FixDialog";
// Rendered with no overlay owner: its controls aren't owned.
export function Bare() {
  return <FixDialog phase="install" />;
}
`,
  overlayWrongOwner: `${IMPORTS}
import { FIX_CARD } from "@/pages/settings/search/catalog/guardFixture";
import { SettingsOverlayOwner } from "@/pages/settings/search/settingsOwnership";
import { FixDialog } from "@/components/guard-fixtures/FixDialog";
// The card opens its overlay from Set up, not from the page's fix.
export function WrongOpener({ member }: { member: "alpha" | "beta" | "gamma" }) {
  return (
    <SettingsOverlayOwner entry={FIX_CARD.entry(member)} opener="fixAction">
      <FixDialog phase="install" />
    </SettingsOverlayOwner>
  );
}
`,
  owned: `${IMPORTS}
export function Owned({ rows }: { rows: string[] }) {
  const reset = settingsControlProps(FIXTURE_SETTING, "reset");
  return (
    <SettingsItem entry={FIXTURE_SETTING}>
      <Switch {...settingsControlProps(FIXTURE_SETTING)} />
      <Button {...reset}>Reset sync</Button>
      <Select>
        <SelectTrigger {...settingsControlProps(FIXTURE_SETTING)} />
        <SelectContent {...settingsOverlayProps(FIXTURE_SETTING)}>
          <SelectItem value="fast">Fast</SelectItem>
        </SelectContent>
      </Select>
      <Dialog>
        <DialogContent {...settingsOverlayProps(FIXTURE_SETTING, "reset")}>
          <Button data-settings-dialog-action="Cancel">Cancel</Button>
        </DialogContent>
      </Dialog>
      <SettingsDataRegion entry={FIXTURE_SETTING} region="items">
        {rows.map((row) => (
          <div key={row}>
            <Button data-settings-row-action="delete">Delete</Button>
            <Input data-settings-row-field="token" aria-label={"Token for " + row} />
          </div>
        ))}
      </SettingsDataRegion>
    </SettingsItem>
  );
}
`,
  entryProp: `${IMPORTS}
import type { SettingsEntry } from "@/pages/settings/search/catalog/types";
function SwitchRow({ entry }: { entry: SettingsEntry }) {
  return <Switch {...settingsControlProps(entry)} />;
}
export function Rows() {
  return <SwitchRow entry={FIXTURE_SETTING} />;
}
`,
  extraControlInItem: `${IMPORTS}
export function ExtraControl() {
  return (
    <SettingsItem entry={FIXTURE_SETTING}>
      <Switch {...settingsControlProps(FIXTURE_SETTING)} />
      <Checkbox aria-label="Also sync drafts" />
    </SettingsItem>
  );
}
`,
  regionControls: `${IMPORTS}
export function Region({ rows }: { rows: string[] }) {
  return (
    <SettingsDataRegion entry={FIXTURE_SETTING} region="items">
      {rows.map((row) => (
        <div key={row}>
          <Button data-settings-row-action="archive">Archive</Button>
          <Select><SelectTrigger aria-label="Row mode" /></Select>
        </div>
      ))}
    </SettingsDataRegion>
  );
}
`,
  rowDialog: `${IMPORTS}
function ConfirmButtons() {
  return <Button data-settings-row-action="confirm">Delete</Button>;
}
export function Region({ rows, pending }: { rows: string[]; pending: string | null }) {
  return (
    <SettingsItem entry={FIXTURE_SETTING}>
      <SettingsDataRegion entry={FIXTURE_SETTING} region="items">
        {rows.map((row) => <Button key={row} data-settings-row-action="delete">Delete</Button>)}
      </SettingsDataRegion>
      <Dialog open={pending !== null}>
        <DialogContent {...settingsRowOverlayProps(FIXTURE_SETTING, "items", "delete")}>
          <Input data-settings-row-field="reason" aria-label={"Why delete " + pending} />
          <Button data-settings-row-action="cancel">Cancel</Button>
          <ConfirmButtons />
        </DialogContent>
      </Dialog>
    </SettingsItem>
  );
}
`,
  rowDialogMisuse: `${IMPORTS}
const REGION: string = "items";
export function Region({ open }: { open: boolean }) {
  return (
    <div>
      <Dialog open={open}>
        <DialogContent {...settingsRowOverlayProps(FIXTURE_SETTING, "items", "delete")}>
          <Button data-settings-row-action="delete">Delete</Button>
          <Input data-settings-row-field="token" aria-label="Token for x" />
          <Switch aria-label="Not the dialog's" />
        </DialogContent>
      </Dialog>
      <Dialog open={open}>
        <DialogContent {...settingsRowOverlayProps(FIXTURE_SETTING, "hosts", "signIn")}>
          <Button data-settings-row-action="cancel">Cancel</Button>
        </DialogContent>
      </Dialog>
      <Dialog open={open}>
        <DialogContent {...settingsRowOverlayProps(FIXTURE_SETTING, REGION, "delete")}>
          <Button data-settings-row-action="cancel">Cancel</Button>
        </DialogContent>
      </Dialog>
    </div>
  );
}
`,
  regionInDialog: `${IMPORTS}
export function Choices({ rows, open }: { rows: string[]; open: boolean }) {
  return (
    <Dialog open={open}>
      <DialogContent {...settingsOverlayProps(FIXTURE_SETTING, "reset")}>
        <SettingsDataRegion entry={FIXTURE_SETTING} region="items">
          {rows.map((row) => (
            <div key={row}>
              <select data-settings-row-field="token" aria-label={"Token for " + row}>
                {["a", "b"].map((value) => <option key={value}>{value}</option>)}
              </select>
              <Switch data-settings-row-action="delete" aria-label={"Delete " + row} />
            </div>
          ))}
        </SettingsDataRegion>
        <Button data-settings-dialog-action="Cancel">Cancel</Button>
      </DialogContent>
    </Dialog>
  );
}
`,
  buttonAsChild: `${IMPORTS}
import { Link } from "@/lib/routing";
declare const LINK_PROPS: Record<string, unknown>;
export function Links({ to }: { to?: string }) {
  return (
    <SettingsItem entry={FIXTURE_SETTING}>
      <Button asChild>
        <Link {...settingsControlProps(FIXTURE_SETTING, "reset")} to="/reset">Reset sync</Link>
      </Button>
      <Button asChild={true}>
        <Link to="/unowned">Unowned</Link>
      </Button>
      <Button asChild={to !== undefined} onClick={() => {}}>
        Maybe a button
      </Button>
      <Button asChild {...{ asChild: false }}>
        Spread to a button
      </Button>
      <Button asChild {...LINK_PROPS}>
        Spread of unknown props
      </Button>
      <Button {...{ asChild: false }} asChild>
        <Link {...settingsControlProps(FIXTURE_SETTING, "reset")} to="/reset">Reset sync</Link>
      </Button>
    </SettingsItem>
  );
}
`,
  rowFieldSelect: `${IMPORTS}
export function Region({ rows }: { rows: string[] }) {
  return (
    <SettingsItem entry={FIXTURE_SETTING}>
      <SettingsDataRegion entry={FIXTURE_SETTING} region="items">
        {rows.map((row) => (
          <div key={row}>
            <select data-settings-row-field="token" aria-label={"Token for " + row}>
              <option value="a">Keep</option>
              {["b", "c"].map((value) => <option key={value} value={value}>{value}</option>)}
            </select>
          </div>
        ))}
      </SettingsDataRegion>
    </SettingsItem>
  );
}
`,
  regionWithoutRows: `${IMPORTS}
export function Region() {
  return (
    <SettingsDataRegion entry={FIXTURE_SETTING} region="items">
      <Switch aria-label="Not a row" />
    </SettingsDataRegion>
  );
}
`,
  regionMisuse: `${IMPORTS}
export function Region({ rows, more }: { rows: string[]; more: string[] }) {
  return (
    <div>
      <SettingsDataRegion entry={FIXTURE_SETTING} region="items" rowActions={["delete"]}>
        {rows.map((row) => <Button key={row} data-settings-row-action="delete">Delete</Button>)}
      </SettingsDataRegion>
      <SettingsDataRegion entry={FIXTURE_SETTING} region="hosts">
        {rows.map((row) => <Button key={row} data-settings-row-action="signIn">Sign in…</Button>)}
        {more.map((row) => <Button key={row} data-settings-row-action="signIn">Sign in…</Button>)}
      </SettingsDataRegion>
      <SettingsDataRegion entry={FIXTURE_SETTING} region="unknown">
        {rows.map((row) => <Button key={row} data-settings-row-action="delete">Delete</Button>)}
      </SettingsDataRegion>
      <SettingsDataRegion entry={FIXTURE_SETTING} region="accounts">
        {rows.map((row) => <Input key={row} data-settings-row-field="other" aria-label="Other" />)}
      </SettingsDataRegion>
    </div>
  );
}
`,
  nestedRegions: `${IMPORTS}
export function Hosts({ hosts }: { hosts: { name: string; accounts: string[] }[] }) {
  return (
    <SettingsDataRegion entry={FIXTURE_SETTING} region="hosts">
      {hosts.map((host) => (
        <section key={host.name}>
          <Button data-settings-row-action="signIn">Sign in…</Button>
          <Button data-settings-row-action="signOut">Sign out</Button>
          <SettingsDataRegion entry={FIXTURE_SETTING} region="accounts">
            {host.accounts.map((account) => (
              <div key={account}>
                <Button data-settings-row-action="signOut">Sign out</Button>
                <Button data-settings-row-action="signIn">Sign in…</Button>
              </div>
            ))}
          </SettingsDataRegion>
        </section>
      ))}
    </SettingsDataRegion>
  );
}
`,
  familyOwned: `${IMPORTS}
import { FIXTURE_ALPHA, FIXTURE_FAMILY } from "@/pages/settings/search/catalog/guardFixture";
export function FamilyRow({ member }: { member: "alpha" | "beta" }) {
  const entry = FIXTURE_FAMILY.entry(member);
  return (
    <SettingsItem entry={entry}>
      <Button {...settingsControlProps(entry, "open")}>Open</Button>
      <Button {...settingsControlProps(FIXTURE_FAMILY.entry(member), "open")}>Open</Button>
      <SettingsDataRegion entry={entry} region="same">
        {["a", "b"].map((row) => <Button key={row} data-settings-row-action="remove">Remove</Button>)}
      </SettingsDataRegion>
    </SettingsItem>
  );
}
export function AlphaRow() {
  return (
    <SettingsItem entry={FIXTURE_ALPHA.entry("alpha")}>
      <Button {...settingsControlProps(FIXTURE_ALPHA.entry("alpha"), "extra")}>Extra</Button>
    </SettingsItem>
  );
}
`,
  familyMisuse: `${IMPORTS}
import {
  FAKE_FAMILY,
  FIXTURE_FAMILY,
  FORGED_FAMILY,
} from "@/pages/settings/search/catalog/guardFixture";
import { SettingsOwner } from "@/pages/settings/search/settingsOwnership";
function lookalike(member: string) {
  return member === "alpha" ? FIXTURE_SETTING : FIXTURE_SETTING;
}
export function Misused({ member }: { member: "alpha" | "beta" }) {
  let changing = FIXTURE_FAMILY.entry(member);
  changing = FIXTURE_FAMILY.entry("beta");
  return (
    <>
      <Button {...settingsControlProps(FIXTURE_FAMILY.entry(member), "extra")}>Extra</Button>
      <Button {...settingsControlProps(FAKE_FAMILY.entry(member))}>Fake</Button>
      <Button {...settingsControlProps(lookalike(member))}>Lookalike</Button>
      <Button {...settingsControlProps(changing, "open")}>Changing</Button>
      <Button {...settingsControlProps(FORGED_FAMILY.entry(member), "open")}>Forged</Button>
    </>
  );
}
export function MisusedOverlay({ member }: { member: "alpha" | "beta" }) {
  return (
    <Dialog>
      <DialogContent {...settingsOverlayProps(FIXTURE_FAMILY.entry(member), "extra")}>
        <p>Only alpha opens this.</p>
      </DialogContent>
    </Dialog>
  );
}
export function MisusedOwner({ member }: { member: "alpha" | "beta" }) {
  return (
    <SettingsOwner entry={FIXTURE_FAMILY.entry(member)} part="extra">
      <p>Only alpha has it.</p>
    </SettingsOwner>
  );
}
export function MisusedRegion({ member }: { member: "alpha" | "beta" }) {
  return (
    <SettingsItem entry={FIXTURE_FAMILY.entry(member)}>
      <SettingsDataRegion entry={FIXTURE_FAMILY.entry(member)} region="differs">
        {["a"].map((row) => <Button key={row} data-settings-row-action="remove">Remove</Button>)}
      </SettingsDataRegion>
    </SettingsItem>
  );
}
`,
  familyPlacement: `${IMPORTS}
import {
  FIXTURE_ALPHA,
  FIXTURE_FAMILY,
  FIXTURE_REVERSED,
} from "@/pages/settings/search/catalog/guardFixture";
import { settingsFileInputProps } from "@/pages/settings/search/settingsControl";
import { settingsRuntimeChoiceProps } from "@/pages/settings/search/settingsOwnership";
// Alpha's region reached through the family and through subsets in either order: one site.
export function SameRegionTwice() {
  return (
    <>
      <SettingsItem entry={FIXTURE_FAMILY.entry("alpha")}>
        <SettingsDataRegion entry={FIXTURE_FAMILY.entry("alpha")} region="same">
          {["a"].map((row) => <Button key={row} data-settings-row-action="remove">Remove</Button>)}
        </SettingsDataRegion>
      </SettingsItem>
      <SettingsItem entry={FIXTURE_ALPHA.entry("alpha")}>
        <SettingsDataRegion entry={FIXTURE_ALPHA.entry("alpha")} region="same">
          {["a"].map((row) => <Button key={row} data-settings-row-action="remove">Remove</Button>)}
        </SettingsDataRegion>
      </SettingsItem>
    </>
  );
}
export function ReorderedRegion({ member }: { member: "alpha" | "beta" }) {
  return (
    <SettingsItem entry={FIXTURE_REVERSED.entry(member)}>
      <SettingsDataRegion entry={FIXTURE_REVERSED.entry(member)} region="same">
        {["a"].map((row) => <Button key={row} data-settings-row-action="remove">Remove</Button>)}
      </SettingsDataRegion>
    </SettingsItem>
  );
}
// Different members found the same way, or the same member by different values.
export function WrongMembers({ member, other }: { member: "alpha" | "beta"; other: "alpha" | "beta" }) {
  return (
    <>
      <Select>
        <SelectContent {...settingsOverlayProps(FIXTURE_FAMILY.entry("alpha"), "open")}>
          <SelectItem value="one" {...settingsRuntimeChoiceProps(FIXTURE_FAMILY.entry("beta"), "open")}>One</SelectItem>
        </SelectContent>
      </Select>
      <SettingsItem entry={FIXTURE_FAMILY.entry("alpha")}>
        <input type="file" hidden {...settingsFileInputProps(FIXTURE_FAMILY.entry("beta"), "open")} />
      </SettingsItem>
      <SettingsItem entry={FIXTURE_FAMILY.entry(member)}>
        <input type="file" hidden {...settingsFileInputProps(FIXTURE_FAMILY.entry(other), "open")} />
      </SettingsItem>
      <SettingsItem entry={FIXTURE_FAMILY.entry("alpha")}>
        <SettingsDataRegion entry={FIXTURE_FAMILY.entry("alpha")} region="differs">
          {["a"].map((row) => (
            <SettingsItem key={row} entry={FIXTURE_FAMILY.entry("beta")}>
              <Button data-settings-row-action="remove">Remove</Button>
            </SettingsItem>
          ))}
        </SettingsDataRegion>
      </SettingsItem>
    </>
  );
}
`,
  familyIdentity: `${IMPORTS}
import {
  FIXTURE_FAMILY,
  FIXTURE_OTHER,
  FIXTURE_REVERSED,
  FIXTURE_SWAPPED,
} from "@/pages/settings/search/catalog/guardFixture";
import { settingsFileInputProps } from "@/pages/settings/search/settingsControl";
type Member = "alpha" | "beta";
// One value through another family, or through the family's entries remapped: another row.
export function OtherFamily({ member }: { member: Member }) {
  return (
    <>
      <SettingsItem entry={FIXTURE_FAMILY.entry(member)}>
        <input type="file" hidden {...settingsFileInputProps(FIXTURE_OTHER.entry(member), "open")} />
      </SettingsItem>
      <SettingsItem entry={FIXTURE_FAMILY.entry(member)}>
        <input type="file" hidden {...settingsFileInputProps(FIXTURE_SWAPPED.entry(member), "open")} />
      </SettingsItem>
    </>
  );
}
// A value that can change between reads: a reassigned parameter, a let binding, a property.
export function Reassigned(member: Member, other: Member) {
  const entry = FIXTURE_FAMILY.entry(member);
  member = other;
  return (
    <SettingsItem entry={entry}>
      <input type="file" hidden {...settingsFileInputProps(FIXTURE_FAMILY.entry(member), "open")} />
    </SettingsItem>
  );
}
export function LetBinding(props: { member: Member; other: Member }) {
  let { member } = props;
  const entry = FIXTURE_FAMILY.entry(member);
  member = props.other;
  return (
    <SettingsItem entry={entry}>
      <input type="file" hidden {...settingsFileInputProps(FIXTURE_FAMILY.entry(member), "open")} />
    </SettingsItem>
  );
}
export function PropertyRead(props: { member: Member; other: Member }) {
  const entry = FIXTURE_FAMILY.entry(props.member);
  return (
    <SettingsItem entry={entry}>
      <input type="file" hidden {...settingsFileInputProps(FIXTURE_FAMILY.entry(props.member), "open")} />
    </SettingsItem>
  );
}
// Valid: a snapshot read twice, even after its value is reassigned, and a reordered subset.
export function Snapshot(member: Member, other: Member) {
  const entry = FIXTURE_FAMILY.entry(member);
  member = other;
  return (
    <SettingsItem entry={entry}>
      <input type="file" hidden {...settingsFileInputProps(entry, "open")} />
    </SettingsItem>
  );
}
export function Reordered({ member }: { member: Member }) {
  return (
    <SettingsItem entry={FIXTURE_FAMILY.entry(member)}>
      <input type="file" hidden {...settingsFileInputProps(FIXTURE_REVERSED.entry(member), "open")} />
    </SettingsItem>
  );
}
`,
  familyWrites: `${IMPORTS}
import { FIXTURE_FAMILY } from "@/pages/settings/search/catalog/guardFixture";
import { settingsFileInputProps } from "@/pages/settings/search/settingsControl";
type Member = "alpha" | "beta";
${Object.entries(WRITE_SHAPES)
  .map(([name, write]) => writtenLookup(name, write))
  .join("\n")}
${writtenLookup("ForIn", "for (member in { alpha: 1 }) break;", "member: string, other: string")}
${writtenLookup(
  "BindingShorthand",
  "({ member } = { member: other });",
  "{ member }: { member: Member }, other: Member",
)}
${writtenLookup("Stable", "", "{ member }: { member: Member }")}
export function SavedSnapshot(member: Member, other: Member) {
  const entry = FIXTURE_FAMILY.entry(member);
  member = other;
  return (
    <SettingsItem entry={entry}>
      <input type="file" hidden {...settingsFileInputProps(entry, "open")} />
    </SettingsItem>
  );
}
`,
  familyPlacementValid: `${IMPORTS}
import { FIXTURE_ALPHA, FIXTURE_BETA, FIXTURE_FAMILY } from "@/pages/settings/search/catalog/guardFixture";
import { settingsFileInputProps } from "@/pages/settings/search/settingsControl";
import { settingsRuntimeChoiceProps } from "@/pages/settings/search/settingsOwnership";
// Disjoint subsets each render their own member's region; one value read twice is one entry.
export function DisjointRegions() {
  return (
    <>
      <SettingsItem entry={FIXTURE_ALPHA.entry("alpha")}>
        <SettingsDataRegion entry={FIXTURE_ALPHA.entry("alpha")} region="same">
          {["a"].map((row) => <Button key={row} data-settings-row-action="remove">Remove</Button>)}
        </SettingsDataRegion>
      </SettingsItem>
      <SettingsItem entry={FIXTURE_BETA.entry("beta")}>
        <SettingsDataRegion entry={FIXTURE_BETA.entry("beta")} region="same">
          {["a"].map((row) => <Button key={row} data-settings-row-action="remove">Remove</Button>)}
        </SettingsDataRegion>
      </SettingsItem>
    </>
  );
}
export function SameValue({ member }: { member: "alpha" | "beta" }) {
  const entry = FIXTURE_FAMILY.entry(member);
  return (
    <>
      <Select>
        <SelectContent {...settingsOverlayProps(entry, "open")}>
          <SelectItem value="one" {...settingsRuntimeChoiceProps(FIXTURE_FAMILY.entry(member), "open")}>One</SelectItem>
        </SelectContent>
      </Select>
      <SettingsItem entry={FIXTURE_FAMILY.entry(member)}>
        <input type="file" hidden {...settingsFileInputProps(entry, "open")} />
      </SettingsItem>
    </>
  );
}
`,
  tabLists: `${IMPORTS}
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
export function OwnedTabs() {
  return (
    <SettingsItem entry={FIXTURE_SETTING}>
      <Tabs defaultValue="a">
        <TabsList {...settingsControlProps(FIXTURE_SETTING, "reset")}>
          <TabsTrigger value="a">A</TabsTrigger>
          <TabsTrigger value="b">B</TabsTrigger>
        </TabsList>
        <TabsContent value="a">A view</TabsContent>
      </Tabs>
      <div role="tablist" {...settingsControlProps(FIXTURE_SETTING, "reset")}>
        <button role="tab">C</button>
      </div>
    </SettingsItem>
  );
}
`,
  tabListMisuse: `${IMPORTS}
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
export function MisusedTabs({ extra }: { extra: object }) {
  return (
    <SettingsItem entry={FIXTURE_SETTING}>
      <Tabs defaultValue="a">
        <TabsList role="group" {...settingsControlProps(FIXTURE_SETTING, "reset")}>
          <TabsTrigger value="a">Role</TabsTrigger>
        </TabsList>
        <TabsList {...settingsControlProps(FIXTURE_SETTING, "reset")} {...extra}>
          <TabsTrigger value="b">Spread</TabsTrigger>
        </TabsList>
        <TabsList>
          <TabsTrigger value="c">Unowned</TabsTrigger>
        </TabsList>
        <TabsList {...settingsControlProps(FIXTURE_SETTING, "missing" as never)}>
          <TabsTrigger value="d">Undeclared</TabsTrigger>
        </TabsList>
      </Tabs>
      <div role="radiogroup" {...settingsControlProps(FIXTURE_SETTING, "reset")}>
        <button role="tab">Not in a tab list</button>
      </div>
    </SettingsItem>
  );
}
`,
  ...Object.fromEntries(
    [
      ["tabMemoListFirst", ["tablist", "radiogroup"]],
      ["tabMemoGroupFirst", ["radiogroup", "tablist"]],
    ].map(([fixture, roles]) => [
      fixture,
      `${IMPORTS}
function SharedTab() {
  return <button role="tab">Shared</button>;
}
// One shared tab under an owned tab list and an owned radio group: the group isn't a tab list.
export function Uses() {
  return (
    <SettingsItem entry={FIXTURE_SETTING}>
      ${(roles as string[])
        .map(
          (role) =>
            `<div role="${role}" {...settingsControlProps(FIXTURE_SETTING, "reset")}><SharedTab /></div>`,
        )
        .join("\n      ")}
    </SettingsItem>
  );
}
`,
    ]),
  ),
  tabPlacement: `${IMPORTS}
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
export function Confined({ extra }: { extra: { role?: "button" } }) {
  return (
    <SettingsItem entry={FIXTURE_SETTING}>
      <Tabs defaultValue="a">
        {/* The nearest list is unowned, so its tab is too. */}
        <TabsList {...settingsControlProps(FIXTURE_SETTING, "reset")}>
          <div role="tablist"><button role="tab">Nested</button></div>
          {/* A spread could make it something other than a tab. */}
          <TabsTrigger value="b" {...extra}>Spread</TabsTrigger>
          <TabsTrigger value="c" asChild><a href="#c">As child</a></TabsTrigger>
        </TabsList>
        {/* asChild hands the list's props to a child whose role replaces tablist. */}
        <TabsList asChild {...settingsControlProps(FIXTURE_SETTING, "reset")}>
          <div role="group"><button role="tab">Regrouped</button></div>
        </TabsList>
      </Tabs>
      {/* A tab with owner props of its own is still a tab, outside any list. */}
      <button role="tab" {...settingsControlProps(FIXTURE_SETTING, "reset")}>Orphan</button>
    </SettingsItem>
  );
}
// A spread beside owner props doesn't stop something from being a tab: still refused.
export function SpreadOrphans({ role }: { role: "tab" | "button" }) {
  return (
    <SettingsItem entry={FIXTURE_SETTING}>
      <button {...{ className: "tab" }} role="tab" {...settingsControlProps(FIXTURE_SETTING, "reset")}>A</button>
      <Tabs defaultValue="b">
        <TabsTrigger value="b" {...{ className: "tab" }} {...settingsControlProps(FIXTURE_SETTING, "reset")}>B</TabsTrigger>
      </Tabs>
      <button role={role} {...settingsControlProps(FIXTURE_SETTING, "reset")}>C</button>
    </SettingsItem>
  );
}
`,
  dataNamedRows: `${IMPORTS}
function ServerRow({ name }: { name: string }) {
  return (
    <li data-settings-region-row>
      <Button data-settings-row-action="open">{name}</Button>
    </li>
  );
}
export function Servers({ rows }: { rows: string[] }) {
  return (
    <SettingsItem entry={FIXTURE_SETTING}>
      <SettingsDataRegion entry={FIXTURE_SETTING} region="servers">
        {rows.map((row) => <ServerRow key={row} name={row} />)}
      </SettingsDataRegion>
    </SettingsItem>
  );
}
`,
  // Each server's row holds its own tools region; a tool with a description opens to it.
  disclosureRows: `${IMPORTS}
type Tool = { name: string; description: string | null };
export function Disclosures({ servers }: { servers: { name: string; tools: Tool[] }[] }) {
  return (
    <SettingsItem entry={FIXTURE_SETTING}>
      <SettingsDataRegion entry={FIXTURE_SETTING} region="servers">
        {servers.map((server) => (
          <div key={server.name} data-settings-region-row>
            <Button data-settings-row-action="open">{server.name}</Button>
            <SettingsDataRegion entry={FIXTURE_SETTING} region="tools">
              <ul>
                {server.tools.map((tool) => (
                  <li key={tool.name} data-settings-region-row>
                    {tool.description ? (
                      <details>
                        <summary data-settings-row-action="expand">{tool.name}</summary>
                        <p>{tool.description}</p>
                      </details>
                    ) : (
                      <span>{tool.name}</span>
                    )}
                  </li>
                ))}
              </ul>
            </SettingsDataRegion>
          </div>
        ))}
      </SettingsDataRegion>
    </SettingsItem>
  );
}
`,
  // A disclosure with no summary of its own, and one whose summary no entry owns.
  disclosureMisuse: `${IMPORTS}
export function Misused({ open }: { open: boolean }) {
  return (
    <SettingsItem entry={FIXTURE_SETTING}>
      <details>
        <p>Additional information</p>
      </details>
      <details>
        {open ? <summary>Raw notes</summary> : <summary>Notes</summary>}
        <p>Kept here.</p>
      </details>
    </SettingsItem>
  );
}
`,
  dataNamedMisuse: `${IMPORTS}
function Row({ children }: { children: React.ReactNode }) {
  return <li>{children}</li>;
}
export function Misused({ rows }: { rows: string[] }) {
  return (
    <SettingsItem entry={FIXTURE_SETTING}>
      <div data-settings-region-row>Outside any template</div>
      <SettingsDataRegion entry={FIXTURE_SETTING} region="servers">
        {rows.map((row) => (
          <section key={row}>
            {/* Not inside a marked region row. */}
            <Button data-settings-row-action="open">{row}</Button>
            <div data-settings-region-row>
              <div data-settings-region-row>
                <Button data-settings-row-action="open">{row}</Button>
              </div>
            </div>
            <Row data-settings-region-row>{row}</Row>
          </section>
        ))}
      </SettingsDataRegion>
    </SettingsItem>
  );
}
`,
  regionScope: `${IMPORTS}
import { FIXTURE_COPY } from "@/pages/settings/search/catalog/guardFixture";
export function Region({ rows }: { rows: string[] }) {
  return (
    <SettingsItem entry={FIXTURE_SETTING}>
      <SettingsDataRegion entry={FIXTURE_SETTING} region="items">
        {rows.map((row) => (
          <section key={row}>
            <Button data-settings-row-action="delete">Delete</Button>
            <Input type="password" data-settings-row-field="token" aria-label={\`Token for \${row}\`} />
            {rows.map((inner) => <Button key={inner} data-settings-row-action="delete">Delete</Button>)}
            <SettingsItem entry={FIXTURE_COPY}>
              <Button data-settings-row-action="delete">Delete</Button>
            </SettingsItem>
            <Button data-settings-row-field="token">Token for New action</Button>
            <input type="button" data-settings-row-field="token" aria-label="Token for New action" />
            <a href="#token" data-settings-row-field="token">Token for New action</a>
          </section>
        ))}
      </SettingsDataRegion>
    </SettingsItem>
  );
}
`,
  regionTwice: `${IMPORTS}
export function Region({ rows }: { rows: string[] }) {
  return (
    <div>
      <SettingsDataRegion entry={FIXTURE_SETTING} region="hosts">
        {rows.map((row) => <Button key={row} data-settings-row-action="signIn">Sign in…</Button>)}
      </SettingsDataRegion>
      <SettingsDataRegion entry={FIXTURE_SETTING} region="hosts">
        {rows.map((row) => <Button key={row} data-settings-row-action="signIn">Sign in…</Button>)}
      </SettingsDataRegion>
    </div>
  );
}
`,
  ownerRow: `${IMPORTS}
import { useSettingsOwnership } from "@/pages/settings/search/settingsOwnership";
export function OwnerRow() {
  const owned = useSettingsOwnership();
  return <button {...owned.control({ outside: { "aria-label": "Open" } })} />;
}
`,
  ownerSection: `${IMPORTS}
import { FIXTURE_COPY } from "@/pages/settings/search/catalog/guardFixture";
import { SettingsOwner } from "@/pages/settings/search/settingsOwnership";
import { OwnerRow } from "@/pages/settings/guard-fixtures/ownerRow";
export function OwnerSection() {
  return (
    <SettingsOwner entry={FIXTURE_COPY} part="open">
      <OwnerRow />
    </SettingsOwner>
  );
}
`,
  ownerRowAlsoUnowned: `${IMPORTS}
import { OwnerRow } from "@/pages/settings/guard-fixtures/ownerRow";
export function UnownedSection() {
  return <OwnerRow />;
}
`,
  unnamedRow: `${IMPORTS}
export function UnnamedRow() {
  return <input aria-label="A control of its own" />;
}
`,
  unnamedSection: `${IMPORTS}
import { FIXTURE_COPY } from "@/pages/settings/search/catalog/guardFixture";
import { SettingsOwner } from "@/pages/settings/search/settingsOwnership";
import { UnnamedRow } from "@/pages/settings/guard-fixtures/unnamedRow";
export function UnnamedSection() {
  return (
    <SettingsOwner entry={FIXTURE_COPY} part="open">
      <UnnamedRow />
    </SettingsOwner>
  );
}
`,
};

const guard = createFixtureGuard(FIXTURES);

const check = guard.check;
/** The violations' sites, `file › function › element`, for one fixture module. */
const sites = (name: string) =>
  check([fixtureFile(name)]).map((violation) => violation.site.replace(/^.*\//, ""));
const siteReasons = (name: string) =>
  check([fixtureFile(name)]).map(
    (violation) => `${violation.site.replace(/^.*\//, "")}: ${violation.reason}`,
  );

describe("Settings source guard", () => {
  it("rejects a button that no catalog entry owns", () => {
    expect(siteReasons("unownedButton")).toEqual([
      "unownedButton.tsx › UnownedButton › button: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
    ]);
  });

  it("accepts a button that spreads its catalog entry's props", () => {
    expect(check([fixtureFile("spreadsOwnEntry")])).toEqual([]);
  });

  it("accepts owned controls, parts, options, dialog and row actions", () => {
    expect(check([fixtureFile("owned")])).toEqual([]);
    expect(check([fixtureFile("entryProp")])).toEqual([]);
  });

  it("rejects a separately labelled control inside an indexed row", () => {
    expect(sites("extraControlInItem")).toEqual([
      "extraControlInItem.tsx › ExtraControl › Checkbox",
    ]);
  });

  it("takes a data region's actions and fields only from its catalog declaration, in its one row template", () => {
    // The old props, a second map, an undeclared region, and an undeclared field.
    expect(siteReasons("regionMisuse")).toEqual([
      // The second map's Delete, and both maps of the two-map region; then the undeclared field.
      "regionMisuse.tsx › Region › Button: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      "regionMisuse.tsx › Region › Button: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      "regionMisuse.tsx › Region › Button: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      "regionMisuse.tsx › Region › Input: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      "regionMisuse.tsx › Region › SettingsDataRegion: A data region's row actions and fields are declared in its catalog entry, not passed here.",
      "regionMisuse.tsx › Region › SettingsDataRegion: A data region wraps exactly one row template: a .map(...) over its data.",
      "regionMisuse.tsx › Region › SettingsDataRegion: A data region names its entry and one of that entry's declared regions, as string literals.",
    ]);
  });

  it("accepts a region inside its entry's dialog: a field's options, and a switch as a row action", () => {
    expect(check([fixtureFile("regionInDialog")])).toEqual([]);
  });

  it("checks an always-asChild Button's child as the control, and a maybe-asChild Button itself", () => {
    // The owned links pass, including one whose asChild comes last after a spread. The unowned
    // link, and each Button that may render itself (a computed asChild, or a spread after it
    // that sets it false or may), are reported.
    expect(sites("buttonAsChild")).toEqual([
      "buttonAsChild.tsx › Links › Button",
      "buttonAsChild.tsx › Links › Button",
      "buttonAsChild.tsx › Links › Button",
      "buttonAsChild.tsx › Links › Link",
    ]);
  });

  it("accepts a row field's own options as its values", () => {
    expect(check([fixtureFile("rowFieldSelect")])).toEqual([]);
  });

  it("accepts a row's dialog whose controls are its declared actions and fields", () => {
    expect(check([fixtureFile("rowDialog")])).toEqual([]);
  });

  it("holds a row's dialog to its declaration: its own actions and fields, from a declared row action", () => {
    // The region's own Delete and token field, and an unmarked switch, aren't the dialog's;
    // Sign in declares no dialog; a region that isn't a literal can't be checked. The controls
    // of those two dialogs are owned by nothing either.
    expect(siteReasons("rowDialogMisuse")).toEqual([
      ...Array(3).fill(
        "rowDialogMisuse.tsx › Region › Button: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      ),
      ...Array(2).fill(
        "rowDialogMisuse.tsx › Region › DialogContent: A row's dialog names its entry, one of its regions and a row action that declares the dialog, as string literals.",
      ),
      "rowDialogMisuse.tsx › Region › Input: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      "rowDialogMisuse.tsx › Region › Switch: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
    ]);
  });

  it("keeps each nested region's actions to its own map", () => {
    // Sign out at host level, and Sign in inside the accounts template, are each reported.
    expect(siteReasons("nestedRegions")).toEqual([
      "nestedRegions.tsx › Hosts › Button: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      "nestedRegions.tsx › Hosts › Button: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
    ]);
  });

  it("exempts only the region's own row: not a map inside it, another entry's row, or a field that isn't one", () => {
    // The real token input passes; the inner map's Delete, the other entry's Delete, and the
    // button, input button and link marked as the token field are each reported.
    expect(siteReasons("regionScope")).toEqual([
      "regionScope.tsx › Region › Button: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      "regionScope.tsx › Region › Button: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      "regionScope.tsx › Region › Button: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      "regionScope.tsx › Region › a: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      "regionScope.tsx › Region › input: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
    ]);
  });

  it("checks a family lookup against every member, through a subset or a const alias", () => {
    expect(check([fixtureFile("familyOwned")])).toEqual([]);
  });

  it("rejects a part one member lacks, and lookups settingsEntryFamily didn't make", () => {
    // The extra part fails beta as a control, an overlay's opener and an owner's part; the
    // members name the region's action differently; the look-alike object, the local function,
    // a reassigned variable and a copied family with its own lookup aren't registered lookups.
    expect(siteReasons("familyMisuse")).toEqual([
      'familyMisuse.tsx › Misused › Button: "extra" is not a declared part of fixture.family-beta.',
      "familyMisuse.tsx › Misused › Button: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      "familyMisuse.tsx › Misused › Button: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      "familyMisuse.tsx › Misused › Button: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      "familyMisuse.tsx › Misused › Button: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      'familyMisuse.tsx › MisusedOverlay › DialogContent: "extra" is not a declared part of fixture.family-beta.',
      'familyMisuse.tsx › MisusedOwner › SettingsOwner: "extra" is not a declared part of fixture.family-beta.',
      "familyMisuse.tsx › MisusedRegion › Button: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      "familyMisuse.tsx › MisusedRegion › SettingsDataRegion: A data region names its entry and one of that entry's declared regions, as string literals.",
    ]);
  });

  it("holds each concrete entry's region to one site, through any lookup or subset order", () => {
    expect(siteReasons("familyPlacement").filter((reason) => reason.includes("already"))).toEqual([
      "familyPlacement.tsx › ReorderedRegion › SettingsDataRegion: fixture.family-alpha:same is already rendered by another data region; each region has one site.",
      "familyPlacement.tsx › SameRegionTwice › SettingsDataRegion: fixture.family-alpha:same is already rendered by another data region; each region has one site.",
    ]);
    expect(check([fixtureFile("familyPlacementValid")])).toEqual([]);
  });

  it("places a control in its row or overlay only when it's provably the same member", () => {
    // Beta's runtime choice in alpha's overlay, beta's file input in alpha's row, one found by
    // another value, and beta's row inside alpha's region, each lose their allowance.
    expect(
      siteReasons("familyPlacement").filter((reason) => reason.includes("WrongMembers")),
    ).toEqual([
      "familyPlacement.tsx › WrongMembers › Button: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      "familyPlacement.tsx › WrongMembers › SelectItem: A runtime choice is an option of the overlay its marker names, a part declaring runtimeChoices, by a string literal.",
      'familyPlacement.tsx › WrongMembers › input: settingsFileInputProps binds a hidden <input type="file">, with only its own written attributes, to a declared part of the row it sits in, named by a string literal.',
      'familyPlacement.tsx › WrongMembers › input: settingsFileInputProps binds a hidden <input type="file">, with only its own written attributes, to a declared part of the row it sits in, named by a string literal.',
    ]);
  });

  it("proves one row only through the same family mapping and an unchanging value", () => {
    const fileInput =
      'settingsFileInputProps binds a hidden <input type="file">, with only its own written attributes, to a declared part of the row it sits in, named by a string literal.';
    expect(siteReasons("familyIdentity")).toEqual([
      `familyIdentity.tsx › LetBinding › input: ${fileInput}`,
      `familyIdentity.tsx › OtherFamily › input: ${fileInput}`,
      `familyIdentity.tsx › OtherFamily › input: ${fileInput}`,
      `familyIdentity.tsx › PropertyRead › input: ${fileInput}`,
      `familyIdentity.tsx › Reassigned › input: ${fileInput}`,
    ]);
  });

  it("treats a value as unchanging only when no assignment shape writes it", () => {
    const written = siteReasons("familyWrites").map((reason) => reason.split(" › ")[1]);
    expect(written).toEqual([...Object.keys(WRITE_SHAPES), "ForIn", "BindingShorthand"].sort());
  });

  it("reports a family member search doesn't list", () => {
    expect(
      catalogFromModules({ "catalog.ts": { FIXTURE_FAMILY } }, [FIXTURE_FAMILY.entries[0]!])
        .unlisted,
    ).toEqual(["catalog.ts#FIXTURE_FAMILY(beta)"]);
  });

  it("owns a tab list as its part's control, its tabs as that part's choices", () => {
    expect(check([fixtureFile("tabLists")])).toEqual([]);
  });

  it("owns only ui/'s TabsList or a written-out tablist, with no other spread or role", () => {
    const control =
      "A Settings control needs settingsControlProps(entry, part) from a catalog entry.";
    const tabList =
      'An owned tab list is ui/\'s TabsList or a role="tablist" element, with no other spread, no role of its own and no asChild.';
    // A list with a role of its own or another spread owns nothing, so its tabs are unowned,
    // as are an unowned list's; an undeclared part fails; a tab outside a tab list is unowned.
    expect(siteReasons("tabListMisuse")).toEqual([
      `tabListMisuse.tsx › MisusedTabs › TabsList: ${tabList}`,
      `tabListMisuse.tsx › MisusedTabs › TabsList: ${tabList}`,
      'tabListMisuse.tsx › MisusedTabs › TabsList: "missing" is not a declared part of fixture.sync.',
      `tabListMisuse.tsx › MisusedTabs › TabsTrigger: ${control}`,
      `tabListMisuse.tsx › MisusedTabs › TabsTrigger: ${control}`,
      `tabListMisuse.tsx › MisusedTabs › TabsTrigger: ${control}`,
      `tabListMisuse.tsx › MisusedTabs › button: ${control}`,
    ]);
  });

  it("allows a tab only in its nearest owned tab list, however it's reached or marked", () => {
    // The regrouped list; spread and asChild triggers; the nested list's and regrouped list's tabs.
    expect(siteReasons("tabPlacement")).toEqual([
      'tabPlacement.tsx › Confined › TabsList: An owned tab list is ui/\'s TabsList or a role="tablist" element, with no other spread, no role of its own and no asChild.',
      "tabPlacement.tsx › Confined › TabsTrigger: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      "tabPlacement.tsx › Confined › TabsTrigger: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      "tabPlacement.tsx › Confined › a: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      "tabPlacement.tsx › Confined › button: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      "tabPlacement.tsx › Confined › button: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
      "tabPlacement.tsx › Confined › button: A tab is a choice of its owned tab list; owner props go on the list.",
      "tabPlacement.tsx › SpreadOrphans › TabsTrigger: A tab is a choice of its owned tab list; owner props go on the list.",
      "tabPlacement.tsx › SpreadOrphans › button: A tab is a choice of its owned tab list; owner props go on the list.",
      "tabPlacement.tsx › SpreadOrphans › button: A tab is a choice of its owned tab list; owner props go on the list.",
    ]);
    // A shared tab walked under an owned list and an owned radio group, in either order.
    for (const fixture of ["tabMemoListFirst", "tabMemoGroupFirst"]) {
      expect(siteReasons(fixture)).toEqual([
        `${fixture}.tsx › SharedTab › button: A Settings control needs settingsControlProps(entry, part) from a catalog entry.`,
      ]);
    }
  });

  it("allows a data-named row action inside a marked region row, through a row component", () => {
    expect(check([fixtureFile("dataNamedRows")])).toEqual([]);
  });

  it("allows a disclosure's summary as a data-named action in a nested region's row", () => {
    expect(check([fixtureFile("disclosureRows")])).toEqual([]);
  });

  it("rejects a disclosure with no summary of its own, and a summary no entry owns", () => {
    const control =
      "A Settings control needs settingsControlProps(entry, part) from a catalog entry.";
    expect(siteReasons("disclosureMisuse")).toEqual([
      "disclosureMisuse.tsx › Misused › details: A disclosure needs its own <summary>, which is its control.",
      `disclosureMisuse.tsx › Misused › summary: ${control}`,
      `disclosureMisuse.tsx › Misused › summary: ${control}`,
    ]);
  });

  it("rejects a data-named action outside a region row, and a misplaced region row", () => {
    const control =
      "A Settings control needs settingsControlProps(entry, part) from a catalog entry.";
    const regionRow =
      "A region row is a DOM element in its data region's row template, not inside another row.";
    expect(siteReasons("dataNamedMisuse")).toEqual([
      `dataNamedMisuse.tsx › Misused › Button: ${control}`,
      `dataNamedMisuse.tsx › Misused › Row: ${regionRow}`,
      `dataNamedMisuse.tsx › Misused › div: ${regionRow}`,
      `dataNamedMisuse.tsx › Misused › div: ${regionRow}`,
    ]);
  });

  it("renders each data region from one site", () => {
    expect(siteReasons("regionTwice")).toEqual([
      "regionTwice.tsx › Region › SettingsDataRegion: fixture.sync:hosts is already rendered by another data region; each region has one site.",
    ]);
  });

  it("rejects controls in a data region that aren't declared row actions", () => {
    expect(sites("regionControls")).toEqual([
      "regionControls.tsx › Region › Button",
      "regionControls.tsx › Region › SelectTrigger",
    ]);
    expect(sites("regionWithoutRows")).toEqual([
      "regionWithoutRows.tsx › Region › SettingsDataRegion",
      "regionWithoutRows.tsx › Region › Switch",
    ]);
  });
});

describe("a shared dialog's overlay owner", () => {
  it("names each part where every member the narrowing keeps declares it, in either walk order", () => {
    expect(siteReasons("overlaySite")).toEqual([]);
    expect(siteReasons("overlaySiteReversed")).toEqual([]);
  });

  it("refuses a part offered to members that lack it, and ungated use of an empty narrowing", () => {
    expect(siteReasons("overlayUnnarrowed")).toEqual([
      'FixDialog.tsx › UnnarrowedInstall › Button: "installAction" is not a declared part of fixture.fix-setUp-gamma.',
    ]);
    // The credential form re-lent without its narrowing reaches a member with no form.
    expect(siteReasons("overlayFormUnnarrowed")).toEqual([
      'FixDialog.tsx › KeyForm › Input: "apiKey" is not a declared part of fixture.fix-credentialAction-beta.',
    ]);
    // Under the card's owner the confirmation's narrowing is empty; under the credential's it isn't.
    expect(siteReasons("overlayUngated")).toEqual([
      "FixDialog.tsx › UngatedConfirm › Button: This narrowing leaves the overlay no owner here; render its parts only behind that getter.",
    ]);
    // A refused Provider lends nothing, so what it wraps is unowned as well.
    expect(siteReasons("overlayUngatedProvider")).toEqual([
      "FixDialog.tsx › SwitchConfirm › Button: A shared dialog's control is owned only under a SettingsOverlayOwner at its Settings site.",
      "FixDialog.tsx › UngatedProvider › confirm.Provider: This narrowing leaves the overlay no owner here; render its parts only behind that getter.",
    ]);
  });

  it("checks what another getter gates, a ternary's other arm, and narrowings it can't prove", () => {
    expect(siteReasons("overlayOtherGate")).toEqual([
      "FixDialog.tsx › OtherGate › Button: This narrowing leaves the overlay no owner here; render its parts only behind that getter.",
    ]);
    // Under the card's owner the confirmation is pruned, and only the other arm is checked.
    expect(sites("overlayOtherArm")).toEqual(["FixDialog.tsx › OtherArm › Button"]);
    // A templated part takes its runtime value through the getter's options, as the helper's.
    expect(siteReasons("overlayMissingRuntime")).toEqual([
      "FixDialog.tsx › MissingRuntime › Button: fixture.fix-setUp-alpha:copyLink is named by a template, so its props need the runtime value.",
    ]);
    // A part passed as a finite union is each of its candidates, on each member.
    expect(siteReasons("overlayUnion")).toEqual([]);
    expect(siteReasons("overlayUnionMissing")).toEqual([
      'FixDialog.tsx › UnionMissing › Button: "copyCode" is not a declared part of fixture.fix-setUp-alpha.',
    ]);
    expect(siteReasons("overlayUnionOutside")).toEqual([
      'FixDialog.tsx › UnionOutsideOverlay › Button: fixture.fix-setUp-alpha:setUp isn\'t a part of its "setUp" overlay.',
    ]);
    expect(siteReasons("overlayUnionTemplated")).toEqual([
      "FixDialog.tsx › UnionTemplated › Button: fixture.fix-setUp-alpha:copyLink is named by a template, so its props need the runtime value.",
    ]);
    expect(siteReasons("overlayOpenerInside")).toEqual([
      'FixDialog.tsx › OpenerInside › Button: fixture.fix-setUp-alpha:setUp isn\'t a part of its "setUp" overlay.',
    ]);
    expect(siteReasons("overlayLoose")).toEqual([
      "FixDialog.tsx › LooseMembers › Button: within() takes a literal member list and, if any, a literal opener.",
    ]);
  });

  it("refuses the dialog rendered with no owner, or under an opener its entry doesn't open", () => {
    // Every control of the dialog, and its content, is unowned there.
    const unowned =
      "A shared dialog's control is owned only under a SettingsOverlayOwner at its Settings site.";
    const dialogControls = [
      `FixDialog.tsx › FixDialog › Button: ${unowned}`,
      `FixDialog.tsx › FixDialog › Button: ${unowned}`,
      `FixDialog.tsx › FixDialog › Button: ${unowned}`,
      `FixDialog.tsx › FixDialog › Button: ${unowned}`,
      `FixDialog.tsx › FixDialog › DialogContent: ${unowned}`,
      `FixDialog.tsx › KeyForm › Input: ${unowned}`,
      `FixDialog.tsx › SwitchConfirm › Button: ${unowned}`,
    ];
    expect(siteReasons("overlayUnowned")).toEqual(dialogControls);
    // A refused owner lends nothing, so the dialog under it is unowned as well.
    expect(siteReasons("overlayWrongOwner")).toEqual([
      ...dialogControls,
      'overlayWrongOwner.tsx › WrongOpener › SettingsOverlayOwner: fixture.fix-setUp-alpha declares no overlay for "fixAction".',
    ]);
  });
});

describe("Settings source guard's whole-tree pass on a component rendered inside owners", () => {
  const reasons = (names: string[]) =>
    check(names.map((name) => fixtureFile(name))).map(
      (violation) => `${violation.site.replace(/^.*\//, "")}: ${violation.reason}`,
    );
  const ownerRowReason =
    "ownerRow.tsx › OwnerRow › button: A shared component's control is owned only under a SettingsOwner at its Settings site.";

  it("checks a component through its owner, not on its own, when no walk renders it unowned", () => {
    expect(reasons(["ownerSection", "ownerRow"])).toEqual([]);
  });

  it("still checks a component on its own when no walk renders it", () => {
    expect(reasons(["ownerRow"])).toEqual([ownerRowReason]);
  });

  it("still checks a component on its own when another walk renders it unowned", () => {
    expect(reasons(["ownerSection", "ownerRow", "ownerRowAlsoUnowned"])).toEqual([ownerRowReason]);
  });

  it("still fails a control its owner doesn't name, in a component rendered inside it", () => {
    expect(reasons(["unnamedSection", "unnamedRow"])).toEqual([
      "unnamedRow.tsx › UnnamedRow › input: A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
    ]);
  });
});
