// Shared look for the workspace's file trees: the app's font and theme tokens, and rows
// whose names give way before their counts and whose deepest levels stop stepping in.

import type { CSSProperties } from "react";
import "./fileTreeStyle.css";

/** The class the workspace's trees add for the rules in `fileTreeStyle.css`. */
export const WORKSPACE_FILE_TREE_CLASS = "workspace-file-tree";

/** Maps the tree's colors and type onto the app's theme tokens. */
export const FILE_TREE_THEME: CSSProperties = {
  "--trees-font-family-override": "var(--ui-font-family, var(--font-sans))",
  "--trees-font-size-override": "var(--text-ui, 13px)",
  "--trees-bg-override": "var(--background)",
  "--trees-bg-muted-override": "var(--muted)",
  "--trees-fg-override": "var(--foreground)",
  "--trees-fg-muted-override": "var(--muted-foreground)",
  "--trees-accent-override": "var(--primary)",
  "--trees-selected-bg-override": "var(--sidebar-active, var(--accent))",
  "--trees-focus-ring-color-override": "var(--ring)",
  "--trees-level-gap-override": "6px",
  "--trees-padding-inline-override": "8px",
} as CSSProperties;
