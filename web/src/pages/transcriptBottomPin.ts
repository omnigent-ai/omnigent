/** Whether the transcript reader is pinned to the bottom, and where that pin was last held. */
export interface TranscriptBottomPin {
  pinned: boolean;
  /** The highest scrollTop seen since the reader was last at the bottom. */
  anchorScrollTop: number;
}

export interface TranscriptScrollPosition {
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
}

/**
 * Track the bottom pin across one scroll event.
 *
 * Within 1px of the bottom always pins. Otherwise only upward movement leaves the
 * bottom: a scroll read while the viewport is briefly short (the composer grew but
 * its overlay hasn't re-measured) keeps the pin unless scrollTop dropped more than
 * 1px below the anchor in total, so repeated small upward steps still escape.
 */
export function nextTranscriptBottomPin(
  previous: TranscriptBottomPin,
  { scrollTop, scrollHeight, clientHeight }: TranscriptScrollPosition,
): TranscriptBottomPin {
  if (scrollHeight - clientHeight - scrollTop <= 1) {
    return { pinned: true, anchorScrollTop: scrollTop };
  }
  if (previous.pinned && scrollTop >= previous.anchorScrollTop - 1) {
    return { pinned: true, anchorScrollTop: Math.max(previous.anchorScrollTop, scrollTop) };
  }
  return { pinned: false, anchorScrollTop: scrollTop };
}
