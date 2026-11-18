// Loads the CodeMirror composer on demand. The textarea keeps working while it loads, and
// stays in use if loading fails, until a retry succeeds.

import type { ComponentType } from "react";
import type { CodeMirrorComposerEnvironment } from "./CodeMirrorComposerInput";
import type * as CodeMirrorComposerModule from "./codeMirrorComposerModule";
import {
  COMPOSER_INPUT_ATTRIBUTE,
  COMPOSER_INPUT_ID_ATTRIBUTE,
  registerComposerSurface,
  type ComposerInputProps,
} from "./composerInput";

export type CodeMirrorComposerLoad =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; Component: ComponentType<ComposerInputProps> }
  | { status: "failed"; error: unknown };

type Importer = () => Promise<
  Pick<typeof CodeMirrorComposerModule, "createCodeMirrorComposerInput">
>;

/** What the lazy editor takes from the app, so its chunk imports nothing the app already loaded. */
export function codeMirrorComposerEnvironment(): CodeMirrorComposerEnvironment {
  return {
    inputAttribute: COMPOSER_INPUT_ATTRIBUTE,
    inputIdAttribute: COMPOSER_INPUT_ID_ATTRIBUTE,
    registerSurface: registerComposerSurface,
  };
}

const defaultImporter: Importer = () => import("./codeMirrorComposerModule");

let importer: Importer = defaultImporter;
let current: CodeMirrorComposerLoad = { status: "idle" };
const listeners = new Set<() => void>();

function settle(next: CodeMirrorComposerLoad) {
  current = next;
  for (const listener of listeners) listener();
}

export function codeMirrorComposerLoad(): CodeMirrorComposerLoad {
  return current;
}

export function subscribeCodeMirrorComposerLoad(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Start loading unless it is loading or loaded; a failed load starts again. */
export function loadCodeMirrorComposer(): void {
  if (current.status === "loading" || current.status === "ready") return;
  settle({ status: "loading" });
  importer().then(
    (module) =>
      settle({
        status: "ready",
        Component: module.createCodeMirrorComposerInput(codeMirrorComposerEnvironment()),
      }),
    (error: unknown) => {
      console.warn("The CodeMirror composer couldn't load; using the standard input.", error);
      settle({ status: "failed", error });
    },
  );
}

/** Test seam: swap the module import and forget any earlier load. */
export function resetCodeMirrorComposerLoadForTests(nextImporter: Importer = defaultImporter) {
  importer = nextImporter;
  current = { status: "idle" };
  listeners.clear();
}
