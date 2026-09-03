import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangleIcon,
  BotIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  FilePenLineIcon,
  Loader2Icon,
  SendIcon,
} from "lucide-react";
import { ApprovalCard, type SubmitApprovalFn } from "@/components/blocks/ApprovalCard";
import { FilePathAwareMessageResponse } from "@/components/blocks/BlockRenderer";
import { Button } from "@/components/ui/button";
import { BlockStream } from "@/lib/blockStream";
import type { AnyBlock, MessageContentBlock } from "@/lib/blocks";
import { itemsToBlocks } from "@/lib/itemsToBlocks";
import { buildBubbles, type Bubble } from "@/lib/renderItems";
import {
  approve,
  bindOnlyOnlineRunner,
  fetchSessionItemsPage,
  getSession,
  openSessionStream,
  postEvent,
} from "@/lib/sessionsApi";
import { parseEvent, parseSseStream } from "@/lib/sse";
import { buildDpiaAgentMessage, officerMessageFromAgentMessage } from "@/lib/dpia/agentContext";
import { parseCorrectionProposalText } from "@/lib/dpia/correctionProposal";
import type {
  CorrectionProposal,
  CorrectionProposalRecord,
  DpiaCaseSnapshot,
} from "@/lib/dpia/types";
import { CorrectionProposalCard, ManualCorrectionDialog } from "./CorrectionProposalCard";

interface DpiaCaseChatProps {
  caseData: DpiaCaseSnapshot;
  sessionId?: string;
  connecting: boolean;
  bindingError: string | null;
  onConnect: () => void;
  onStageProposal: (proposal: CorrectionProposal, source: "agent" | "manual") => void;
  onEditProposal: (proposalId: string, proposal: CorrectionProposal) => void;
  onApplyProposal: (proposal: CorrectionProposal) => void;
  onRejectProposal: (proposalId: string) => void;
}

interface LocalMessage {
  id: string;
  text: string;
}

type RespondedMap = Record<
  string,
  { action: "accept" | "decline"; content?: Record<string, unknown> }
>;

function contentText(content: MessageContentBlock[]): string {
  return content
    .filter(
      (block): block is Extract<MessageContentBlock, { type: "input_text" | "output_text" }> =>
        block.type === "input_text" || block.type === "output_text",
    )
    .map((block) => block.text)
    .join("\n");
}

function pendingElicitationBlocks(rawEvents: Record<string, unknown>[]): AnyBlock[] {
  const events = rawEvents.flatMap((raw) => {
    const event = parseEvent("response.elicitation_request", raw);
    return event?.type === "elicitation_request" ? [event] : [];
  });
  return new BlockStream().reduceSync(events);
}

export function DpiaCaseChat({
  caseData,
  sessionId,
  connecting,
  bindingError,
  onConnect,
  onStageProposal,
  onEditProposal,
  onApplyProposal,
  onRejectProposal,
}: DpiaCaseChatProps) {
  const [expanded, setExpanded] = useState(false);
  const [draft, setDraft] = useState("");
  const [historyBlocks, setHistoryBlocks] = useState<AnyBlock[]>([]);
  const [liveBlocks, setLiveBlocks] = useState<AnyBlock[]>([]);
  const [localMessages, setLocalMessages] = useState<LocalMessage[]>([]);
  const [responded, setResponded] = useState<RespondedMap>({});
  const [streamState, setStreamState] = useState<"idle" | "connecting" | "connected" | "failed">(
    "idle",
  );
  const [chatError, setChatError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [connectionAttempt, setConnectionAttempt] = useState(0);
  const [manualDialogOpen, setManualDialogOpen] = useState(false);
  const [editingRecord, setEditingRecord] = useState<CorrectionProposalRecord | null>(null);
  const transcriptEndRef = useRef<HTMLDivElement | null>(null);
  const detectedProposalRef = useRef(new Set<string>());

  const historyBubbles = useMemo(() => buildBubbles(historyBlocks, null), [historyBlocks]);
  const liveBubbles = useMemo(() => buildBubbles(liveBlocks, null), [liveBlocks]);
  const pendingProposals = (caseData.correctionProposals ?? []).filter(
    ({ status }) => status === "pending",
  );
  const detectedProposals = useMemo(
    () =>
      [...historyBubbles, ...liveBubbles].flatMap((bubble) => {
        if (bubble.kind !== "assistant") return [];
        const text = bubble.items
          .filter((item) => item.kind === "text")
          .map((item) => item.text)
          .join("");
        const proposal = parseCorrectionProposalText(text);
        return proposal ? [proposal] : [];
      }),
    [historyBubbles, liveBubbles],
  );

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
    setResponded({});
    detectedProposalRef.current.clear();

    async function connect() {
      try {
        const [response, history, snapshot] = await Promise.all([
          openSessionStream(boundSessionId, abortController.signal),
          fetchSessionItemsPage(boundSessionId, { limit: 40 }),
          getSession(boundSessionId),
        ]);
        if (!response.ok) throw new Error(`Case chat stream failed (${response.status}).`);
        if (!response.body) throw new Error("Case chat stream returned no response body.");
        if (snapshot.runnerId == null) {
          const bound = await bindOnlyOnlineRunner(boundSessionId);
          if (!bound) {
            await response.body.cancel();
            throw new Error(
              "The case session is bound, but no runner is online. Start the local runner and retry the connection.",
            );
          }
        }
        if (!active) return;
        setHistoryBlocks([
          ...itemsToBlocks(history.items),
          ...pendingElicitationBlocks(snapshot.pendingElicitations ?? []),
        ]);
        setStreamState("connected");

        const reducer = new BlockStream({ textFlushThreshold: 64 });
        for await (const block of reducer.reduce(parseSseStream(response.body))) {
          if (!active) return;
          if (block.type === "response_end" && block.status === "completed") {
            const committed = await fetchSessionItemsPage(boundSessionId, { limit: 40 });
            if (!active) return;
            setHistoryBlocks(itemsToBlocks(committed.items));
            setLiveBlocks([]);
            continue;
          }
          setLiveBlocks((current) => [...current, block]);
          setExpanded(true);
        }
      } catch (error) {
        if (!active || abortController.signal.aborted) return;
        setStreamState("failed");
        setChatError(error instanceof Error ? error.message : "The case chat disconnected.");
      }
    }

    void connect();
    return () => {
      active = false;
      abortController.abort();
    };
  }, [connectionAttempt, sessionId]);

  useEffect(() => {
    for (const proposal of detectedProposals) {
      const fingerprint = JSON.stringify(proposal);
      if (detectedProposalRef.current.has(fingerprint)) continue;
      detectedProposalRef.current.add(fingerprint);
      try {
        onStageProposal(proposal, "agent");
      } catch (error) {
        setChatError(
          error instanceof Error ? error.message : "The correction proposal is invalid.",
        );
      }
    }
  }, [detectedProposals, onStageProposal]);

  useEffect(() => {
    if (expanded) transcriptEndRef.current?.scrollIntoView({ block: "nearest" });
  }, [expanded, historyBubbles, liveBubbles, localMessages]);

  async function sendMessage() {
    const message = draft.trim();
    if (!sessionId || !message || sending || streamState !== "connected") return;
    const localId = `local-${Date.now()}`;
    setLocalMessages((current) => [...current, { id: localId, text: message }]);
    setDraft("");
    setExpanded(true);
    setSending(true);
    setChatError(null);
    try {
      const result = await postEvent(sessionId, {
        type: "message",
        data: {
          role: "user",
          content: [{ type: "input_text", text: buildDpiaAgentMessage(caseData, message) }],
        },
      });
      if (result.denied) {
        setLocalMessages((current) => current.filter(({ id }) => id !== localId));
        setChatError("The case agent policy declined that message.");
      }
    } catch (error) {
      setLocalMessages((current) => current.filter(({ id }) => id !== localId));
      setDraft(message);
      setChatError(error instanceof Error ? error.message : "The message could not be sent.");
    } finally {
      setSending(false);
    }
  }

  function submitApproval(
    resolveSessionId: string,
    elicitationId: string,
    action: "accept" | "decline",
    content?: Record<string, unknown>,
  ) {
    setResponded((current) => ({
      ...current,
      [elicitationId]: content === undefined ? { action } : { action, content },
    }));
    const result = content === undefined ? { action } : { action, content };
    void approve(resolveSessionId, elicitationId, result).catch((error: unknown) => {
      setResponded((current) => {
        const { [elicitationId]: _failed, ...remaining } = current;
        return remaining;
      });
      setChatError(error instanceof Error ? error.message : "The approval could not be sent.");
    });
  }

  function renderBubbles(bubbles: Bubble[], prefix: string) {
    return bubbles.map((bubble) => {
      if (bubble.kind === "user") {
        const text = officerMessageFromAgentMessage(contentText(bubble.content));
        return (
          <div
            key={`${prefix}-${bubble.itemId}`}
            className="ml-auto max-w-[85%] bg-muted px-3 py-2"
          >
            <FilePathAwareMessageResponse breaks>{text}</FilePathAwareMessageResponse>
          </div>
        );
      }
      if (bubble.kind !== "assistant") return null;
      const text = bubble.items
        .filter((item) => item.kind === "text")
        .map((item) => item.text)
        .join("");
      const errors = bubble.items.filter((item) => item.kind === "error");
      const elicitations = bubble.items.filter((item) => item.kind === "elicitation");
      if (!text && errors.length === 0 && elicitations.length === 0) return null;
      const correctionProposal = parseCorrectionProposalText(text);
      return (
        <div key={`${prefix}-${bubble.stableId}`} className="max-w-3xl space-y-2">
          {text && !correctionProposal && (
            <FilePathAwareMessageResponse>{text}</FilePathAwareMessageResponse>
          )}
          {correctionProposal && (
            <p className="text-sm text-muted-foreground">
              Structured correction proposal received for officer review.
            </p>
          )}
          {errors.map((item) => (
            <p key={item.itemId ?? item.message} role="alert" className="text-sm text-status-red">
              {item.message}
            </p>
          ))}
          {elicitations.map((item) => {
            const response = responded[item.elicitationId] ?? item.response;
            const resolveSessionId = item.targetSessionId ?? sessionId;
            const onSubmit: SubmitApprovalFn = (elicitationId, action, content) => {
              if (resolveSessionId) {
                submitApproval(resolveSessionId, elicitationId, action, content);
              }
            };
            return (
              <ApprovalCard
                key={item.elicitationId}
                {...item}
                status={response ? "responded" : "pending"}
                response={response}
                onSubmit={onSubmit}
              />
            );
          })}
        </div>
      );
    });
  }

  return (
    <section
      className="dpia-no-print sticky bottom-0 z-20 mt-6 overflow-hidden rounded-lg border border-border bg-card shadow-lg"
      aria-label="Case agent chat"
      data-case-id={caseData.id}
    >
      {!sessionId ? (
        <div>
          <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
            <div className="flex min-w-0 items-center gap-3">
              <BotIcon className="size-4 shrink-0 text-muted-foreground" />
              <div>
                <p className="text-sm font-medium">Case agent chat is not connected</p>
                <p className="text-sm text-muted-foreground">
                  Connect the registered DPIA agent to ask questions or instruct a correction.
                </p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <Button type="button" variant="outline" onClick={() => setManualDialogOpen(true)}>
                <FilePenLineIcon data-icon="inline-start" />
                Draft correction manually
              </Button>
              <Button type="button" variant="outline" disabled={connecting} onClick={onConnect}>
                {connecting && <Loader2Icon className="animate-spin" data-icon="inline-start" />}
                {connecting ? "Connecting…" : "Connect case agent"}
              </Button>
            </div>
            {bindingError && (
              <p role="alert" className="w-full text-sm text-status-red">
                {bindingError}
              </p>
            )}
          </div>
          {pendingProposals.length > 0 && (
            <div className="space-y-3 border-t border-border px-4 py-3">
              {pendingProposals.map((record) => (
                <CorrectionProposalCard
                  key={record.id}
                  caseData={caseData}
                  record={record}
                  onApply={() => onApplyProposal(record.proposal)}
                  onEdit={() => setEditingRecord(record)}
                  onReject={() => onRejectProposal(record.id)}
                  onFollowUp={() => setChatError("Connect the case agent to send a follow-up.")}
                />
              ))}
            </div>
          )}
        </div>
      ) : (
        <>
          <button
            type="button"
            className="flex w-full items-center justify-between gap-3 border-b border-border px-4 py-2.5 text-left hover:bg-muted/50"
            aria-expanded={expanded}
            onClick={() => setExpanded((current) => !current)}
          >
            <span className="flex items-center gap-2 text-sm font-medium">
              <BotIcon className="size-4 text-muted-foreground" />
              Case agent chat
              <span className="font-normal text-muted-foreground">
                {streamState === "connected"
                  ? "Connected"
                  : streamState === "connecting"
                    ? "Connecting"
                    : "Needs attention"}
              </span>
            </span>
            {expanded ? (
              <ChevronDownIcon className="size-4" />
            ) : (
              <ChevronUpIcon className="size-4" />
            )}
          </button>

          {expanded && (
            <div
              className="max-h-[min(42vh,28rem)] space-y-3 overflow-y-auto border-b border-border px-4 py-3"
              data-testid="dpia-chat-transcript"
              aria-live="polite"
            >
              {historyBubbles.length === 0 &&
                liveBubbles.length === 0 &&
                localMessages.length === 0 && (
                  <p className="text-sm text-muted-foreground">
                    Ask about the evidence, or instruct the agent to draft a correction proposal.
                  </p>
                )}
              {pendingProposals.map((record) => (
                <CorrectionProposalCard
                  key={record.id}
                  caseData={caseData}
                  record={record}
                  onApply={() => {
                    try {
                      onApplyProposal(record.proposal);
                    } catch (error) {
                      setChatError(
                        error instanceof Error
                          ? error.message
                          : "The correction could not be applied.",
                      );
                    }
                  }}
                  onEdit={() => setEditingRecord(record)}
                  onReject={() => {
                    try {
                      onRejectProposal(record.id);
                    } catch (error) {
                      setChatError(
                        error instanceof Error
                          ? error.message
                          : "The correction could not be rejected.",
                      );
                    }
                  }}
                  onFollowUp={() => {
                    setDraft(`Follow up on correction proposal ${record.id}: `);
                    setExpanded(true);
                  }}
                />
              ))}
              {renderBubbles(historyBubbles, "history")}
              {localMessages.map((message) => (
                <div key={message.id} className="ml-auto max-w-[85%] bg-muted px-3 py-2">
                  <FilePathAwareMessageResponse breaks>{message.text}</FilePathAwareMessageResponse>
                </div>
              ))}
              {renderBubbles(liveBubbles, "live")}
              <div ref={transcriptEndRef} />
            </div>
          )}

          <form
            className="flex items-end gap-2 px-3 py-3"
            onSubmit={(event) => {
              event.preventDefault();
              void sendMessage();
            }}
          >
            <Button
              type="button"
              size="icon"
              variant="outline"
              aria-label="Draft correction manually"
              title="Draft correction manually"
              onClick={() => setManualDialogOpen(true)}
            >
              <FilePenLineIcon />
            </Button>
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void sendMessage();
                }
              }}
              rows={1}
              aria-label="Message case agent"
              placeholder="Ask a question or instruct a correction…"
              className="max-h-32 min-h-10 flex-1 resize-y rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
              disabled={streamState !== "connected" || sending}
            />
            <Button
              type="submit"
              size="icon"
              disabled={!draft.trim() || streamState !== "connected" || sending}
              aria-label="Send message"
              title="Send message"
            >
              {sending ? <Loader2Icon className="animate-spin" /> : <SendIcon />}
            </Button>
          </form>
          {chatError && (
            <div className="flex items-center gap-2 border-t border-border px-4 py-2" role="alert">
              <AlertTriangleIcon className="size-4 shrink-0 text-status-red" />
              <span className="flex-1 text-sm text-status-red">{chatError}</span>
              {streamState === "failed" && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => setConnectionAttempt((current) => current + 1)}
                >
                  Retry connection
                </Button>
              )}
            </div>
          )}
        </>
      )}
      <ManualCorrectionDialog
        open={manualDialogOpen || editingRecord !== null}
        onOpenChange={(open) => {
          if (!open) {
            setManualDialogOpen(false);
            setEditingRecord(null);
          }
        }}
        caseData={caseData}
        initialProposal={editingRecord?.proposal}
        onSubmit={(proposal) => {
          try {
            if (editingRecord) onEditProposal(editingRecord.id, proposal);
            else onStageProposal(proposal, "manual");
          } catch (error) {
            setChatError(
              error instanceof Error ? error.message : "The correction proposal is invalid.",
            );
          }
        }}
      />
    </section>
  );
}
