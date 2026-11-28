// What Live Edit does, apart from how it's shown: pick text in the page, preview edits
// there, put the page back on Discard, and send the edits. The app holds the text; the
// page only previews it, under the session's scope.
import { queueDesignRequest } from "./designOutbox";
import { buildLiveEditPrompt } from "./designPrompts";
import type { Hit, TextEdit } from "./designToolsStore";
import { asHit, SENDING } from "./toolActions";
import { isSessionEnded, type ToolSession } from "./toolSession";

/** The most text one edit holds (what main's page operations take). */
export const LIVE_EDIT_MAX_TEXT = 100_000;

/** An edit whose text differs from the page's original. */
export const changed = (edit: TextEdit) => edit.after !== edit.before;

const GONE = "That text isn't on the page any more.";
const RESTORE_FAILED = "Couldn't put the original text back. Try Discard again.";
// The page's refusals of a pick, by what it answered.
const REFUSED: Record<string, string> = {
  tooLong: "That text is too long to edit here.",
  tooMany: "That's as many edits as one Live Edit sends. Send these first, then edit more.",
  nested:
    "Live Edit changes plain text only, and that has more inside it (formatting, links or icons). Pick a part of it.",
  busy: "The last Live Edit is still finishing on that text. Try again in a moment.",
};

interface Picked extends Hit {
  handle: number;
  before: string;
  text: string;
  /** Where in `text` the click landed, or null (off the text). */
  caret: number | null;
}

/** A pick's reply, checked: it comes from the page. */
export function asPicked(value: unknown): Picked | null {
  const hit = asHit(value);
  if (!hit) return null;
  const { handle, before, text, caret } = value as Record<string, unknown>;
  if (typeof handle !== "number" || typeof before !== "string" || typeof text !== "string") {
    return null;
  }
  const inText =
    typeof caret === "number" && Number.isInteger(caret) && caret >= 0 && caret <= text.length;
  return { ...hit, handle, before, text, caret: inText ? caret : null };
}

const editCount = (count: number) => `${count} ${count === 1 ? "edit" : "edits"}`;

/**
 * One Live Edit session's work in the page. When the session ends, the page shows the
 * app's latest text for each edit, then forgets its state; after Discard it only forgets,
 * and when it ends during a Discard (the tool closed or switched), it puts the page back.
 */
export class LiveEditActions {
  // Each edit's latest text typed in, most recently typed last.
  private readonly typed = new Map<number, string>();
  // Picks asked for: only the latest's answer opens the editor.
  private picks = 0;
  // Typing queues at most one preview per edit; it shows the newest text when it runs.
  private readonly queuedText = new Map<number, string>();
  private discarding = false;
  private discarded = false;
  private readonly session: ToolSession;
  private readonly onNotice: (message: string) => void;

  constructor(session: ToolSession, onNotice: (message: string) => void) {
    this.session = session;
    this.onNotice = onNotice;
    session.atEnd(() => {
      const texts = [...this.typed];
      if (this.discarded) return (page) => page.liveEditForget(session.scope);
      if (this.discarding) return (page) => page.liveEditDiscard(session.scope);
      return (page) => page.liveEditSettle(session.scope, texts);
    });
  }

  /** Pick the text at `(x, y)` in the page's viewport; a miss closes the editor. */
  async pick(x: number, y: number): Promise<void> {
    const { session } = this;
    const pick = ++this.picks;
    session.edit({ editing: null });
    let answer: unknown = null;
    try {
      // After any preview still queued, so the page's text is the newest.
      answer = JSON.parse(await session.run((page) => page.liveEditPick(session.scope, x, y)));
    } catch (error) {
      if (isSessionEnded(error)) return;
    }
    if (pick !== this.picks) return;
    if (answer && typeof answer === "object") {
      const refusal = Object.keys(REFUSED).find((reason) => reason in answer);
      if (refusal) {
        this.onNotice(REFUSED[refusal]!);
        return;
      }
    }
    const picked = asPicked(answer);
    if (!picked) return;
    const { handle, before, text, element, rect, caret } = picked;
    session.edit((current) => {
      const existing = current.edits.find((edit) => edit.handle === handle);
      const others = current.edits.filter((edit) => edit.handle !== handle);
      const edit = existing
        ? { ...existing, rect }
        : { handle, element, rect, before, after: text };
      return { edits: [...others, edit], editing: handle, caret };
    });
  }

  /** The new text for edit `handle`, previewed on the page. */
  type(handle: number, input: string): void {
    const text = input.slice(0, LIVE_EDIT_MAX_TEXT);
    const edited = this.session.edit((current) => ({
      edits: current.edits.map((edit) =>
        edit.handle === handle ? { ...edit, after: text } : edit,
      ),
    }));
    if (edited) this.preview(handle, text);
  }

  private preview(handle: number, text: string): void {
    const { session } = this;
    this.typed.delete(handle);
    this.typed.set(handle, text);
    const queued = this.queuedText.has(handle);
    this.queuedText.set(handle, text);
    if (queued) return;
    void session
      .run(async (page) => {
        const latest = this.queuedText.get(handle);
        this.queuedText.delete(handle);
        if (latest === undefined) return "";
        const answer = await page.liveEditPreview(session.scope, handle, latest);
        // Its element left the page: there's nothing there to settle at the end.
        if (answer === "gone") this.typed.delete(handle);
        return answer;
      })
      .then(
        (answer) => answer === "gone",
        (error: unknown) => !isSessionEnded(error),
      )
      .then((gone) => {
        if (gone) this.onNotice(GONE);
      });
  }

  /** Put the page back and close once it is; stays open (saying so) to try again. */
  async discard(): Promise<void> {
    const { session } = this;
    if (!session.begin("discarding")) return;
    // Ending before it's done (the tool switched, say) still puts the page back.
    this.discarding = true;
    const restored = await session
      .run((page) => page.liveEditRestore(session.scope))
      .then(
        (answer) => answer !== "none",
        () => false,
      );
    if (restored) {
      this.discarded = true;
      session.close();
    } else if (session.settle()) {
      this.discarding = false;
      this.onNotice(RESTORE_FAILED);
    }
  }

  /** Hand the changed edits, as the app has them, to the outbox and close; the notice, or null. */
  send(url: string): string | null {
    const { session } = this;
    const current = session.state();
    if (current.tool !== "edit" || current.phase !== "editing") return null;
    const sending = current.edits.filter(changed);
    if (sending.length === 0) return null;
    queueDesignRequest({
      tabId: session.tabId,
      label: `Live Edit: ${editCount(sending.length)}`,
      tool: "edit",
      text: buildLiveEditPrompt(
        sending.map(({ element, before, after }) => ({ element, before, after })),
        url,
      ),
    });
    session.close();
    return SENDING;
  }
}

const liveEdits = new WeakMap<ToolSession, LiveEditActions>();

/** The Live Edit actions of `session`, made once per session. */
export function liveEditFor(
  session: ToolSession,
  onNotice: (message: string) => void,
): LiveEditActions {
  let actions = liveEdits.get(session);
  if (!actions) {
    actions = new LiveEditActions(session, onNotice);
    liveEdits.set(session, actions);
  }
  return actions;
}
