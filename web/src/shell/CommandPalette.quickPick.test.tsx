// Runs every CommandPalette case against the QuickPick palette, so both
// implementations of ⌘K are held to the same behavior.
import { vi } from "vitest";

import type * as CommandPaletteModule from "./CommandPalette";

vi.mock("./CommandPalette", async (importOriginal) => {
  const actual = await importOriginal<typeof CommandPaletteModule>();
  const { QuickPickCommandPalette } = await import("./QuickPickCommandPalette");
  return { ...actual, CommandPalette: QuickPickCommandPalette };
});

await import("./CommandPalette.test");
