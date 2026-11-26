import { useEffect, useRef } from "react";
import { releaseConversation } from "@/store/chatStore";

/**
 * Closes the stream of a tile that stops being live, and of every tile but the
 * active one when the Tile view unmounts. The conversation registry would
 * otherwise keep them open and exhaust HTTP/1.1's per-origin connection pool.
 */
export function useReleaseIdleTileStreams(liveIds: ReadonlySet<string>, activeId: string | null) {
  const streamedIds = useRef<ReadonlySet<string>>(new Set());
  const activeIdRef = useRef(activeId);
  activeIdRef.current = activeId;

  useEffect(() => {
    for (const id of streamedIds.current) {
      if (!liveIds.has(id)) releaseConversation(id);
    }
    streamedIds.current = new Set(liveIds);
  }, [liveIds]);

  useEffect(
    () => () => {
      // The next view most likely opens the active agent, so keep its stream.
      for (const id of streamedIds.current) {
        if (id !== activeIdRef.current) releaseConversation(id);
      }
    },
    [],
  );
}
