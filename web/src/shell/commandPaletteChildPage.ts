// The command palette's child pages: a setting's values and a command's options. They live
// outside the palette component so work that settles late can tell whether its page still shows.

import { create } from "zustand";

/** Where Back returns to: the parent page's query and active row. */
export interface PaletteParentPlace {
  query: string;
  activeId: string | null;
}

export type PaletteChildPage =
  /** A command's options, bound to its row and the command target then. */
  | { kind: "command-options"; itemId: string; targetKey: string; label: string }
  /**
   * A setting's values, bound to the row that opened it: its palette id, the setting
   * and owner it named, and the command target then. If any of them changes, the page
   * leaves instead of applying a choice to whatever offers that setting now.
   */
  | {
      kind: "setting-choices";
      itemId: string;
      settingId: string;
      owner: string;
      targetKey: string;
      /** The value active when the page opened, such as the one the query named. */
      initialValue: string;
    };

interface PaletteChildPageState {
  page: PaletteChildPage | null;
  /** The parent page as it was when the child opened; the palette records it first. */
  parent: PaletteParentPlace | null;
}

export const paletteChildPageStore = create<PaletteChildPageState>(() => ({
  page: null,
  parent: null,
}));

/**
 * Hands out a generation per intent so work that finishes late can tell whether
 * it still applies. Starting a new intent or revoking makes every earlier one stale.
 */
export interface IntentGenerations {
  /** Start a new intent, making every earlier one stale. */
  begin: () => number;
  /** Make every intent stale, such as on Back, a cancel or a new root. */
  revoke: () => void;
  isCurrent: (generation: number) => boolean;
}

export function createIntentGenerations(): IntentGenerations {
  let current = 0;
  return {
    begin: () => (current += 1),
    revoke: () => {
      current += 1;
    },
    isCurrent: (generation) => generation === current,
  };
}

/** Every child-page intent, so work that finishes late can tell whether it still applies. */
export const paletteIntents = createIntentGenerations();

/** Remember where Back returns to, just before a row opens a child page. */
export function rememberPaletteParent(parent: PaletteParentPlace): void {
  paletteChildPageStore.setState({ parent });
}

export function openPaletteChildPage(page: PaletteChildPage): void {
  paletteIntents.revoke();
  paletteChildPageStore.setState({ page });
}

/** Back to the parent page; anything the child page was waiting on no longer applies. */
export function leavePaletteChildPage(): PaletteParentPlace | null {
  paletteIntents.revoke();
  const { parent } = paletteChildPageStore.getState();
  paletteChildPageStore.setState({ page: null, parent: null });
  return parent;
}

/** Leave any child page for a fresh root, as closing or reopening the palette does. */
export function resetPaletteToRoot(): void {
  paletteIntents.revoke();
  paletteChildPageStore.setState({ page: null, parent: null });
}
