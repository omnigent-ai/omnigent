import { afterEach, describe, expect, it } from "vitest";

import { hideNativeChatTerminalBar } from "./nativeChatTerminalBar";

// The Chat/Terminal switcher lives in the header on every shell; the iOS
// shell's legacy bottom pill must be pushed hidden at boot (main.tsx, before
// the router mounts) so stale shell state — a page served before the pill's
// retirement — can never float it below the composer, on any route.

interface RecordedViewMode {
  mode: string;
  terminalEnabled: boolean;
  terminalStartingUp?: boolean;
  visible: boolean;
}

function installBridge(kind: string): RecordedViewMode[] {
  const calls: RecordedViewMode[] = [];
  (window as unknown as Record<string, unknown>).omnigentNative = {
    kind,
    setViewMode: (params: RecordedViewMode) => calls.push(params),
  };
  return calls;
}

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).omnigentNative;
});

describe("hideNativeChatTerminalBar", () => {
  it("pushes the legacy iOS bottom pill hidden", () => {
    const calls = installBridge("ios");

    hideNativeChatTerminalBar();

    expect(calls).toEqual([
      { mode: "chat", terminalEnabled: false, terminalStartingUp: false, visible: false },
    ]);
  });

  it("is a no-op outside the iOS shell", () => {
    const calls = installBridge("android");

    hideNativeChatTerminalBar();

    expect(calls).toEqual([]);
  });
});
