export function buildBrowserActionScript(
  action: "snapshot" | "click" | "type" | "locate" | "focus" | "scroll" | "select_option" | "check",
  args?: Record<string, unknown>,
): string;
