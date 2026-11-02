import { type KeyboardEvent, useLayoutEffect, useRef, useState } from "react";
import type { RoomMessage } from "../../shared/roomMessages.ts";
import { type WorkerSession, officeApi } from "../api.ts";
import { useRuntimeOptions } from "../capabilities.ts";
import { DictationButton } from "../Dictation.tsx";
import { WORKER_STATE_LABELS, type WorkerState } from "../workers.ts";
import { type MentionTarget, insertMention, mentionAtCaret, mentionedTargets, suggestMentions } from "./mentions.ts";
import { workerName } from "./OfficeBoard.tsx";

const MAX_SUGGESTIONS = 8;
const MAX_INPUT_HEIGHT = 160;

interface MessageComposerProps {
  projectId: string;
  workers: WorkerSession[];
  /** Who an unaddressed message goes to; follows the worker chosen in the room. */
  recipientId: string | null;
  onRecipientChange: (sessionId: string) => void;
  stateOf: (worker: WorkerSession) => WorkerState;
  /** Who "@" can address: roles in an SDLC room, sessions otherwise. */
  targets: MentionTarget[];
  /** A message reached someone; the room's channel shows it. */
  onSent: (message: RoomMessage) => void;
}

/** A Slack-style message box: Enter sends, Shift+Enter adds a line, and @mentions address roles. */
export function MessageComposer({ projectId, workers, recipientId, onRecipientChange, stateOf, targets, onSent }: MessageComposerProps) {
  const inputRef = useRef<HTMLTextAreaElement>(null);
  // Where to put the caret after the box re-renders with text the composer changed itself.
  const pendingCaret = useRef<number | null>(null);
  const [draft, setDraft] = useState("");
  const [caret, setCaret] = useState(0);
  const [activeIndex, setActiveIndex] = useState(0);
  const [menuDismissed, setMenuDismissed] = useState(false);
  const [notice, setNotice] = useState<{ text: string; error: boolean } | null>(null);
  const [sending, setSending] = useState(false);
  const dictationAvailable = useRuntimeOptions()?.dictationAvailable ?? false;
  const recipient = workers.find((worker) => worker.id === recipientId) ?? null;
  const state = recipient ? stateOf(recipient) : null;

  const typing = mentionAtCaret(draft, caret);
  const suggestions = typing && !menuDismissed ? suggestMentions(typing.query, targets).slice(0, MAX_SUGGESTIONS) : [];
  const menuOpen = suggestions.length > 0;
  const mentioned = mentionedTargets(draft, targets);

  // Grow with the message, like Slack, up to a few lines before scrolling; place the caret
  // before the next keystroke arrives, so fast typing after a mention stays in order.
  useLayoutEffect(() => {
    const input = inputRef.current;
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, MAX_INPUT_HEIGHT)}px`;
    if (pendingCaret.current !== null) {
      input.focus();
      input.setSelectionRange(pendingCaret.current, pendingCaret.current);
      pendingCaret.current = null;
    }
  }, [draft]);

  const update = (text: string, nextCaret: number) => {
    setDraft(text);
    setCaret(nextCaret);
    setActiveIndex(0);
    setMenuDismissed(false);
  };

  const choose = (target: MentionTarget) => {
    if (!typing) return;
    const next = insertMention(draft, typing.start, caret, target.handle);
    pendingCaret.current = next.caret;
    update(next.text, next.caret);
  };

  const send = async () => {
    const text = draft.trim();
    if (!text || sending) return;
    const addressed = mentioned.length ? mentioned : null;
    const unstarted = (addressed ?? []).filter((target) => !target.sessionId);
    const recipients = addressed
      ? addressed.filter((target) => target.sessionId).map((target) => ({ sessionId: target.sessionId as string, name: `@${target.handle}` }))
      : recipient
        ? [{ sessionId: recipient.id, name: workerName(recipient) }]
        : [];
    if (recipients.length === 0) {
      setNotice({
        text: unstarted.length ? `${unstarted.map((target) => `@${target.handle}`).join(", ")} has not started yet; ask @PM to dispatch work.` : "Choose who to message.",
        error: true,
      });
      return;
    }
    setSending(true);
    setNotice(null);
    try {
      const result = await officeApi.sendRoomMessage(projectId, text, recipients);
      const problems = [...result.failures.map((failure) => `${failure.name}: ${failure.error}`), ...unstarted.map((target) => `@${target.handle} has not started`)];
      if (result.message) {
        onSent(result.message);
        update("", 0);
      }
      const delivered = result.message ? `Sent to ${result.message.recipients.map((entry) => entry.name).join(", ")}.` : "";
      setNotice({ text: [delivered, ...problems].filter(Boolean).join(" · "), error: problems.length > 0 });
    } catch (error) {
      setNotice({ text: `Not sent: ${(error as Error).message}`, error: true });
    } finally {
      setSending(false);
    }
  };

  const stop = async () => {
    if (!recipient) return;
    try {
      await officeApi.stop(recipient.id);
      setNotice({ text: `Stop sent to ${workerName(recipient)}.`, error: false });
    } catch (error) {
      setNotice({ text: `Stop failed: ${(error as Error).message}`, error: true });
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // Escape belongs to the box, not the room (which would otherwise leave the office).
    if (event.key === "Escape") {
      event.stopPropagation();
      if (menuOpen) setMenuDismissed(true);
      else event.currentTarget.blur();
      return;
    }
    if (menuOpen) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const step = event.key === "ArrowDown" ? 1 : -1;
        setActiveIndex((index) => (index + step + suggestions.length) % suggestions.length);
        return;
      }
      if (event.key === "Enter" || event.key === "Tab") {
        event.preventDefault();
        choose(suggestions[Math.min(activeIndex, suggestions.length - 1)]);
        return;
      }
    }
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void send();
    }
  };

  if (workers.length === 0 && targets.length === 0) return null;
  const placeholder = recipient ? `Message ${workerName(recipient)}` : "Message the room";
  const deliveryHint = mentioned.length
    ? `Sends to ${mentioned.map((target) => `@${target.handle}${target.sessionId ? "" : " (not started)"}`).join(", ")}`
    : recipient
      ? `Sends to ${workerName(recipient)} · @ to address a role`
      : "@ to address a role";

  return (
    <form
      className="slack-composer"
      aria-label="Message a worker"
      onSubmit={(event) => {
        event.preventDefault();
        void send();
      }}
    >
      <div className="slack-composer-box">
        {menuOpen ? (
          <ul className="mention-menu" id="mention-menu" role="listbox" aria-label="People to mention">
            {suggestions.map((target, index) => (
              <li
                key={target.handle}
                id={`mention-${target.handle}`}
                role="option"
                aria-selected={index === activeIndex}
                className="mention-option"
                // Keep focus in the box while choosing with the mouse.
                onMouseDown={(event) => {
                  event.preventDefault();
                  choose(target);
                }}
                onMouseEnter={() => setActiveIndex(index)}
              >
                <strong>@{target.handle}</strong>
                <span className="muted small">
                  {target.label !== target.handle ? target.label : ""}
                  {target.sessionId ? "" : " · not started"}
                </span>
              </li>
            ))}
          </ul>
        ) : null}
        <textarea
          ref={inputRef}
          rows={1}
          value={draft}
          placeholder={placeholder}
          aria-label={placeholder}
          role="combobox"
          aria-autocomplete="list"
          aria-expanded={menuOpen}
          aria-controls={menuOpen ? "mention-menu" : undefined}
          aria-activedescendant={menuOpen ? `mention-${suggestions[Math.min(activeIndex, suggestions.length - 1)].handle}` : undefined}
          onChange={(event) => {
            setNotice(null);
            update(event.target.value, event.target.selectionStart);
          }}
          onSelect={(event) => setCaret(event.currentTarget.selectionStart)}
          onKeyDown={onKeyDown}
        />
        <div className="slack-composer-toolbar">
          <label className="to-pill">
            To
            <select value={recipientId ?? ""} onChange={(event) => onRecipientChange(event.target.value)} aria-label="Default recipient">
              {recipient ? null : <option value="">Choose…</option>}
              {workers.map((worker) => (
                <option key={worker.id} value={worker.id}>
                  {workerName(worker)} · {WORKER_STATE_LABELS[stateOf(worker)]}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            className="composer-icon"
            aria-label="Mention someone"
            title="Mention someone"
            onClick={() => {
              const at = inputRef.current?.selectionStart ?? draft.length;
              const prefix = at > 0 && !/\s$/.test(draft.slice(0, at)) ? " @" : "@";
              pendingCaret.current = at + prefix.length;
              update(`${draft.slice(0, at)}${prefix}${draft.slice(at)}`, at + prefix.length);
            }}
          >
            @
          </button>
          <DictationButton available={dictationAvailable} onText={(text) => update(draft.trim() ? `${draft.trimEnd()} ${text}` : text, Number.MAX_SAFE_INTEGER)} />
          <span className="composer-spacer" />
          {state === "working" || state === "waiting_human" ? (
            <button type="button" className="composer-stop" onClick={() => void stop()} title={`Stop ${recipient ? workerName(recipient) : ""}'s turn`}>
              ■ Stop
            </button>
          ) : null}
          <button type="submit" className="composer-send" aria-label="Send" title="Send (Enter)" disabled={sending || !draft.trim()}>
            ➤
          </button>
        </div>
      </div>
      <p className={`slack-composer-foot small${notice?.error ? " error-text" : " muted"}`} role="status">
        {notice ? notice.text : `${deliveryHint} · Enter to send, Shift+Enter for a new line`}
      </p>
    </form>
  );
}
