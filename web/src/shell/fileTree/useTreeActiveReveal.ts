// Reveal Active File for a Files tree that browses a remembered folder: takes up the
// requests ⌘K and the tree's header post, roots the tree back at the workspace when
// the browsed folder doesn't hold the file, and hands the tree one reveal at a time.

import { useCallback, useEffect, useRef, useState } from "react";
import { rememberBrowseLocation } from "../filesPane/browseLocations";
import { activeFileTreePath } from "./activeFileTreePath";
import type { FilesTreeActiveReveal } from "./FilesTree";
import {
  type ActiveFileRevealPlan,
  planActiveFileReveal,
  useActiveFileRevealRequests,
} from "./revealActiveFile";

export interface TreeActiveReveal {
  /** Whether the viewed file can be revealed, and why not. */
  plan: ActiveFileRevealPlan;
  /** The reveal for the tree browsing `location`, or null. */
  activeReveal: FilesTreeActiveReveal | null;
  /** The tree took up the reveal with this key. */
  onActiveRevealHandled: (key: number) => void;
  /** Reveals the viewed file, as the header's button does. */
  revealActiveFile: () => void;
}

interface PendingReveal {
  conversationId: string;
  /** The folder the reveal's file is relative to, in the tree's wire form. */
  location: string;
  reveal: FilesTreeActiveReveal;
}

export function useTreeActiveReveal({
  conversationId,
  active,
  activePath,
  location,
  workspaceRoot,
  workspaceKnown,
  showHidden,
}: {
  conversationId: string;
  /** The tree is on screen, so it takes up this session's reveal requests. */
  active: boolean;
  /** The viewed file, workspace-relative or absolute. */
  activePath: string | null;
  /** The browsed folder: "" for the workspace root, relative inside it, absolute outside. */
  location: string;
  workspaceRoot: string | null;
  /** The workspace's root is known, or can't be, so a reveal can be planned. */
  workspaceKnown: boolean;
  showHidden: boolean;
}): TreeActiveReveal {
  const plan = planActiveFileReveal({ activePath, location, workspaceRoot, showHidden });
  const [pending, setPending] = useState<PendingReveal | null>(null);
  const nextKey = useRef(0);
  const reveal = (file: string | null) => {
    const filePlan = planActiveFileReveal({
      activePath: file,
      location,
      workspaceRoot,
      showHidden,
    });
    if (filePlan.kind !== "reveal") return;
    if (filePlan.rerootToWorkspace) rememberBrowseLocation(conversationId, null);
    const revealLocation = filePlan.rerootToWorkspace ? "" : location;
    nextKey.current += 1;
    setPending({
      conversationId,
      location: revealLocation,
      reveal: {
        key: nextKey.current,
        file: activeFileTreePath(file, revealLocation, workspaceRoot),
      },
    });
  };
  const onActiveRevealHandled = useCallback(
    (key: number) => setPending((current) => (current?.reveal.key === key ? null : current)),
    [],
  );
  const inScope =
    pending !== null && pending.conversationId === conversationId && pending.location === location;
  // Moving to another session or folder before the tree took the reveal up drops it.
  useEffect(() => {
    if (pending && !inScope) setPending(null);
  }, [pending, inScope]);

  // A request asked for before the workspace answers waits for it, in its own session.
  const [ask, setAsk] = useState<{ conversationId: string; file: string | null } | null>(null);
  useActiveFileRevealRequests(active ? conversationId : undefined, (file) =>
    setAsk({ conversationId, file }),
  );
  useEffect(() => {
    if (!ask || !workspaceKnown) return;
    setAsk(null);
    // A file opened since the request wins; the captured one stands in once it closed.
    if (ask.conversationId === conversationId) reveal(activePath ?? ask.file);
    // Runs once per request, with the workspace of the render that took it up.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ask, workspaceKnown]);

  return {
    plan,
    activeReveal: inScope ? pending.reveal : null,
    onActiveRevealHandled,
    revealActiveFile: () => reveal(activePath),
  };
}
