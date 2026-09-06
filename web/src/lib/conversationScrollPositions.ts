export interface ConversationScrollPosition {
  scrollTop: number;
  anchorMessageId?: string;
  anchorOffset?: number;
  wasAtBottom?: boolean;
}

interface ActiveConversationScroller {
  conversationId: string;
  element: HTMLElement;
}

const STORAGE_KEY = "omnigent:conversation-scroll-positions:v2";

function loadPositions(): Map<string, ConversationScrollPosition> {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}") as Record<
      string,
      ConversationScrollPosition
    >;
    return new Map(
      Object.entries(parsed).filter(
        (entry): entry is [string, ConversationScrollPosition] =>
          typeof entry[1]?.scrollTop === "number",
      ),
    );
  } catch {
    return new Map();
  }
}

function persistPositions(positions: Map<string, ConversationScrollPosition>): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(Object.fromEntries(positions)));
  } catch {
    // Scroll restoration remains available in memory when storage is unavailable.
  }
}

const positions = loadPositions();
let activeScroller: ActiveConversationScroller | null = null;

function naturalTop(element: HTMLElement): number {
  let top = 0;
  let current: HTMLElement | null = element;
  while (current) {
    top += current.offsetTop;
    current = current.offsetParent as HTMLElement | null;
  }
  return top;
}

function readElementPosition(element: HTMLElement): ConversationScrollPosition {
  const wasAtBottom = element.scrollHeight - element.clientHeight - element.scrollTop <= 1;
  const messages = Array.from(
    element.querySelectorAll<HTMLElement>('[data-role="user"][data-user-message-id]'),
  );
  const viewportTop = naturalTop(element) + element.scrollTop;
  let anchor: HTMLElement | undefined;
  let anchorTop = Number.NEGATIVE_INFINITY;
  for (const message of messages) {
    const top = naturalTop(message);
    if (top <= viewportTop + 1 && top > anchorTop) {
      anchor = message;
      anchorTop = top;
    }
  }
  if (!anchor && messages.length > 0) {
    anchor = messages.reduce((nearest, message) =>
      naturalTop(message) < naturalTop(nearest) ? message : nearest,
    );
    anchorTop = naturalTop(anchor);
  }
  return anchor
    ? {
        scrollTop: element.scrollTop,
        anchorMessageId: anchor.dataset.userMessageId,
        anchorOffset: anchorTop - viewportTop,
        wasAtBottom,
      }
    : { scrollTop: element.scrollTop, wasAtBottom };
}

export function saveConversationScrollPosition(conversationId: string, element: HTMLElement): void {
  const position = readElementPosition(element);
  positions.set(conversationId, position);
  persistPositions(positions);
}

export function getConversationScrollPosition(
  conversationId: string,
): ConversationScrollPosition | undefined {
  return positions.get(conversationId);
}

export type ConversationScrollRestoreTarget =
  | { kind: "waiting"; reason: "anchor-missing" | "target-unreachable" }
  | { kind: "restore"; top: number };

/**
 * Returns a target only when the saved location exists in the current layout.
 * An incomplete transcript must not be replaced with a clamped pixel fallback:
 * that fallback is a temporary location and visibly jumps when layout catches up.
 */
export function getConversationScrollRestoreTarget(
  element: HTMLElement,
  position: ConversationScrollPosition,
): ConversationScrollRestoreTarget {
  const maxScrollTop = Math.max(0, element.scrollHeight - element.clientHeight);
  if (position.anchorMessageId !== undefined) {
    const anchor = Array.from(
      element.querySelectorAll<HTMLElement>('[data-role="user"][data-user-message-id]'),
    ).find((message) => message.dataset.userMessageId === position.anchorMessageId);
    if (!anchor || position.anchorOffset === undefined) {
      return { kind: "waiting", reason: "anchor-missing" };
    }
    const top = naturalTop(anchor) - naturalTop(element) - position.anchorOffset;
    return top >= 0 && top <= maxScrollTop + 1
      ? { kind: "restore", top }
      : { kind: "waiting", reason: "target-unreachable" };
  }

  const top = Math.max(0, position.scrollTop);
  return top <= maxScrollTop + 1
    ? { kind: "restore", top }
    : { kind: "waiting", reason: "target-unreachable" };
}

export function restoreConversationScrollPosition(
  element: HTMLElement,
  position: ConversationScrollPosition,
): boolean {
  const target = getConversationScrollRestoreTarget(element, position);
  if (target.kind === "waiting") return false;
  element.scrollTop = target.top;
  return true;
}

export function isConversationScrollPositionRestored(
  element: HTMLElement,
  position: ConversationScrollPosition,
): boolean {
  const target = getConversationScrollRestoreTarget(element, position);
  return target.kind === "restore" && Math.abs(element.scrollTop - target.top) <= 1;
}

export function registerActiveConversationScroller(
  conversationId: string,
  element: HTMLElement,
): () => void {
  const registration = { conversationId, element };
  activeScroller = registration;
  return () => {
    if (activeScroller === registration) {
      activeScroller = null;
    }
  };
}

/**
 * Capture immediately before chatStore changes its active conversation.
 *
 * The expected id prevents a URL/store render race from saving the shared DOM
 * element under a session whose messages have not rendered yet.
 */
export function captureActiveConversationScroll(expectedConversationId: string | null): void {
  if (!activeScroller || activeScroller.conversationId !== expectedConversationId) {
    return;
  }
  saveConversationScrollPosition(activeScroller.conversationId, activeScroller.element);
}
