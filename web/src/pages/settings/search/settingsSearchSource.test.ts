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

const WEB_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../..");

const CATALOG_MODULES = import.meta.glob<Record<string, unknown>>("./catalog/*.ts", {
  eager: true,
});

/**
 * Controls the guard accepts as unowned, by site: how many there are and why. The whole-tree
 * run must find exactly these; a section's run may find no more than the baseline allows.
 */
const UNOWNED_CONTROLS: Readonly<Record<string, { count: number; reason: string }>> = {
  "src/components/McpRegistry.tsx › ServiceConnection › Button": {
    count: 2,
    reason: "Managed MCP service connect button, not yet catalog-owned.",
  },
  "src/components/ModelValueCombobox.tsx › ModelValueCombobox › button": {
    count: 1,
    reason: 'Model field\'s combobox; its accessible name includes "*".',
  },
  "src/components/ModelValueCombobox.tsx › ModelValueCombobox › input": {
    count: 1,
    reason: 'Model field\'s combobox; its accessible name includes "*".',
  },
  "src/components/theme/ThemeColorPicker.tsx › ThemeColorPicker › button": {
    count: 1,
    reason: "Theme color picker popover internals, not named by a row.",
  },
  "src/components/theme/ThemeColorPicker.tsx › ThemeColorPicker › div": {
    count: 1,
    reason: "Theme color picker popover internals, not named by a row.",
  },
  "src/components/theme/ThemeColorPicker.tsx › ThemeColorPicker › input": {
    count: 2,
    reason: "Theme color picker popover internals, not named by a row.",
  },
  "src/pages/AdminGeneralPage.tsx › AdminGeneralPage › SelectContent": {
    count: 1,
    reason: "Admin general page's select, not yet catalog-owned.",
  },
  "src/pages/AdminGeneralPage.tsx › AdminGeneralPage › SelectItem": {
    count: 1,
    reason: "Admin general page's select, not yet catalog-owned.",
  },
  "src/pages/AdminGeneralPage.tsx › AdminGeneralPage › SelectTrigger": {
    count: 1,
    reason: "Admin general page's select, not yet catalog-owned.",
  },
  "src/pages/MembersPage.tsx › CopyableValue › Button": {
    count: 1,
    reason: "Members copy control and invite-result Done button, not yet catalog-owned.",
  },
  "src/pages/MembersPage.tsx › CopyableValue › Input": {
    count: 1,
    reason: "Members copy control and invite-result Done button, not yet catalog-owned.",
  },
  "src/pages/MembersPage.tsx › MembersPage › Button": {
    count: 2,
    reason: "Members copy control and invite-result Done button, not yet catalog-owned.",
  },
  "src/pages/PoliciesPage.tsx › AddDefaultPolicyDialog › Button": {
    count: 1,
    reason: "Add default policy dialog's actions, not yet catalog-owned.",
  },
  "src/pages/PoliciesPage.tsx › AddDefaultPolicyDialog › button": {
    count: 1,
    reason: "Add default policy dialog's actions, not yet catalog-owned.",
  },
  "src/pages/SettingsPage.tsx › AppearanceSection › Button": {
    count: 2,
    reason: "Reset dialog's Cancel button, a dialog action not yet declared.",
  },
  "src/pages/SettingsPage.tsx › AppearanceSection › input": {
    count: 1,
    reason: "Hidden file input bound to a row part, not yet declared.",
  },
  "src/pages/SettingsPage.tsx › StepperButton › button": {
    count: 1,
    reason: "Number stepper's button, not yet catalog-owned.",
  },
  "src/pages/SharingPage.tsx › BlockedBy › div": {
    count: 1,
    reason: "Sharing page's select, input or note, not yet catalog-owned.",
  },
  "src/pages/SharingPage.tsx › SharingPage › SelectContent": {
    count: 2,
    reason: "Sharing page's select, input or note, not yet catalog-owned.",
  },
  "src/pages/SharingPage.tsx › SharingPage › SelectItem": {
    count: 3,
    reason: "Sharing page's select, input or note, not yet catalog-owned.",
  },
  "src/pages/SharingPage.tsx › SharingPage › input": {
    count: 1,
    reason: "Sharing page's select, input or note, not yet catalog-owned.",
  },
  "src/pages/settings/CustomAgentConfigForm.tsx › CustomAgentConfigForm › Button": {
    count: 1,
    reason: "Retry button; owning it would rename it.",
  },
  "src/pages/settings/CustomAgentConfigForm.tsx › MCPFields › Button": {
    count: 5,
    reason: "MCP server row in the agent form, the same rows as the create page's.",
  },
  "src/pages/settings/CustomAgentConfigForm.tsx › MCPFields › Input": {
    count: 7,
    reason: "MCP server row in the agent form, the same rows as the create page's.",
  },
  "src/pages/settings/CustomAgentConfigForm.tsx › MCPFields › option": {
    count: 5,
    reason: "MCP server row in the agent form, the same rows as the create page's.",
  },
  "src/pages/settings/CustomAgentConfigForm.tsx › MCPFields › select": {
    count: 2,
    reason: "MCP server row in the agent form, the same rows as the create page's.",
  },
  "src/pages/settings/GlobalShortcutControl.tsx › GlobalShortcutControl › Button": {
    count: 1,
    reason: "Global shortcut row's control, not yet catalog-owned.",
  },
  "src/pages/settings/GlobalShortcutControl.tsx › GlobalShortcutControl › Switch": {
    count: 1,
    reason: "Global shortcut row's control, not yet catalog-owned.",
  },
  "src/pages/settings/HarnessCatalog.tsx › BackButton › Button": {
    count: 1,
    reason: "Back link; owning it would rename it.",
  },
  "src/pages/settings/HarnessCatalog.tsx › BackButton › Link": {
    count: 1,
    reason: "Back link; owning it would rename it.",
  },
  "src/pages/settings/HarnessCatalog.tsx › CatalogRow › button": {
    count: 1,
    reason: "Harness catalog row's control, not yet catalog-owned.",
  },
  "src/pages/settings/HarnessCatalog.tsx › CatalogRow › li": {
    count: 1,
    reason: "Harness catalog row's control, not yet catalog-owned.",
  },
  "src/pages/settings/HarnessCatalog.tsx › HarnessCatalog › SettingsDataRegion": {
    count: 2,
    reason: "Data region's entry is picked at runtime, which the guard can't read.",
  },
  "src/pages/settings/HarnessCatalog.tsx › HarnessCatalog › TabsTrigger": {
    count: 2,
    reason: "Harness page tab, not yet declared as a tab list.",
  },
  "src/pages/settings/HarnessCatalog.tsx › McpRow › summary": {
    count: 1,
    reason: "Per-harness MCP server switch; its name is per server, not yet declared.",
  },
  "src/pages/settings/HarnessCatalog.tsx › McpSwitch › Switch": {
    count: 1,
    reason: "Per-harness MCP server switch; its name is per server, not yet declared.",
  },
  "src/pages/settings/HarnessCatalog.tsx › PluginPage › SettingsDataRegion": {
    count: 1,
    reason: "Data region's entry is picked at runtime, which the guard can't read.",
  },
  "src/pages/settings/HarnessCatalog.tsx › SKILL_MARKDOWN_COMPONENTS › a": {
    count: 1,
    reason: "Link inside a skill's rendered markdown.",
  },
  "src/pages/settings/HarnessCatalog.tsx › SkillPage › SettingsContent": {
    count: 1,
    reason: "Skill content area's entry is picked at runtime, which the guard can't read.",
  },
  "src/pages/settings/HarnessFixDialog.tsx › ChooseCredential › button": {
    count: 1,
    reason: "Credential choice card.",
  },
  "src/pages/settings/HarnessFixDialog.tsx › ChooseCredential › div": {
    count: 1,
    reason: "Credential choice card.",
  },
  "src/pages/settings/HarnessFixDialog.tsx › CommandRow › Button": {
    count: 1,
    reason: "Subscription command button in the fix dialog.",
  },
  "src/pages/settings/HarnessFixDialog.tsx › CopyButton › Button": {
    count: 1,
    reason: "Subscription command button in the fix dialog.",
  },
  "src/pages/settings/HarnessFixDialog.tsx › CredentialCard › button": {
    count: 1,
    reason: "Credential choice card.",
  },
  "src/pages/settings/HarnessFixDialog.tsx › DeviceCodeState › a": {
    count: 1,
    reason: "Open sign-in page link.",
  },
  "src/pages/settings/HarnessFixDialog.tsx › HarnessFixDialog › Button": {
    count: 15,
    reason:
      "Fix dialog's own buttons; the dialog's owner is picked at runtime, which the guard can't read.",
  },
  "src/pages/settings/HarnessFixDialog.tsx › HarnessFixDialog › DialogContent": {
    count: 1,
    reason:
      "Fix dialog's own buttons; the dialog's owner is picked at runtime, which the guard can't read.",
  },
  "src/pages/settings/HarnessFixDialog.tsx › SwitchAccountConfirm › input": {
    count: 1,
    reason: "Fix dialog control; its owner is picked at runtime, which the guard can't read.",
  },
  "src/pages/settings/PickerVisibilitySetting.tsx › PickerVisibilitySetting › CommandInput": {
    count: 1,
    reason: "Picker visibility filter input, not yet catalog-owned.",
  },
  "src/pages/settings/SettingsCustomAgentsSection.tsx › ListStatus › Button": {
    count: 1,
    reason: "Retry button; owning it would rename it.",
  },
  "src/pages/settings/SettingsCustomAgentsSection.tsx › SettingsCustomAgentsSection › Button": {
    count: 2,
    reason: "Retry button; owning it would rename it.",
  },
  "src/pages/settings/SettingsCustomAgentsSection.tsx › SettingsCustomAgentsSection › Link": {
    count: 1,
    reason: "Region row template link, not yet catalog-owned.",
  },
  "src/pages/settings/SettingsHarnessesSection.tsx › CredentialCard › button": {
    count: 1,
    reason: "Credential choice card.",
  },
  "src/pages/settings/SettingsHarnessesSection.tsx › HarnessCard › Link": {
    count: 1,
    reason: "Harness card link, not yet catalog-owned.",
  },
  "src/pages/settings/SettingsHarnessesSection.tsx › HostSelect › Button": {
    count: 1,
    reason: "Machine picker (HostSelect).",
  },
  "src/pages/settings/SettingsHarnessesSection.tsx › HostSelect › DropdownMenuContent": {
    count: 1,
    reason: "Machine picker (HostSelect).",
  },
  "src/pages/settings/SettingsHarnessesSection.tsx › HostSelect › DropdownMenuItem": {
    count: 1,
    reason: "Machine picker (HostSelect).",
  },
  "src/pages/settings/SettingsHarnessesSection.tsx › SettingsHarnessesSection › SettingsOverlayOwner":
    {
      count: 1,
      reason: "Fix dialog's owner is picked at runtime, which the guard can't read.",
    },
  "src/shell/CreateAgentDialog.tsx › AgentFormFields › Button": {
    count: 1,
    reason: "Create form button, not yet catalog-owned.",
  },
  "src/shell/CreateAgentDialog.tsx › AgentFormFields › Input": {
    count: 3,
    reason: 'Create form field whose accessible name includes "*".',
  },
  "src/shell/CreateAgentDialog.tsx › AgentFormFields › SelectContent": {
    count: 1,
    reason: "Overlay of the create form's select, not yet catalog-owned.",
  },
  "src/shell/CreateAgentDialog.tsx › AgentFormFields › SelectItem": {
    count: 1,
    reason: "Option of the create form's select, not yet catalog-owned.",
  },
  "src/shell/CreateAgentDialog.tsx › AgentFormFields › SelectTrigger": {
    count: 1,
    reason: "Create form field, not yet catalog-owned.",
  },
  "src/shell/CreateAgentDialog.tsx › AgentFormFields › Textarea": {
    count: 1,
    reason: "Create form field, not yet catalog-owned.",
  },
  "src/shell/CreateAgentDialog.tsx › CreateAgentForm › Button": {
    count: 2,
    reason: "Create form button, not yet catalog-owned.",
  },
  "src/shell/CreateAgentDialog.tsx › MCPServerRow › Button": {
    count: 1,
    reason: "Create page's MCP server row.",
  },
  "src/shell/CreateAgentDialog.tsx › MCPServerRow › Input": {
    count: 4,
    reason: "Create page's MCP server row.",
  },
  "src/shell/CreateAgentDialog.tsx › MCPServerRow › SelectContent": {
    count: 1,
    reason: "Create page's MCP server row.",
  },
  "src/shell/CreateAgentDialog.tsx › MCPServerRow › SelectItem": {
    count: 2,
    reason: "Create page's MCP server row.",
  },
  "src/shell/CreateAgentDialog.tsx › MCPServerRow › SelectTrigger": {
    count: 1,
    reason: "Create page's MCP server row.",
  },
  "src/shell/CreateAgentDialog.tsx › MCPServerRow › Textarea": {
    count: 2,
    reason: "Create page's MCP server row.",
  },
  "src/shell/HarnessCredentialForm.tsx › AdoptRow › Button": {
    count: 1,
    reason: "Credential form control; its owner is picked at runtime, which the guard can't read.",
  },
  "src/shell/HarnessCredentialForm.tsx › HarnessCredentialForm › Input": {
    count: 3,
    reason: "Credential form control; its owner is picked at runtime, which the guard can't read.",
  },
  "src/shell/HarnessCredentialForm.tsx › HarnessCredentialForm › button": {
    count: 1,
    reason: "Credential form control; its owner is picked at runtime, which the guard can't read.",
  },
  "src/shell/HarnessCredentialForm.tsx › SaveButton › Button": {
    count: 1,
    reason: "Credential form control; its owner is picked at runtime, which the guard can't read.",
  },
};

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
