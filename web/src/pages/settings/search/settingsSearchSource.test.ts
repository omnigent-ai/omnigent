// Every control the Settings code can render belongs to a catalog entry, checked on the source
// so branches no test renders count too.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  catalogFromModules,
  createSettingsProgram,
  guardSettingsSource,
  type GuardViolation,
} from "@/test/settingsSourceGuard";

import { SETTINGS_ENTRIES } from "./catalog";
import { UNOWNED_CONTROLS } from "./settingsUnownedBaseline";

const WEB_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../..");

const CATALOG_MODULES = import.meta.glob<Record<string, unknown>>("./catalog/*.ts", {
  eager: true,
});

/** SettingsPage and every non-test module under pages/settings/. */
function settingsRootFiles(webRoot: string): string[] {
  const settingsDir = path.join(webRoot, "src/pages/settings");
  const files = fs
    .readdirSync(settingsDir, { recursive: true, encoding: "utf8" })
    .filter((file) => /\.tsx?$/.test(file) && !/\.(test|stories)\.tsx?$/.test(file))
    .map((file) => path.join(settingsDir, file));
  return [path.join(webRoot, "src/pages/SettingsPage.tsx"), ...files];
}

/** The catalog modules by absolute path, as the guard resolves declarations. */
function catalogModules(): Record<string, Record<string, unknown>> {
  return Object.fromEntries(
    Object.entries(CATALOG_MODULES).map(([file, module]) => [
      path.join(WEB_ROOT, "src/pages/settings/search", file),
      module,
    ]),
  );
}

/** The guard's view of the catalog: its entries, and its families' members. */
function guardCatalog() {
  const { catalog, families } = catalogFromModules(catalogModules(), SETTINGS_ENTRIES);
  return { catalog, families };
}

/** The guard's violations, walked from the given functions or from every root module. */
function violationsFrom(entryFunctions?: readonly { file: string; name: string }[]) {
  const rootFiles = settingsRootFiles(WEB_ROOT);
  const program = createSettingsProgram(WEB_ROOT, rootFiles);
  return guardSettingsSource({
    program,
    webRoot: WEB_ROOT,
    rootFiles,
    entryFunctions,
    ...guardCatalog(),
  });
}

/**
 * The violations the baseline doesn't allow, one line per site. `exact` also fails when the
 * baseline lists a control the code no longer has, so the baseline shrinks with the code.
 */
function unexpectedViolations(violations: readonly GuardViolation[], exact: boolean): string[] {
  const found = new Map<string, { count: number; reason: string }>();
  for (const violation of violations) {
    const entry = found.get(violation.site) ?? { count: 0, reason: violation.reason };
    entry.count += 1;
    found.set(violation.site, entry);
  }
  const problems: string[] = [];
  for (const [site, { count, reason }] of found) {
    const allowed = UNOWNED_CONTROLS[site]?.count ?? 0;
    if (count > allowed) problems.push(`${site} (${count}, baseline allows ${allowed}): ${reason}`);
  }
  if (exact) {
    for (const [site, { count }] of Object.entries(UNOWNED_CONTROLS)) {
      const actual = found.get(site)?.count ?? 0;
      if (actual !== count) {
        problems.push(`${site}: baseline lists ${count}, the guard finds ${actual}`);
      }
    }
  }
  return problems;
}

/** A top-level function of a Settings page module, as the page renders it. */
function pageFunction(file: string, name: string) {
  return { file: path.join(WEB_ROOT, "src/pages", file), name };
}

describe("Settings source guard", () => {
  it("lists every entry the catalog declares where search reads it", () => {
    expect(catalogFromModules(catalogModules(), SETTINGS_ENTRIES).unlisted).toEqual([]);
  });

  it("finds an owner for every Settings control, or a baseline reason for none", () => {
    expect(unexpectedViolations(violationsFrom(), true)).toEqual([]);
  }, 60_000);

  it("finds an owner for every control General renders", () => {
    expect(
      unexpectedViolations(
        violationsFrom([pageFunction("SettingsPage.tsx", "GeneralSection")]),
        false,
      ),
    ).toEqual([]);
  }, 60_000);

  it("finds an owner for every control Appearance renders", () => {
    expect(
      unexpectedViolations(
        violationsFrom([pageFunction("SettingsPage.tsx", "AppearanceSection")]),
        false,
      ),
    ).toEqual([]);
  }, 60_000);

  it("finds an owner for every control Git and Sandbox Integrations render", () => {
    expect(
      unexpectedViolations(
        violationsFrom([
          pageFunction("SettingsPage.tsx", "GitSection"),
          pageFunction("SettingsPage.tsx", "IntegrationsSection"),
        ]),
        false,
      ),
    ).toEqual([]);
  }, 60_000);

  it("finds an owner for every control MCP and Account render", () => {
    expect(
      unexpectedViolations(
        violationsFrom([
          pageFunction("SettingsPage.tsx", "AccountSection"),
          // MCP's section renders this component alone.
          {
            file: path.join(WEB_ROOT, "src/components/McpRegistry.tsx"),
            name: "McpRegistryConnections",
          },
        ]),
        false,
      ),
    ).toEqual([]);
  }, 60_000);

  it("finds an owner for every control Members renders, its rows' dialogs too", () => {
    expect(
      unexpectedViolations(violationsFrom([pageFunction("MembersPage.tsx", "MembersPage")]), false),
    ).toEqual([]);
  }, 60_000);

  it("finds an owner for every control Policies renders, its add dialog and rows' dialogs too", () => {
    expect(
      unexpectedViolations(
        violationsFrom([pageFunction("PoliciesPage.tsx", "PoliciesPage")]),
        false,
      ),
    ).toEqual([]);
  }, 60_000);

  it("finds an owner for every control Archived sessions renders, its bar and rows' dialogs too", () => {
    expect(
      unexpectedViolations(
        violationsFrom([pageFunction("SettingsPage.tsx", "ArchivedSection")]),
        false,
      ),
    ).toEqual([]);
  }, 60_000);

  it("finds an owner for every control Custom agents renders, its create form too", () => {
    expect(
      unexpectedViolations(
        violationsFrom([
          {
            file: path.join(WEB_ROOT, "src/pages/settings/SettingsCustomAgentsSection.tsx"),
            name: "SettingsCustomAgentsSection",
          },
        ]),
        false,
      ),
    ).toEqual([]);
  }, 60_000);

  it("finds an owner for every control the Harnesses grid renders", () => {
    // The grid's own components, walked as the page renders them; the detail page comes later.
    const section = path.join(WEB_ROOT, "src/pages/settings/SettingsHarnessesSection.tsx");
    expect(
      unexpectedViolations(
        violationsFrom(
          ["HostSelect", "HarnessCard", "SetupButton"].map((name) => ({ file: section, name })),
        ),
        false,
      ),
    ).toEqual([]);
  }, 60_000);
});
