// What each design tool does, apart from how it's shown: Annotate hit-tests and picks an
// element, then sends a request; Draw sends a screenshot with its marks. The layers call
// these with points in the page's viewport; page work goes through the tool's session.
import { buildDesignModePrompt, type DesignModeElement } from "@/lib/designModePrompt";
import { designPlatform } from "./designPlatform";
import { queueDesignRequest } from "./designOutbox";
import { buildDrawPrompt } from "./designPrompts";
import type { CssRect, Hit } from "./designToolsStore";
import type { ToolSession } from "./toolSession";

/** The notice once a tool has handed its request to the outbox. */
export const SENDING = "Sending to agent…";
/** Annotate's notice when its request goes without the element's screenshot. */
export const SENDING_WITHOUT_SCREENSHOT =
  "Sending to agent without the element's screenshot, which couldn't be taken.";

/** Whether a Send's notice says its request was handed to the outbox. */
export const isSendingNotice = (notice: string | null) =>
  notice === SENDING || notice === SENDING_WITHOUT_SCREENSHOT;

export function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** A short name for an element: its tag and the start of its text. */
export function elementTag(element: DesignModeElement): string {
  const tag = element.tag || "element";
  const text = (element.text ?? "").replace(/\s+/g, " ").trim();
  return text ? `${tag} "${text.length > 28 ? `${text.slice(0, 27)}…` : text}"` : tag;
}

const isFiniteNumber = (value: unknown) => typeof value === "number" && Number.isFinite(value);

/** A hit test's reply, checked: it comes from the page. */
export function asHit(value: unknown): Hit | null {
  if (!value || typeof value !== "object") return null;
  const { element, rect } = value as { element?: unknown; rect?: Partial<CssRect> };
  if (!element || typeof element !== "object" || !rect) return null;
  if (![rect.x, rect.y, rect.width, rect.height].every(isFiniteNumber)) return null;
  // Keep only the string fields the prompt and the tag read.
  const fields = Object.entries(element).filter(([, field]) => typeof field === "string");
  const { key } = value as { key?: unknown };
  return {
    element: Object.fromEntries(fields) as DesignModeElement,
    rect: rect as CssRect,
    ...(typeof key === "string" ? { key } : {}),
  };
}

/** The line Annotate's block gets when it goes without the element's screenshot. */
const NO_SCREENSHOT_LINE = "Screenshot: none attached (it couldn't be taken)";

/** The longest Annotate waits for its element's screenshot before sending without it. */
const ELEMENT_SHOT_MS = 5000;

// ── Annotate ─────────────────────────────────────────────

/** The element at `(x, y)` in the page's viewport, or null. */
export async function hitAt(session: ToolSession, x: number, y: number): Promise<Hit | null> {
  try {
    return asHit(JSON.parse(await session.run((page) => page.hitTest(x, y))));
  } catch {
    return null;
  }
}

/** Pick the element at `(x, y)`: what's under the click itself, as hover replies can lag. */
export async function pickElement(session: ToolSession, x: number, y: number): Promise<void> {
  if (session.state().picked) return;
  const hit = await hitAt(session, x, y);
  // Another click's reply may have picked one meanwhile: keep it, with what's typed for it.
  if (!hit || session.state().picked) return;
  session.edit({ picked: { ...hit, at: { x, y } } });
}

const ELEMENT_FIELDS = ["tag", "id", "classes", "text", "testId", "ariaLabel", "role"] as const;

/**
 * Where the picked element is now (the page can have moved it): the element under the
 * point it was picked at, if that's still the one picked; else null.
 */
async function pickedRectNow(session: ToolSession, picked: Hit): Promise<CssRect | null> {
  if (!picked.at) return null;
  const now = await hitAt(session, picked.at.x, picked.at.y);
  if (!now) return null;
  const same =
    (now.key ?? "") === (picked.key ?? "") &&
    ELEMENT_FIELDS.every((field) => (now.element[field] ?? "") === (picked.element[field] ?? ""));
  return same ? now.rect : null;
}

/**
 * Hand Annotate's request, with a screenshot of the element where it can still be found,
 * to the outbox and close; the notice to show (saying when it goes without one), or null.
 */
export async function sendAnnotation(session: ToolSession): Promise<string | null> {
  const current = session.state();
  if (current.tool !== "annotate" || !current.picked || !current.request.trim()) return null;
  const { picked, request } = current;
  // Frozen while it's taken, so what's sent is what was asked.
  if (!session.begin("preparing")) return null;
  const rect = await pickedRectNow(session, picked);
  if (session.isEnded) return null;
  const files: File[] = [];
  try {
    const shoot = designPlatform(session.tabId).capture.element;
    if (rect) files.push(await withDeadline(shoot(session.tabId, rect), ELEMENT_SHOT_MS));
  } catch {
    // Sent without it (it failed, or took too long).
  }
  if (session.isEnded) return null;
  const prompt = buildDesignModePrompt(picked.element, request);
  queueDesignRequest({
    tabId: session.tabId,
    label: `Annotate ${elementTag(picked.element)}`,
    tool: "annotate",
    // Without one, the block ends by saying so (its fields can't hold a line break).
    text: files.length > 0 ? prompt : prompt.replace(/\n---$/, `\n${NO_SCREENSHOT_LINE}\n---`),
    ...(files.length > 0 ? { files } : {}),
  });
  session.close();
  return files.length > 0 ? SENDING : SENDING_WITHOUT_SCREENSHOT;
}

/** `promise`, or a rejection once `ms` have passed without it. */
function withDeadline<T>(promise: Promise<T>, ms: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const late = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => reject(new Error("took too long")), ms);
  });
  return Promise.race([promise, late]).finally(() => clearTimeout(timer));
}

// ── Draw ─────────────────────────────────────────────────

const marks = (count: number) => `${count} ${count === 1 ? "mark" : "marks"}`;

/**
 * Freeze the drawing, screenshot the page with its marks, hand both to the outbox and
 * close. The notice to show, or null (nothing to send, or closed while capturing).
 */
export async function sendDrawing(session: ToolSession, url: string): Promise<string | null> {
  if (session.state().shapes.length === 0 || !session.begin("preparing")) return null;
  // Frozen from here: these are what the screenshot shows, never a stroke still in progress.
  const { note, shapes } = session.state();
  let file: File;
  try {
    file = await designPlatform(session.tabId).capture.screenshot(session.tabId, shapes);
  } catch (error) {
    return session.settle() ? errorText(error) : null;
  }
  // Closed while capturing: send nothing.
  if (session.isEnded) return null;
  queueDesignRequest({
    tabId: session.tabId,
    label: `Draw: ${note.trim() || marks(shapes.length)}`,
    tool: "draw",
    text: buildDrawPrompt(note, url),
    files: [file],
  });
  session.close();
  return SENDING;
}
