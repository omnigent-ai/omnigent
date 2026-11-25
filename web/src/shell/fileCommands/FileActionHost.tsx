// Carries out file actions requested from any surface: menus, toolbar buttons,
// and ⌘K ask through `requestFileAction`, and this one host re-checks the action
// against the target's current facts, then runs it.

import { useEffect } from "react";
import { CODE_EDITORS, launchEditorLink } from "@/lib/editorLinks";
import { useRebasePath } from "@/lib/routing";
import { revealInFileManager } from "../RevealInFileManager";
import { isCommandAvailable } from "@/store/paletteCommands";
import { copyWithToast } from "./copyWithToast";
import { rememberPreferredEditor } from "./preferredEditor";
import { fileCommandsById, OPEN_IN_EDITOR } from "./model";
import {
  claimFileAction,
  type FileActionRequest,
  type HostedFileCommandId,
  nextFileActionRequest,
  useFileActionStore,
} from "./fileActionStore";
import { useFocusedFile } from "./focusedFile";
import { pathToCopy } from "./target";
import { useFileCommandsForTarget } from "./useFileCommands";
import { fileCommandFacts, fileEditorLink, useFileCommandEnvironment } from "./useFileCommandFacts";
import { useFilePaletteCommands } from "./useFilePaletteCommands";

/**
 * A link that opens `relativePath` in session `conversationId`'s viewer,
 * keeping `linkState`'s other query parameters.
 */
export function fileLink(
  conversationId: string,
  relativePath: string,
  rebasePath: (path: string) => string,
  linkState = "",
  origin = window.location.origin,
): string {
  const url = new URL(rebasePath(`/c/${conversationId}`), origin);
  url.search = linkState;
  url.searchParams.set("file", relativePath);
  return url.toString();
}

/** Offers the focused Files row's or file link's actions in ⌘K. */
function useFocusedFilePaletteCommands() {
  const target = useFocusedFile();
  // The row or link opens its file itself, so Open isn't offered for it.
  useFilePaletteCommands("focused", useFileCommandsForTarget(target), target !== null);
}

/** Runs each requested action, and offers the focused Files row's actions in ⌘K. */
export function FileActionHost() {
  useFocusedFilePaletteCommands();
  const request = useFileActionStore(nextFileActionRequest);
  return request ? <FileActionRunner key={request.key} request={request} /> : null;
}

function FileActionRunner({ request }: { request: FileActionRequest }) {
  const { target, command, linkState } = request;
  const rebasePath = useRebasePath();
  const environment = useFileCommandEnvironment(target.hostId, target.conversationId);

  useEffect(() => {
    // StrictMode runs this effect twice; only the first run claims the request.
    if (!claimFileAction(request)) return;
    // An action that stopped applying since the request was made does nothing.
    const facts = fileCommandFacts(target, environment);
    if (!isCommandAvailable(fileCommandsById(facts)[command].availability)) return;
    runFileAction(command);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- runs once per request
  }, []);

  function runFileAction(id: HostedFileCommandId) {
    const { relativePath, absolutePath, conversationId } = target;
    switch (id) {
      case "copy-path":
      case "copy-relative-path": {
        // Each copies the other's path when its own isn't known, and says which it copied;
        // a repository file's path, such as a pull request's, is its full path.
        const copied = pathToCopy(target, id);
        if (copied?.absolute || (copied && target.repositoryFile && id === "copy-path")) {
          copyWithToast(copied.text, "Copied path", "Couldn't copy the path");
        } else if (copied) {
          copyWithToast(copied.text, "Copied relative path", "Couldn't copy the relative path");
        }
        return;
      }
      case "copy-link":
        if (relativePath !== null && conversationId !== null) {
          copyWithToast(
            fileLink(conversationId, relativePath, rebasePath, linkState),
            "Link copied",
            "Couldn't copy the link",
          );
        }
        return;
      case "reveal":
        if (target.hostId !== null && absolutePath !== null) {
          revealInFileManager({ hostId: target.hostId, path: absolutePath });
        }
        return;
      default: {
        const editor = CODE_EDITORS.find(({ id: editorId }) => OPEN_IN_EDITOR[editorId] === id);
        const url = editor && fileEditorLink(editor.id, target, environment);
        if (!editor || !url) return;
        rememberPreferredEditor(editor.id);
        launchEditorLink(url);
      }
    }
  }

  return null;
}
