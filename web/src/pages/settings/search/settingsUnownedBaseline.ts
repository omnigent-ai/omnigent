/**
 * Controls the source guard accepts as unowned, by site: how many there are and why, shared by
 * the guard and the real-page audit. The whole-tree run must find exactly these; a section's run
 * may find no more than the baseline allows.
 */
export const UNOWNED_CONTROLS: Readonly<Record<string, { count: number; reason: string }>> = {
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
  "src/pages/settings/GlobalShortcutControl.tsx › GlobalShortcutControl › Button": {
    count: 1,
    reason: "Global shortcut row's control, not yet catalog-owned.",
  },
  "src/pages/settings/GlobalShortcutControl.tsx › GlobalShortcutControl › Switch": {
    count: 1,
    reason: "Global shortcut row's control, not yet catalog-owned.",
  },
  "src/pages/settings/HarnessCatalog.tsx › CatalogRow › button": {
    count: 1,
    reason:
      "Skill or plugin row's open button; its region's owner is a prop, which the guard can't read.",
  },
  "src/pages/settings/HarnessCatalog.tsx › HarnessCatalog › SettingsDataRegion": {
    count: 2,
    reason: "Data region's entry is picked at runtime, which the guard can't read.",
  },
  "src/pages/settings/HarnessCatalog.tsx › McpRow › summary": {
    count: 1,
    reason:
      "MCP server row's open trigger, named by the server's own data, as its list's rows are.",
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
  "src/pages/settings/SettingsHarnessesSection.tsx › CredentialCard › button": {
    count: 1,
    reason: "Credential choice card.",
  },
  "src/pages/settings/SettingsHarnessesSection.tsx › SettingsHarnessesSection › SettingsOverlayOwner":
    {
      count: 1,
      reason: "Fix dialog's owner is picked at runtime, which the guard can't read.",
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
