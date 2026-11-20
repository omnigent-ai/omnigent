// The desktop shell's design tools bridge (web/electron/src/preload.js). Small and free of
// the tools' code, so the Browser pane can ask whether the shell has them without loading them.

/** Main ended a view's opening: its page went (`navigated`, `gone`). */
export interface DesignToolsEvent {
  viewId: string;
  type: "ended";
  reason?: "navigated" | "gone";
  /** The opening it ended. */
  session?: string;
}

export interface Answer {
  ok: boolean;
  error?: string;
  /** `start`'s opening, which every later request names. */
  session?: string;
  /** `start`'s frame name, which the opening's overlay opens as. */
  frame?: string;
  /** A page operation's answer (the page's own data: untrusted). */
  result?: string;
  png?: string;
  width?: number;
  height?: number;
  /** A capture's page viewport at that moment, in page CSS pixels. */
  viewport?: { width: number; height: number };
}

export interface DesignToolsBridge {
  browserDesignTools: (
    args: { conversationId: string; op: string } & Record<string, unknown>,
  ) => Promise<Answer>;
  onBrowserDesignToolsEvent: (callback: (event: DesignToolsEvent) => void) => () => void;
}

/** The desktop shell's design tools, or null (the web app, or an older shell). */
export function desktopDesignTools(): DesignToolsBridge | null {
  if (typeof window === "undefined") return null;
  const bridge = (window as unknown as { omnigentDesktop?: Partial<DesignToolsBridge> })
    .omnigentDesktop;
  return bridge?.browserDesignTools && bridge.onBrowserDesignToolsEvent
    ? (bridge as DesignToolsBridge)
    : null;
}
