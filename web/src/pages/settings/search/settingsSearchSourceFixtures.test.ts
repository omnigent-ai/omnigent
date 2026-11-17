// The guard's verdict on small modules, so a rule that stops firing fails here, not in the page walk.

import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  catalogFromModules,
  createSettingsProgram,
  guardSettingsSource,
} from "@/test/settingsSourceGuard";

import { CUSTOM_AGENT_CREATE_ENTRY } from "./catalog/customAgents";

const WEB_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../..");
const FIXTURE = path.join(WEB_ROOT, "src/pages/settings/FixtureSettings.tsx");
const ENTRY_FILE = path.join(WEB_ROOT, "src/pages/settings/search/catalog/customAgents.ts");

/** Runs the guard on one fixture module, with the create-agent entry as the only catalog entry. */
function guardFixture(source: string) {
  const program = createSettingsProgram(WEB_ROOT, [FIXTURE], new Map([[FIXTURE, source]]));
  const { catalog, families } = catalogFromModules(
    { [ENTRY_FILE]: { CUSTOM_AGENT_CREATE_ENTRY } },
    [CUSTOM_AGENT_CREATE_ENTRY],
  );
  return guardSettingsSource({
    program,
    webRoot: WEB_ROOT,
    rootFiles: [FIXTURE],
    catalog,
    families,
  });
}

describe("Settings source guard on fixtures", () => {
  it("rejects a button that no catalog entry owns", () => {
    const violations = guardFixture(`
      export function FixtureSettings() {
        return <button type="button">Save</button>;
      }
    `);
    expect(violations.map((violation) => violation.reason)).toEqual([
      "A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
    ]);
  });

  it("accepts a button that spreads its catalog entry's props", () => {
    const violations = guardFixture(`
      import { CUSTOM_AGENT_CREATE_ENTRY } from "@/pages/settings/search/catalog/customAgents";
      import { settingsControlProps } from "@/pages/settings/search/settingsControl";
      export function FixtureSettings() {
        return <button type="button" {...settingsControlProps(CUSTOM_AGENT_CREATE_ENTRY)}>Save</button>;
      }
    `);
    expect(violations).toEqual([]);
  });
});
