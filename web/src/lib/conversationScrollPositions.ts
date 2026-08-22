export interface ConversationScrollPosition {
  scrollTop: number;
  anchorMessageId?: string;
  anchorOffset?: number;
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
      }
    : { scrollTop: element.scrollTop };
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

function restorationTarget(
  element: HTMLElement,
  position: ConversationScrollPosition,
): { target: number; anchorFound: boolean } {
  const anchor =
    position.anchorMessageId === undefined
      ? undefined
      : Array.from(
          element.querySelectorAll<HTMLElement>('[data-role="user"][data-user-message-id]'),
        ).find((message) => message.dataset.userMessageId === position.anchorMessageId);
  const anchorTarget =
    anchor && position.anchorOffset !== undefined
      ? naturalTop(anchor) - naturalTop(element) - position.anchorOffset
      : undefined;
  const maxScrollTop = Math.max(0, element.scrollHeight - element.clientHeight);
  const target =
    anchorTarget !== undefined && anchorTarget >= 0 && anchorTarget <= maxScrollTop + 1
      ? anchorTarget
      : Math.min(Math.max(0, position.scrollTop), maxScrollTop);
  return { target, anchorFound: anchor !== undefined };
}

export function restoreConversationScrollPosition(
  element: HTMLElement,
  position: ConversationScrollPosition,
): boolean {
  const { target, anchorFound } = restorationTarget(element, position);
  element.scrollTop = target;
  return position.anchorMessageId === undefined || anchorFound;
}

export function isConversationScrollPositionRestored(
  element: HTMLElement,
  position: ConversationScrollPosition,
): boolean {
  const { target, anchorFound } = restorationTarget(element, position);
  if (position.anchorMessageId !== undefined && !anchorFound) return false;
  return Math.abs(element.scrollTop - target) <= 1;
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
export function captureActiveConversationScroll(
  expectedConversationId: string | null,
): void {
  if (!activeScroller || activeScroller.conversationId !== expectedConversationId) {
    return;
  }
  saveConversationScrollPosition(activeScroller.conversationId, activeScroller.element);
}
