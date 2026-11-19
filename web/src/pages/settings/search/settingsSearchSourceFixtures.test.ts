// The guard's verdict on small modules, so a rule that stops firing fails here, not in the page walk.

import path from "node:path";
import { fileURLToPath } from "node:url";

import { beforeAll, describe, expect, it } from "vitest";

import {
  catalogFromModules,
  createSettingsProgram,
  guardSettingsSource,
} from "@/test/settingsSourceGuard";

import { CUSTOM_AGENT_CREATE_ENTRY } from "./catalog/customAgents";

const WEB_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../..");
const ENTRY_FILE = path.join(WEB_ROOT, "src/pages/settings/search/catalog/customAgents.ts");
const REJECTS_FIXTURE = path.join(WEB_ROOT, "src/pages/settings/FixtureRejects.tsx");
const ACCEPTS_FIXTURE = path.join(WEB_ROOT, "src/pages/settings/FixtureAccepts.tsx");

const REJECTS_SOURCE = `
  export function FixtureSettings() {
    return <button type="button">Save</button>;
  }
`;
const ACCEPTS_SOURCE = `
  import { CUSTOM_AGENT_CREATE_ENTRY } from "@/pages/settings/search/catalog/customAgents";
  import { settingsControlProps } from "@/pages/settings/search/settingsControl";
  export function FixtureSettings() {
    return <button type="button" {...settingsControlProps(CUSTOM_AGENT_CREATE_ENTRY)}>Save</button>;
  }
`;

/** One program holds both fixtures, so the TypeScript program is built once for the block. */
let program: ReturnType<typeof createSettingsProgram>;
beforeAll(() => {
  program = createSettingsProgram(
    WEB_ROOT,
    [REJECTS_FIXTURE, ACCEPTS_FIXTURE],
    new Map([
      [REJECTS_FIXTURE, REJECTS_SOURCE],
      [ACCEPTS_FIXTURE, ACCEPTS_SOURCE],
    ]),
  );
});

/** Runs the guard on one fixture module, with the create-agent entry as the only catalog entry. */
function guardFixture(fixture: string) {
  const { catalog, families } = catalogFromModules(
    { [ENTRY_FILE]: { CUSTOM_AGENT_CREATE_ENTRY } },
    [CUSTOM_AGENT_CREATE_ENTRY],
  );
  return guardSettingsSource({
    program,
    webRoot: WEB_ROOT,
    rootFiles: [fixture],
    catalog,
    families,
  });
}

describe("Settings source guard on fixtures", () => {
  it("rejects a button that no catalog entry owns", () => {
    const violations = guardFixture(REJECTS_FIXTURE);
    expect(violations.map((violation) => violation.reason)).toEqual([
      "A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
    ]);
  });

  it("accepts a button that spreads its catalog entry's props", () => {
    expect(guardFixture(ACCEPTS_FIXTURE)).toEqual([]);
  });
});
