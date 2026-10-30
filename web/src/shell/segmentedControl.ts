// Shared look for the chat header's segmented controls (the Chat/Terminal
// toggle and the open-in-editor button): a sunken track holding 24px segments,
// the active one raised.

/** The sunken track the segments sit in. */
export const SEGMENTED_TRACK =
  "flex h-6 shrink-0 items-center gap-0.5 rounded-[6px] bg-muted p-0 dark:bg-[var(--segmented-trough-bg)]";

/** Shared segment shape; pair with an active or inactive state. */
export const SEGMENT_BASE = "h-6 rounded-[6px] border py-0";

export const SEGMENT_ACTIVE =
  "border-border bg-background text-foreground shadow-sm hover:bg-background dark:border-transparent dark:bg-[var(--segmented-active-bg)] dark:hover:bg-[var(--segmented-active-bg)]";

export const SEGMENT_INACTIVE =
  "border-transparent text-muted-foreground hover:bg-transparent hover:text-foreground dark:bg-[var(--segmented-inactive-bg)] dark:hover:bg-[var(--segmented-inactive-bg)]";
