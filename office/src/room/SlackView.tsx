import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { roleAgentName, roleTitle } from "../../shared/sdlcTheme.ts";
import { type ActivityEntry, repliesTo, toActivity } from "../../shared/sessionActivity.ts";
import type { RoomMessage } from "../../shared/roomMessages.ts";
import type { BranchView, WorkflowEvent } from "../../shared/workflowTypes.ts";
import { type SessionItem, type WorkerSession, officeApi } from "../api.ts";
import { PersonSprite } from "../PersonSprite.tsx";
import { HUMAN_PALETTE, type Palette, workerPalette } from "../sprites.ts";
import { WORKER_STATE_LABELS, type WorkerState } from "../workers.ts";
import { type TemporaryWorker, workerName } from "./OfficeBoard.tsx";

const DM_POLL_MS = 3_000;
// The channel shows replies to this many of the PO's latest messages.
const REPLY_MESSAGE_WINDOW = 20;
// Within this distance of the bottom, the reader is following the conversation.
const FOLLOW_THRESHOLD_PX = 80;

/** Bring the newest message into view; a long way back jumps rather than animating. */
function scrollToNewest(list: HTMLElement, smooth: boolean): void {
  const distance = list.scrollHeight - list.scrollTop - list.clientHeight;
  list.scrollTo({ top: list.scrollHeight, behavior: smooth && distance < list.clientHeight * 2 ? "smooth" : "auto" });
}
// The project's own channel, named after the Omnigent project.
const PROJECT_CHANNEL = "project";

interface SlackViewProps {
  roomName: string;
  /** What the PO sent from this room; the project channel shows it. */
  roomMessages: RoomMessage[];
  view: BranchView | null;
  workers: WorkerSession[];
  stateOf: (worker: WorkerSession) => WorkerState;
  /** The DM shown follows the message box's recipient, and choosing a DM sets it. */
  recipientId: string | null;
  onRecipientChange: (sessionId: string) => void;
  /** Live temporary workers, which can be closed from their DM. */
  temporaries: Map<string, TemporaryWorker>;
  onCloseTemporary: (sessionIds: string[]) => Promise<string | null>;
}

interface ChatMessage {
  id: string;
  author: string;
  palette: Palette;
  at: number | null;
  text: string;
  kind: "message" | "tool" | "error" | "office";
  /** Who a PO message went to, e.g. "@PM, @QA". */
  to?: string;
}

function timeOf(epochSeconds: number | null): string {
  return epochSeconds ? new Date(epochSeconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
}

function eventMessage(event: WorkflowEvent): ChatMessage {
  const role = event.actor;
  return {
    id: event.id,
    author: role === "office" ? "Office" : role === "po" ? "You (PO)" : roleTitle(role),
    palette: role === "po" ? HUMAN_PALETTE : role === "office" ? workerPalette("office") : workerPalette(roleAgentName(role)),
    at: event.at,
    text: event.summary,
    kind: role === "office" ? "office" : "message",
  };
}

function poMessage(message: RoomMessage): ChatMessage {
  return {
    id: message.id,
    author: "You",
    palette: HUMAN_PALETTE,
    at: message.at,
    text: message.text,
    kind: "message",
    to: message.recipients.map((recipient) => recipient.name).join(", "),
  };
}

function dmMessage(entry: ActivityEntry, worker: WorkerSession): ChatMessage {
  const author = entry.kind === "you" ? "You" : entry.kind === "office" ? "Office (relayed)" : workerName(worker);
  return {
    id: entry.id,
    author,
    palette: entry.kind === "you" ? HUMAN_PALETTE : workerPalette(worker.agent_name ?? worker.id),
    at: entry.at ?? null,
    text: entry.text,
    kind: entry.kind === "tool" ? "tool" : entry.kind === "error" ? "error" : entry.kind === "office" ? "office" : "message",
  };
}

/** A worker's recent conversation, oldest first like a chat, refreshed while shown. */
function useConversation(sessionId: string | null): SessionItem[] | null {
  const [items, setItems] = useState<SessionItem[] | null>(null);
  useEffect(() => {
    setItems(null);
    if (!sessionId) return;
    let cancelled = false;
    const load = () =>
      officeApi
        .items(sessionId)
        .then((next) => !cancelled && setItems([...next].reverse()))
        .catch(() => undefined);
    void load();
    const timer = window.setInterval(load, DM_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [sessionId]);
  return items;
}

/** Each recipient's recent conversation, oldest first, refreshed while the channel is shown. */
function useRecipientConversations(sessionIds: string[]): Map<string, SessionItem[]> {
  const [conversations, setConversations] = useState<Map<string, SessionItem[]>>(new Map());
  const key = sessionIds.join(",");
  useEffect(() => {
    const ids = key ? key.split(",") : [];
    if (ids.length === 0) {
      setConversations(new Map());
      return;
    }
    let cancelled = false;
    const load = () =>
      Promise.all(ids.map((id) => officeApi.items(id).then((items) => [id, [...items].reverse()] as const, () => null))).then((loaded) => {
        if (!cancelled) setConversations(new Map(loaded.filter((entry) => entry !== null)));
      });
    void load();
    const timer = window.setInterval(load, DM_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [key]);
  return conversations;
}

/** Recipients' answers to the PO's room messages, shown in the channel as their replies. */
function replyMessages(roomMessages: RoomMessage[], conversations: Map<string, SessionItem[]>, workers: WorkerSession[]): ChatMessage[] {
  return [...conversations].flatMap(([sessionId, items]) => {
    const sent = roomMessages.filter((message) => message.recipients.some((recipient) => recipient.sessionId === sessionId));
    const worker = workers.find((candidate) => candidate.id === sessionId);
    const author = worker ? workerName(worker) : (sent.at(-1)?.recipients.find((recipient) => recipient.sessionId === sessionId)?.name ?? "Agent");
    return repliesTo(new Set(sent.map((message) => message.text)), items).map((entry) => ({
      id: entry.id,
      author,
      palette: workerPalette(worker?.agent_name ?? sessionId),
      at: entry.at ?? null,
      text: entry.text,
      kind: "message" as const,
      to: "You",
    }));
  });
}

/** The room as a Slack-style chat: the event feed as a channel and each worker as a DM. */
export function SlackView({
  roomName,
  roomMessages,
  view,
  workers,
  stateOf,
  recipientId,
  onRecipientChange,
  temporaries,
  onCloseTemporary,
}: SlackViewProps) {
  const [confirmingClose, setConfirmingClose] = useState<string | null>(null);
  const [closeError, setCloseError] = useState<string | null>(null);
  const [channel, setChannel] = useState<string>(PROJECT_CHANNEL);
  const followedRecipient = useRef(recipientId);
  const listRef = useRef<HTMLOListElement>(null);
  const following = useRef(true);
  const scrolledChannel = useRef<string | null>(null);
  const lastSeenId = useRef<string | null>(null);
  const [unseen, setUnseen] = useState(false);
  const dm = channel === PROJECT_CHANNEL ? null : (workers.find((worker) => worker.id === channel) ?? null);
  const items = useConversation(dm?.id ?? null);
  // Only recent messages' recipients, since each session's conversation is fetched.
  const recentRecipients =
    dm === null ? [...new Set(roomMessages.slice(-REPLY_MESSAGE_WINDOW).flatMap((message) => message.recipients.map((recipient) => recipient.sessionId)))].sort() : [];
  const conversations = useRecipientConversations(recentRecipients);

  // Open on the project channel, then follow the worker chosen elsewhere in the room.
  // The room filling in its default recipient after loading is not a choice.
  useEffect(() => {
    const previous = followedRecipient.current;
    if (recipientId && previous && recipientId !== previous) setChannel(recipientId);
    followedRecipient.current = recipientId;
  }, [recipientId]);

  const messages: ChatMessage[] =
    dm === null
      ? [
          ...[...(view?.events ?? [])].reverse().map(eventMessage),
          ...roomMessages.map(poMessage),
          ...replyMessages(roomMessages, conversations, workers),
        ].sort((a, b) => (a.at ?? 0) - (b.at ?? 0))
      : toActivity(items ?? []).map((entry) => dmMessage(entry, dm));

  // Scroll like Slack when a message arrives: always for your own, and for others' only
  // while you are reading at the bottom. Lists are capped, so watch the newest message's
  // id rather than how many there are.
  const newest = messages.at(-1);
  useLayoutEffect(() => {
    const list = listRef.current;
    if (!list || !newest) return;
    const opened = scrolledChannel.current !== channel;
    const arrived = newest.id !== lastSeenId.current;
    scrolledChannel.current = channel;
    lastSeenId.current = newest.id;
    if (opened || (arrived && (following.current || newest.author === "You"))) {
      scrollToNewest(list, !opened);
      following.current = true;
      setUnseen(false);
    } else if (arrived) {
      setUnseen(true);
    }
  }, [newest, channel]);

  const jumpToNewest = () => {
    if (listRef.current) scrollToNewest(listRef.current, true);
    following.current = true;
    setUnseen(false);
  };

  const open = (id: string) => {
    setChannel(id);
    if (id !== PROJECT_CHANNEL) onRecipientChange(id);
  };

  return (
    <div className="slack-view">
      <nav className="slack-sidebar" aria-label="Channels and direct messages">
        <div className="slack-workspace">Omnigent</div>
        <div className="slack-section">Channels</div>
        <button type="button" className="slack-item" aria-current={channel === PROJECT_CHANNEL || undefined} onClick={() => open(PROJECT_CHANNEL)}>
          # {roomName}
        </button>
        <div className="slack-section">Direct messages</div>
        {workers.map((worker) => {
          const state = stateOf(worker);
          const temporary = temporaries.get(worker.id);
          if (confirmingClose === worker.id && temporary) {
            return (
              <div key={worker.id} className="slack-close-confirm" role="group" aria-label={`Close ${workerName(worker)}?`}>
                <span>{temporary.finished ? "Close this worker?" : "Task not finished. Close?"}</span>
                <button
                  type="button"
                  onClick={() => {
                    setConfirmingClose(null);
                    if (channel === worker.id) setChannel(PROJECT_CHANNEL);
                    void onCloseTemporary([worker.id]).then(setCloseError);
                  }}
                >
                  Close
                </button>
                <button type="button" onClick={() => setConfirmingClose(null)}>
                  Cancel
                </button>
              </div>
            );
          }
          return (
            <div key={worker.id} className="slack-item-row">
              <button type="button" className="slack-item" aria-current={channel === worker.id || undefined} onClick={() => open(worker.id)}>
                <span className={`state-dot state-${state}`} aria-hidden="true" />
                <span className="slack-item-name">{workerName(worker)}</span>
              </button>
              {temporary ? (
                <button
                  type="button"
                  className="slack-item-close"
                  aria-label={`Close ${workerName(worker)}`}
                  title="Close this temporary worker"
                  onClick={() => setConfirmingClose(worker.id)}
                >
                  ×
                </button>
              ) : null}
            </div>
          );
        })}
        {closeError ? <p className="slack-close-error small">{closeError}</p> : null}
      </nav>
      <section className="slack-main" aria-label={dm ? `Conversation with ${workerName(dm)}` : `# ${roomName}`}>
        <header className="slack-header">
          <strong>{dm ? workerName(dm) : `# ${roomName}`}</strong>
          <span className="muted small">
            {dm
              ? WORKER_STATE_LABELS[stateOf(dm)]
              : view
                ? "Your messages and the room's events: tasks, gates, decisions, and handoffs"
                : "Your messages to this room's sessions"}
          </span>
        </header>
        <ol
          className="slack-messages"
          ref={listRef}
          aria-live="polite"
          onScroll={(event) => {
            const list = event.currentTarget;
            following.current = list.scrollHeight - list.scrollTop - list.clientHeight < FOLLOW_THRESHOLD_PX;
            if (following.current) setUnseen(false);
          }}
        >
          {dm && items === null ? <li className="muted small slack-empty">Loading…</li> : null}
          {messages.length === 0 && (dm === null || items !== null) ? <li className="muted small slack-empty">No messages yet.</li> : null}
          {messages.map((message) => (
            <li key={message.id} className={`slack-message slack-message-${message.kind}`}>
              <PersonSprite palette={message.palette} pixel={3} />
              <div className="slack-message-body">
                <div className="slack-message-meta">
                  <strong>{message.author}</strong>
                  {message.to ? <span className="slack-to"> → {message.to}</span> : null} <span className="muted small">{timeOf(message.at)}</span>
                </div>
                <div className="slack-message-text">{message.text}</div>
              </div>
            </li>
          ))}
        </ol>
        {unseen ? (
          <button type="button" className="slack-new-messages" onClick={jumpToNewest}>
            New messages ↓
          </button>
        ) : null}

      </section>
    </div>
  );
}
