import { useEffect, useMemo, useState } from "react";
import { BlockStream } from "@/lib/blockStream";
import type { AnyBlock } from "@/lib/blocks";
import { itemsToBlocks } from "@/lib/itemsToBlocks";
import { buildBubbles, type Bubble } from "@/lib/renderItems";
import {
  bindOnlyOnlineRunner,
  fetchSessionItemsPage,
  getSession,
  openSessionStream,
  postEvent,
} from "@/lib/sessionsApi";
import { parseSseStream } from "@/lib/sse";

export interface LocalChatMessage {
  id: string;
  text: string;
}

export interface DpiaSessionChat {
  historyBubbles: Bubble[];
  liveBubbles: Bubble[];
  localMessages: LocalChatMessage[];
  streamState: "idle" | "connecting" | "connected" | "failed";
  chatError: string | null;
  sending: boolean;
  sendMessage: (wireText: string, displayText: string) => Promise<boolean>;
  retry: () => void;
}

export function useDpiaSessionChat(sessionId: string | undefined): DpiaSessionChat {
  const [historyBlocks, setHistoryBlocks] = useState<AnyBlock[]>([]);
  const [liveBlocks, setLiveBlocks] = useState<AnyBlock[]>([]);
  const [localMessages, setLocalMessages] = useState<LocalChatMessage[]>([]);
  const [streamState, setStreamState] = useState<DpiaSessionChat["streamState"]>("idle");
  const [chatError, setChatError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [connectionAttempt, setConnectionAttempt] = useState(0);

  useEffect(() => {
    if (!sessionId) {
      setStreamState("idle");
      setHistoryBlocks([]);
      setLiveBlocks([]);
      return;
    }
    const boundSessionId = sessionId;
    const abortController = new AbortController();
    let active = true;
    setStreamState("connecting");
    setChatError(null);
    setHistoryBlocks([]);
    setLiveBlocks([]);
    setLocalMessages([]);

    async function connect() {
      try {
        const [response, history, snapshot] = await Promise.all([
          openSessionStream(boundSessionId, abortController.signal),
          fetchSessionItemsPage(boundSessionId, { limit: 60 }),
          getSession(boundSessionId),
        ]);
        if (!response.ok) throw new Error(`The conversation stream failed (${response.status}).`);
        if (!response.body) throw new Error("The conversation stream returned no response body.");
        if (snapshot.runnerId == null) {
          const bound = await bindOnlyOnlineRunner(boundSessionId);
          if (!bound) {
            await response.body.cancel();
            throw new Error("No runner is online for this conversation. Start one and retry.");
          }
        }
        if (!active) return;
        setHistoryBlocks(itemsToBlocks(history.items));
        setStreamState("connected");

        const reducer = new BlockStream({ textFlushThreshold: 64 });
        for await (const block of reducer.reduce(parseSseStream(response.body))) {
          if (!active) return;
          if (block.type === "response_end" && block.status === "completed") {
            const committed = await fetchSessionItemsPage(boundSessionId, { limit: 60 });
            if (!active) return;
            setHistoryBlocks(itemsToBlocks(committed.items));
            setLiveBlocks([]);
            setLocalMessages([]);
            continue;
          }
          setLiveBlocks((current) => [...current, block]);
        }
      } catch (error) {
        if (!active || abortController.signal.aborted) return;
        setStreamState("failed");
        setChatError(error instanceof Error ? error.message : "The conversation disconnected.");
      }
    }

    void connect();
    return () => {
      active = false;
      abortController.abort();
    };
  }, [connectionAttempt, sessionId]);

  async function sendMessage(wireText: string, displayText: string): Promise<boolean> {
    if (!sessionId || sending || streamState !== "connected") return false;
    const localId = `local-${Date.now()}`;
    setLocalMessages((current) => [...current, { id: localId, text: displayText }]);
    setSending(true);
    setChatError(null);
    try {
      const result = await postEvent(sessionId, {
        type: "message",
        data: { role: "user", content: [{ type: "input_text", text: wireText }] },
      });
      if (result.denied) {
        setLocalMessages((current) => current.filter(({ id }) => id !== localId));
        setChatError("The agent policy declined that message.");
        return false;
      }
      return true;
    } catch (error) {
      setLocalMessages((current) => current.filter(({ id }) => id !== localId));
      setChatError(error instanceof Error ? error.message : "The message could not be sent.");
      return false;
    } finally {
      setSending(false);
    }
  }

  const historyBubbles = useMemo(() => buildBubbles(historyBlocks, null), [historyBlocks]);
  const liveBubbles = useMemo(() => buildBubbles(liveBlocks, null), [liveBlocks]);

  return {
    historyBubbles,
    liveBubbles,
    localMessages,
    streamState,
    chatError,
    sending,
    sendMessage,
    retry: () => setConnectionAttempt((current) => current + 1),
  };
}

export { bubbleText } from "@/lib/dpia/requestInbox";
