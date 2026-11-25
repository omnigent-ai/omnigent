// The workspace sidebar's shortcuts now live in the app's registry; this re-exports them
// until each importer reads the registry directly.

export {
  activeShortcutChords,
  chordAriaShortcut,
  codeMirrorKey,
  codeMirrorKeys,
  matchesShortcut,
  type ShortcutBinding,
  shortcutAriaKeys,
  shortcutBinding,
  type ShortcutId,
  shortcutKeys,
  WORKSPACE_SIDEBAR_BINDINGS,
  type WorkspaceSidebarShortcutId,
} from "@/lib/shortcuts/registry";
