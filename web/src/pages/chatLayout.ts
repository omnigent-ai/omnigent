export const CHAT_COLUMN_WIDTH =
  "max-w-[var(--chat-column-width)] [--chat-column-width:48rem] min-[1921px]:[--chat-column-width:56rem] min-[2561px]:[--chat-column-width:clamp(64rem,40vw,100rem)]";

/** Stack composer popovers below the transparent z-30 header. */
export const COMPOSER_POPOVER_Z = "z-20";

/** Cap popovers at max-h-64 or the space above the composer on short screens. */
export const COMPOSER_POPOVER_MAX_H =
  "max-h-[min(--spacing(64),calc(100svh_-_var(--omnigent-header-height)_-_10.5rem))]";
