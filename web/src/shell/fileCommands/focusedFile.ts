// The file row or link keyboard focus is in, so ⌘K can act on it. Focus moving
// into the palette or a menu keeps it; focus moving anywhere else lets it go.

import { createFocusedTarget } from "../focusedTarget";
import type { FileCommandTarget } from "./target";

const focusedFile = createFocusedTarget<FileCommandTarget>();

export const useFocusedFileStore = focusedFile.useStore;

/** Make `target`, named by `element` (a Files row or chat link), ⌘K's file while focus stays in it. */
export const publishFocusedFile = focusedFile.publish;

/**
 * Makes the row or link that owns `target` ⌘K's file while focus is in it:
 * spread the returned handler as its `onFocus`. A target whose facts change
 * (the workspace location loads, the host resolves) is republished, and an
 * unmounted row lets go.
 */
export const useFocusedFileSource = focusedFile.useFocusSource;

/** The focused row's or link's file, while it's still on the page. */
export const useFocusedFile = focusedFile.useFocused;
