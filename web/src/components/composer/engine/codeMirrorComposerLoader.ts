// Loads the CodeMirror composer on demand. The textarea keeps working while it loads, and
// stays in use if loading fails. Some browsers cache a failed module fetch, so a retry may
// need a page reload before it succeeds.

import type { ComponentType } from "react";
import { findMentionTokens, separateMentionEdges } from "@/lib/composerMentions";
import { currentShortcutEnvironment } from "@/lib/shortcuts/registry";
import { COMMAND_TOKEN_CLASS } from "@/components/composer/commandToken";
import { MENTION_TOKEN_CLASS } from "@/components/composer/MentionTokenOverlay";
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
    mac: currentShortcutEnvironment().mac,
    inputAttribute: COMPOSER_INPUT_ATTRIBUTE,
    inputIdAttribute: COMPOSER_INPUT_ID_ATTRIBUTE,
    registerSurface: registerComposerSurface,
    findMentionTokens,
    separateMentionEdges,
    mentionTokenClass: MENTION_TOKEN_CLASS,
    commandTokenClass: COMMAND_TOKEN_CLASS,
  };
}

const defaultImporter: Importer = () => import("./codeMirrorComposerModule");

let importer: Importer = defaultImporter;
let current: CodeMirrorComposerLoad = { status: "idle" };
const listeners = new Set<() => void>();
// Bumped on reset, so a load that started earlier can't settle the fresh state.
let generation = 0;

function settle(next: CodeMirrorComposerLoad) {
  current = next;
  for (const listener of listeners) {
    try {
      listener();
    } catch (error: unknown) {
      console.error("A CodeMirror composer listener threw.", error);
    }
  }
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
  const attempt = generation;
  const startImport = importer;
  settle({ status: "loading" });
  // A synchronous importer throw becomes a rejection, so the load settles failed.
  const built = Promise.resolve()
    .then(() => startImport())
    .then((module) => ({
      status: "ready" as const,
      Component: module.createCodeMirrorComposerInput(codeMirrorComposerEnvironment()),
    }));
  built.then(
    (next) => {
      if (attempt !== generation) return;
      settle(next);
    },
    (error: unknown) => {
      if (attempt !== generation) return;
      console.warn("The CodeMirror composer couldn't load; using the standard input.", error);
      settle({ status: "failed", error });
    },
  );
}

/** Test seam: swap the module import and forget any earlier load. */
export function resetCodeMirrorComposerLoadForTests(nextImporter: Importer = defaultImporter) {
  generation += 1;
  importer = nextImporter;
  current = { status: "idle" };
  listeners.clear();
}
