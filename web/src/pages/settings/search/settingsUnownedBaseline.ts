/**
 * Controls the source guard accepts as unowned, by site: how many there are and why, shared by
 * the guard and the real-page audit. The whole-tree run must find exactly these; a section's run
 * may find no more than the baseline allows.
 */
export const UNOWNED_CONTROLS: Readonly<Record<string, { count: number; reason: string }>> = {
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
  "src/pages/settings/HarnessCatalog.tsx › CatalogRow › button": {
    count: 1,
    reason:
      "Skill or plugin row's open button; its region's owner is a prop, which the guard can't read.",
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
  "src/pages/settings/SettingsHarnessesSection.tsx › CredentialCard › button": {
    count: 1,
    reason:
      "Credential card's button; its control is passed in as a prop, which the guard can't read.",
  },
};
