// pointer-events-auto: the button is portaled to <body>, which a modal dialog (a project
// file opened from a ticket) sets to `pointer-events: none`; without it clicks fall through.
export const ADD_COMMENT_BUTTON_CLASS_NAME =
  "pointer-events-auto flex items-center gap-1.5 rounded-md border border-neutral-900 bg-neutral-900 px-2.5 py-1 text-sm font-medium text-white shadow-md hover:bg-neutral-800 dark:backdrop-blur-xl dark:backdrop-saturate-150 dark:border-border dark:bg-popover dark:text-foreground dark:hover:bg-secondary transition-colors";
