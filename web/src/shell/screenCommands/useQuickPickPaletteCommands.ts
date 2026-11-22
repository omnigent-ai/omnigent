// A screen's ⌘K commands that only the QuickPick palette lists: with quick_pick_palette off
// they don't register, so the current palette lists what it did before.

import { isFeatureEnabled } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";

const NO_COMMANDS: PaletteCommand[] = [];

/** Whether ⌘K is the QuickPick palette, which lists each screen's own actions. */
export function useQuickPickPaletteEnabled(): boolean {
  return isFeatureEnabled(useServerInfo(), "quick_pick_palette");
}

/** `usePaletteCommands`, registering only while quick_pick_palette is on. */
export function useQuickPickPaletteCommands(
  source: string,
  heading: string,
  commands: PaletteCommand[],
): void {
  const enabled = useQuickPickPaletteEnabled();
  usePaletteCommands(source, heading, enabled ? commands : NO_COMMANDS);
}
