// Unit tests for the WebSocket attach path builder and the closed
// bridge overlay.
//
// The production TerminalView component creates xterm + a real
// WebSocket bridge. These tests mock that bridge and drive its state
// callback directly, while still pinning the pure URL builder contract
// the server cares about.

import type * as TerminalSessionModule from "./TerminalSession";

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { act, useMemo } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Toaster } from "@/components/ui/sonner";
import { FileViewerContext, type OpenFileOptions } from "@/shell/FileViewerContext";
import { toast } from "sonner";
import * as host from "@/lib/host";
import {
  readTerminalClipboardPreference,
  writeTerminalClipboardPreference,
} from "@/lib/terminalClipboardPreferences";
import type { ConnectionState } from "./TerminalSession";
import {
  TerminalView,
  RECONNECT_BACKOFF_MS,
  RECONNECT_STABLE_MS,
  buildAttachPath,
} from "./TerminalView";

const clipboardMock = vi.hoisted(() => ({
  copyText: vi.fn<(text: string) => Promise<void>>(),
}));

vi.mock("@/lib/clipboard", () => ({ copyText: clipboardMock.copyText }));

function FileViewerHarness({
  openFile,
}: {
  openFile: (path: string, options?: OpenFileOptions) => void;
}) {
  const value = useMemo(
    () => ({
      openFile,
      openGithubTab: () => {},
      isChangedPath: () => false,
      conversationId: "conv_abc",
      workspaceRoot: "/home/u/ws",
      workspaceHome: "/home/u",
    }),
    [openFile],
  );
  return (
    <FileViewerContext.Provider value={value}>
      <TerminalView sessionId="conv_abc" terminalId="terminal_codex_main" />
    </FileViewerContext.Provider>
  );
}

const terminalSessionMock = vi.hoisted(() => ({
  instances: [] as {
    url: string;
    container: HTMLDivElement;
    clipboardEnabled: boolean;
    adaptCodexPalette: boolean;
    onClipboardRequest?: (text: string, copyEvent?: ClipboardEvent) => void;
    onFileLink?: (uri: string) => boolean;
    onState: (state: ConnectionState) => void;
    dispose: ReturnType<typeof vi.fn>;
    setTheme: ReturnType<typeof vi.fn>;
    setClipboardEnabled: ReturnType<typeof vi.fn>;
    focus: ReturnType<typeof vi.fn>;
  }[],
}));

vi.mock("./TerminalSession", async (importOriginal) => ({
  // Keep the real module (isUnexpectedTerminalClose and friends) —
  // only the session class itself is replaced.
  ...(await importOriginal<typeof TerminalSessionModule>()),
  TerminalSession: class {
    dispose = vi.fn();
    setTheme = vi.fn();
    setClipboardEnabled = vi.fn();
    focus = vi.fn();

    constructor(
      container: HTMLDivElement,
      url: string,
      onState: (state: ConnectionState) => void,
      _isDark?: boolean,
      _onActivity?: () => void,
      _onInput?: () => void,
      clipboardEnabled = true,
      onClipboardRequest?: (text: string, copyEvent?: ClipboardEvent) => void,
      _focusOnConnect = true,
      adaptCodexPalette = false,
      onFileLink?: (uri: string) => boolean,
    ) {
      terminalSessionMock.instances.push({
        url,
        container,
        clipboardEnabled,
        adaptCodexPalette,
        onClipboardRequest,
        onFileLink,
        onState,
        dispose: this.dispose,
        setTheme: this.setTheme,
        setClipboardEnabled: this.setClipboardEnabled,
        focus: this.focus,
      });
    }
  },
}));

beforeEach(() => {
  localStorage.clear();
  act(() => toast.dismiss());
  render(<Toaster visibleToasts={100} />);
  terminalSessionMock.instances = [];
  clipboardMock.copyText.mockReset().mockResolvedValue(undefined);
});

afterEach(() => {
  act(() => toast.dismiss());
  cleanup();
  vi.restoreAllMocks();
});

describe("buildAttachPath", () => {
  it("addresses the terminal by resource id under /v1/sessions/.../resources/terminals", () => {
    expect(buildAttachPath("conv_abc", "terminal_bash_s1", false)).toBe(
      "/v1/sessions/conv_abc/resources/terminals/terminal_bash_s1/attach",
    );
  });

  it("omits ?read_only when the flag is false (common case)", () => {
    expect(buildAttachPath("conv_abc", "terminal_bash_s1", false).includes("?")).toBe(false);
  });

  it("appends ?read_only=true when requested", () => {
    expect(buildAttachPath("conv_abc", "terminal_bash_s1", true)).toBe(
      "/v1/sessions/conv_abc/resources/terminals/terminal_bash_s1/attach?read_only=true",
    );
  });

  it("appends ?omnigent_slice_key=host_id for host-sharded routing", () => {
    expect(buildAttachPath("conv_abc", "terminal_bash_s1", false, "host_123")).toBe(
      "/v1/sessions/conv_abc/resources/terminals/terminal_bash_s1/attach?omnigent_slice_key=host_123",
    );
  });

  it("combines read_only and slice-key params", () => {
    const path = buildAttachPath("conv_abc", "terminal_bash_s1", true, "host_789");
    expect(path).toContain("read_only=true");
    expect(path).toContain("omnigent_slice_key=host_789");
  });

  it("omits ?omnigent_slice_key when no hostId is provided", () => {
    const path = buildAttachPath("conv_abc", "terminal_bash_s1", false);
    expect(path.includes("omnigent_slice_key")).toBe(false);
  });

  it("url-encodes the session and terminal ids", () => {
    const path = buildAttachPath("conv with space", "terminal/odd:id", false);
    expect(path).toContain("/v1/sessions/conv%20with%20space/");
    expect(path).toContain("/resources/terminals/terminal%2Fodd%3Aid/attach");
  });

  it("does not embed user-facing display names in the path", () => {
    // Resource-addressed routing was chosen specifically to keep
    // user-derived names (which can contain slashes / reserved
    // chars) out of the path. Pin that contract.
    const path = buildAttachPath("conv_abc", "terminal_bash_s1", false);
    expect(path).not.toContain("terminal_name=");
    expect(path).not.toContain("session_key=");
  });

  it("emits a path that starts at root (not a relative URL)", () => {
    // Caller composes the full URL with window.location.host;
    // a leading slash is required for that concatenation to be
    // correct against any page origin.
    expect(buildAttachPath("conv_abc", "terminal_bash_s1", false).startsWith("/")).toBe(true);
  });
});

describe("control-mode terminal", () => {
  it.each([
    ["terminal_codex_main", true],
    ["terminal_claude_main", false],
    ["terminal_bash_s1", false],
  ])("adapts cached colors only for the Codex pane: %s", async (terminalId, expected) => {
    render(<TerminalView sessionId="conv_abc" terminalId={terminalId} readOnly />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    expect(terminalSessionMock.instances[0].adaptCodexPalette).toBe(expected);
  });

  it("disables clipboard bridging for read-only attaches", async () => {
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" readOnly />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    expect(terminalSessionMock.instances[0].clipboardEnabled).toBe(false);
  });

  it("does not render a legacy selection hint", async () => {
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    expect(screen.queryByTestId("terminal-selection-hint")).toBeNull();
  });

  it("opens an OSC 8 workspace file citation in the FileViewer", async () => {
    const openFile = vi.fn();
    render(<FileViewerHarness openFile={openFile} />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));

    expect(
      terminalSessionMock.instances[0].onFileLink?.("file:///home/u/ws/src/app.ts#L42-L50"),
    ).toBe(true);
    expect(openFile).toHaveBeenCalledWith("src/app.ts", { line: 42 });
  });
});

describe("terminal clipboard", () => {
  async function renderClipboardView() {
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    return terminalSessionMock.instances[0];
  }

  async function requestClipboard(
    text: string,
    terminalIndex = 0,
    copyEvent?: ClipboardEvent,
  ): Promise<void> {
    act(() => terminalSessionMock.instances[terminalIndex].onClipboardRequest?.(text, copyEvent));
    // Sonner publishes toast updates on the next event-loop turn.
    await act(async () => {
      await new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
      });
    });
  }

  async function copySelection(text: string, terminalIndex = 0) {
    const setData = vi.fn();
    const event = new Event("copy", { bubbles: true, cancelable: true }) as ClipboardEvent;
    Object.defineProperty(event, "clipboardData", { value: { setData } });
    await requestClipboard(text, terminalIndex, event);
    return { event, setData };
  }

  function visibleClipboardConsent(): HTMLElement | null {
    return screen.queryByTestId("terminal-clipboard-consent");
  }

  function clickConsentButton(name: string): void {
    const prompt = visibleClipboardConsent();
    expect(prompt).not.toBeNull();
    fireEvent.click(within(prompt!).getByRole("button", { name }));
  }

  function uncheckRemember(): void {
    fireEvent.click(screen.getByRole("checkbox", { name: "Remember my choice" }));
  }

  async function allowForThisTerminal(): Promise<void> {
    uncheckRemember();
    clickConsentButton("Allow for this session");
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
  }

  async function requestClipboardConsent(text: string, terminalIndex = 0): Promise<HTMLElement> {
    await requestClipboard(text, terminalIndex);
    await waitFor(() => expect(visibleClipboardConsent()).not.toBeNull());
    return visibleClipboardConsent()!;
  }

  it("requires consent before the first terminal clipboard write", async () => {
    const terminal = await renderClipboardView();

    const prompt = await requestClipboardConsent("copied text");

    expect(clipboardMock.copyText).not.toHaveBeenCalled();
    expect(prompt).not.toBeNull();
    expect(screen.getAllByText("Allow copying from terminals?").length).toBeGreaterThan(0);
    expect(prompt).toHaveTextContent("Your selection hasn’t been copied yet.");
    expect(prompt).toHaveTextContent(
      "Allowing copying also lets terminal programs silently replace your clipboard with text or commands you didn’t intend to paste.",
    );
    expect(within(prompt).getByRole("checkbox", { name: "Remember my choice" })).toBeChecked();
    expect(prompt).toHaveTextContent("Change this in Settings → General.");
    expect(prompt).not.toHaveTextContent("On this server, in this browser or app.");
    clickConsentButton("Copy once");
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledWith("copied text"));
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
    expect(terminal.focus).toHaveBeenCalled();
    expect(readTerminalClipboardPreference()).toBe("ask");

    await requestClipboardConsent("next text");
    expect(visibleClipboardConsent()).toBeInTheDocument();
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(1);
  });

  it("asks before copying browser selections and Copy once does not grant future copies", async () => {
    await renderClipboardView();
    const first = await copySelection("browser selection");

    expect(first.setData).not.toHaveBeenCalled();
    expect(clipboardMock.copyText).not.toHaveBeenCalled();
    expect(visibleClipboardConsent()).toBeInTheDocument();
    clickConsentButton("Copy once");
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
    expect(clipboardMock.copyText).toHaveBeenCalledWith("browser selection");

    const next = await copySelection("next browser selection");
    expect(next.setData).not.toHaveBeenCalled();
    expect(visibleClipboardConsent()).toBeInTheDocument();
    expect(readTerminalClipboardPreference()).toBe("ask");
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(1);
  });

  it("uses the native copy event for allowed selections without requiring the async clipboard API", async () => {
    writeTerminalClipboardPreference("allow");
    await renderClipboardView();

    const copied = await copySelection("selected text");

    expect(copied.setData).toHaveBeenCalledWith("text/plain", "selected text");
    expect(copied.event.defaultPrevented).toBe(true);
    expect(clipboardMock.copyText).not.toHaveBeenCalled();
    expect(visibleClipboardConsent()).toBeNull();
  });

  it("blocks browser selection copies and explains how to re-enable them", async () => {
    writeTerminalClipboardPreference("block");
    await renderClipboardView();

    const copied = await copySelection("blocked text");

    expect(copied.setData).not.toHaveBeenCalled();
    expect(clipboardMock.copyText).not.toHaveBeenCalled();
    expect(visibleClipboardConsent()).toBeNull();
    expect(screen.getByText("Copying from this terminal is blocked.")).toBeInTheDocument();
    expect(screen.getByText("Change this in Settings → General.")).toBeInTheDocument();
  });

  it.each(["ask", "block"] as const)(
    "revokes selection copying in a mounted terminal when Settings changes to %s",
    async (decision) => {
      writeTerminalClipboardPreference("allow");
      await renderClipboardView();
      const before = await copySelection("allowed text");
      expect(before.setData).toHaveBeenCalledWith("text/plain", "allowed text");

      act(() => writeTerminalClipboardPreference(decision));
      const after = await copySelection("after revocation");

      expect(after.setData).not.toHaveBeenCalled();
      expect(clipboardMock.copyText).not.toHaveBeenCalled();
      expect(terminalSessionMock.instances).toHaveLength(1);
      if (decision === "ask") expect(visibleClipboardConsent()).toBeInTheDocument();
      else expect(visibleClipboardConsent()).toBeNull();
    },
  );

  it("keeps an older in-flight program copy from overwriting a newer browser selection", async () => {
    writeTerminalClipboardPreference("allow");
    await renderClipboardView();
    let finishOldCopy: (() => void) | undefined;
    clipboardMock.copyText.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          finishOldCopy = resolve;
        }),
    );
    await requestClipboard("older program copy");
    const newer = await copySelection("newer browser selection");

    expect(newer.setData).not.toHaveBeenCalled();
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(1);
    await act(async () => finishOldCopy?.());
    expect(clipboardMock.copyText.mock.calls).toEqual([
      ["older program copy"],
      ["newer browser selection"],
    ]);
  });

  it("lets a native selection copy satisfy a pending browser retry", async () => {
    writeTerminalClipboardPreference("allow");
    await renderClipboardView();
    clipboardMock.copyText.mockRejectedValueOnce(new Error("browser needs a gesture"));
    await requestClipboard("program selection");
    await waitFor(() => expect(visibleClipboardConsent()).toHaveTextContent("Copy needs a click"));

    const copied = await copySelection("explicit browser selection");

    expect(copied.setData).toHaveBeenCalledWith("text/plain", "explicit browser selection");
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(1);
    expect(readTerminalClipboardPreference()).toBe("allow");
  });

  it.each(["ask", "allow", "block"] as const)(
    "honors %s for explicit selections in read-only terminals without allowing program writes",
    async (decision) => {
      writeTerminalClipboardPreference(decision);
      render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" readOnly />);
      await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
      await requestClipboard("program request from read-only terminal");
      expect(clipboardMock.copyText).not.toHaveBeenCalled();
      expect(visibleClipboardConsent()).toBeNull();

      const copied = await copySelection("explicit read-only selection");

      if (decision === "allow") {
        expect(copied.setData).toHaveBeenCalledWith("text/plain", "explicit read-only selection");
      } else {
        expect(copied.setData).not.toHaveBeenCalled();
      }
      if (decision === "ask") {
        expect(visibleClipboardConsent()).toBeInTheDocument();
        clickConsentButton("Copy once");
        await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
        expect(clipboardMock.copyText).toHaveBeenCalledWith("explicit read-only selection");
      } else {
        expect(visibleClipboardConsent()).toBeNull();
        expect(clipboardMock.copyText).not.toHaveBeenCalled();
      }
    },
  );

  it("does not copy selections from an inactive terminal", async () => {
    writeTerminalClipboardPreference("allow");
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" active={false} />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));

    const copied = await copySelection("hidden selection");

    expect(copied.setData).not.toHaveBeenCalled();
    expect(clipboardMock.copyText).not.toHaveBeenCalled();
    expect(visibleClipboardConsent()).toBeNull();
  });

  it("allows automatic copies for the rest of the mounted terminal session", async () => {
    await renderClipboardView();
    await requestClipboardConsent("first text");

    await allowForThisTerminal();
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledWith("first text"));

    await requestClipboard("second text");
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenLastCalledWith("second text"));
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
  });

  it.each(["allow", "block"] as const)(
    "remembers %s across conversations, terminals, and remounts",
    async (decision) => {
      const view = render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
      await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
      await requestClipboardConsent("first text");
      clickConsentButton(decision === "allow" ? "Allow copying" : "Block");

      expect(readTerminalClipboardPreference()).toBe(decision);
      await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
      if (decision === "allow") {
        await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));
        expect(clipboardMock.copyText).toHaveBeenLastCalledWith("first text");
      }

      view.rerender(<TerminalView sessionId="conv_next" terminalId="terminal_bash_s2" />);
      await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
      await requestClipboard("second text", 1);
      expect(visibleClipboardConsent()).toBeNull();
      if (decision === "allow") {
        await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(2));
      }

      view.unmount();
      render(<TerminalView sessionId="conv_third" terminalId="terminal_bash_s3" />);
      await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(3));
      await requestClipboard("third text", 2);
      expect(visibleClipboardConsent()).toBeNull();
      if (decision === "allow") {
        await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(3));
        expect(clipboardMock.copyText).toHaveBeenLastCalledWith("third text");
      } else {
        expect(clipboardMock.copyText).not.toHaveBeenCalled();
      }
    },
  );

  it("updates an already-mounted terminal after another terminal remembers Allow", async () => {
    render(
      <>
        <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />
        <TerminalView sessionId="conv_next" terminalId="terminal_bash_s2" />
      </>,
    );
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
    await requestClipboardConsent("first text");
    clickConsentButton("Allow copying");
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));

    await requestClipboard("other terminal", 1);
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(2));
    expect(clipboardMock.copyText).toHaveBeenLastCalledWith("other terminal");
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
  });

  it.each(["allow", "block"] as const)(
    "a remembered %s overrides an earlier opposite terminal-only choice",
    async (decision) => {
      render(
        <>
          <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />
          <TerminalView sessionId="conv_next" terminalId="terminal_bash_s2" />
        </>,
      );
      await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
      await requestClipboardConsent("terminal-only choice");
      uncheckRemember();
      clickConsentButton(decision === "allow" ? "Block" : "Allow for this session");
      await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
      expect(readTerminalClipboardPreference()).toBe("ask");

      const before = await copySelection("before the remembered choice");
      if (decision === "allow") expect(before.setData).not.toHaveBeenCalled();
      else
        expect(before.setData).toHaveBeenCalledWith("text/plain", "before the remembered choice");

      await requestClipboardConsent("choice in another terminal", 1);
      expect(screen.getByRole("checkbox", { name: "Remember my choice" })).toBeChecked();
      clickConsentButton(decision === "allow" ? "Allow copying" : "Block");
      await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
      expect(readTerminalClipboardPreference()).toBe(decision);
      clipboardMock.copyText.mockClear();

      const after = await copySelection("after the remembered choice");
      await requestClipboard("program copy after the remembered choice");

      if (decision === "allow") {
        expect(after.setData).toHaveBeenCalledWith("text/plain", "after the remembered choice");
        expect(clipboardMock.copyText).toHaveBeenCalledWith(
          "program copy after the remembered choice",
        );
      } else {
        expect(after.setData).not.toHaveBeenCalled();
        expect(clipboardMock.copyText).not.toHaveBeenCalled();
      }
      expect(visibleClipboardConsent()).toBeNull();
      expect(terminalSessionMock.instances).toHaveLength(2);
    },
  );

  it("keeps another terminal's pending selection available after remembered Allow", async () => {
    render(
      <>
        <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />
        <TerminalView sessionId="conv_next" terminalId="terminal_bash_s2" />
      </>,
    );
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
    const firstPrompt = await requestClipboardConsent("first terminal text");
    await requestClipboard("second terminal text", 1);
    await waitFor(() =>
      expect(screen.getAllByTestId("terminal-clipboard-consent")).toHaveLength(2),
    );
    const secondPrompt = screen
      .getAllByTestId("terminal-clipboard-consent")
      .find((prompt) => prompt !== firstPrompt)!;

    fireEvent.click(within(firstPrompt).getByRole("button", { name: "Allow copying" }));
    await waitFor(() => expect(firstPrompt).not.toBeInTheDocument());
    await waitFor(() => expect(secondPrompt).toHaveTextContent("Finish copying your selection"));
    expect(secondPrompt).toHaveTextContent("Your selection hasn’t been copied yet.");
    expect(within(secondPrompt).queryByRole("checkbox")).not.toBeInTheDocument();
    expect(clipboardMock.copyText.mock.calls).toEqual([["first terminal text"]]);

    fireEvent.click(within(secondPrompt).getByRole("button", { name: "Copy now" }));
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(2));
    expect(clipboardMock.copyText).toHaveBeenLastCalledWith("second terminal text");
    await waitFor(() => expect(secondPrompt).not.toBeInTheDocument());
    expect(readTerminalClipboardPreference()).toBe("allow");
  });

  it("replaces a pending shared-grant selection when the user copies newer text", async () => {
    await renderClipboardView();
    await requestClipboardConsent("older selection");
    act(() => writeTerminalClipboardPreference("allow"));
    await waitFor(() =>
      expect(visibleClipboardConsent()).toHaveTextContent("Finish copying your selection"),
    );
    expect(clipboardMock.copyText).not.toHaveBeenCalled();

    await requestClipboard("newer selection");
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
    expect(clipboardMock.copyText.mock.calls).toEqual([["newer selection"]]);
  });

  it.each(["ask", "block"] as const)(
    "discards a pending shared-grant selection after revocation to %s",
    async (decision) => {
      await renderClipboardView();
      await requestClipboardConsent("pending selection");
      act(() => writeTerminalClipboardPreference("allow"));
      await waitFor(() =>
        expect(visibleClipboardConsent()).toHaveTextContent("Finish copying your selection"),
      );

      act(() => writeTerminalClipboardPreference(decision));
      await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
      expect(clipboardMock.copyText).not.toHaveBeenCalled();
      await requestClipboard("selection after revocation");
      expect(clipboardMock.copyText).not.toHaveBeenCalled();
      if (decision === "ask") expect(visibleClipboardConsent()).toBeInTheDocument();
      else expect(visibleClipboardConsent()).toBeNull();
    },
  );

  it("keeps concurrent terminal prompts and their pending copies independent", async () => {
    render(
      <>
        <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />
        <TerminalView sessionId="conv_next" terminalId="terminal_bash_s2" />
      </>,
    );
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
    const firstPrompt = await requestClipboardConsent("first terminal text");
    fireEvent.click(within(firstPrompt).getByRole("checkbox"));

    await requestClipboard("second terminal text", 1);
    await waitFor(() =>
      expect(screen.getAllByTestId("terminal-clipboard-consent")).toHaveLength(2),
    );
    const secondPrompt = screen
      .getAllByTestId("terminal-clipboard-consent")
      .find((prompt) => prompt !== firstPrompt)!;
    expect(within(firstPrompt).getByRole("checkbox")).not.toBeChecked();
    expect(within(secondPrompt).getByRole("checkbox")).toBeChecked();
    expect(firstPrompt.closest("[data-sonner-toast]")).not.toBe(
      secondPrompt.closest("[data-sonner-toast]"),
    );

    await requestClipboard("newest first terminal text");
    expect(screen.getAllByTestId("terminal-clipboard-consent")).toHaveLength(2);
    expect(within(firstPrompt).getByRole("checkbox")).not.toBeChecked();
    expect(within(secondPrompt).getByRole("checkbox")).toBeChecked();
    expect(clipboardMock.copyText).not.toHaveBeenCalled();

    fireEvent.click(within(firstPrompt).getByRole("button", { name: "Allow for this session" }));
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));
    expect(clipboardMock.copyText).toHaveBeenLastCalledWith("newest first terminal text");
    await waitFor(() => expect(firstPrompt).not.toBeInTheDocument());
    expect(secondPrompt).toBeInTheDocument();
    expect(within(secondPrompt).getByRole("checkbox")).toBeChecked();
    expect(readTerminalClipboardPreference()).toBe("ask");

    fireEvent.click(within(secondPrompt).getByRole("button", { name: "Copy once" }));
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(2));
    expect(clipboardMock.copyText).toHaveBeenLastCalledWith("second terminal text");
    await waitFor(() => expect(secondPrompt).not.toBeInTheDocument());
    expect(readTerminalClipboardPreference()).toBe("ask");

    await requestClipboard("first terminal remains allowed");
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(3);
    expect(clipboardMock.copyText).toHaveBeenLastCalledWith("first terminal remains allowed");
    await requestClipboard("second terminal still asks", 1);
    expect(visibleClipboardConsent()).toBeInTheDocument();
    expect(screen.getByRole("checkbox")).toBeChecked();
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(3);
  });

  it("preserves an unchecked Remember choice when newer text replaces the request", async () => {
    const terminal = await renderClipboardView();
    const input = document.createElement("input");
    terminal.container.appendChild(input);
    input.focus();
    const prompt = await requestClipboardConsent("older text");

    expect(input).toHaveFocus();
    expect(screen.getByTestId("terminal-view")).not.toContainElement(prompt);
    expect(prompt.closest("[data-sonner-toaster]")).toHaveAttribute("data-x-position", "right");
    expect(prompt.closest("[data-sonner-toaster]")).toHaveAttribute("data-y-position", "bottom");
    expect(prompt).toHaveAttribute("role", "region");
    uncheckRemember();
    // Click before Sonner's asynchronous update publishes the new callback.
    act(() => terminal.onClipboardRequest?.("newest text"));
    expect(screen.getByRole("checkbox")).not.toBeChecked();
    clickConsentButton("Allow for this session");

    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));
    expect(clipboardMock.copyText).toHaveBeenCalledWith("newest text");
    expect(readTerminalClipboardPreference()).toBe("ask");
  });

  it("makes an exiting prompt inert while a different terminal asks for consent", async () => {
    const view = render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    const oldPrompt = await requestClipboardConsent("old terminal text");
    const oldAllow = within(oldPrompt).getByRole("button", { name: "Allow copying" });
    const oldDismiss = within(oldPrompt).getByRole("button", { name: "Dismiss clipboard request" });

    view.rerender(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s2" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
    await requestClipboard("new terminal text", 1);
    await waitFor(() =>
      expect(screen.getAllByTestId("terminal-clipboard-consent")).toHaveLength(2),
    );
    const newPrompt = screen
      .getAllByTestId("terminal-clipboard-consent")
      .find((prompt) => prompt !== oldPrompt)!;

    // Sonner keeps the old controls in the DOM during their exit animation.
    expect(oldPrompt).toBeInTheDocument();
    fireEvent.click(oldAllow);
    fireEvent.click(oldDismiss);
    expect(clipboardMock.copyText).not.toHaveBeenCalled();
    expect(readTerminalClipboardPreference()).toBe("ask");
    expect(newPrompt).toBeInTheDocument();

    fireEvent.click(within(newPrompt).getByRole("button", { name: "Copy once" }));
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));
    expect(clipboardMock.copyText).toHaveBeenCalledWith("new terminal text");
    expect(readTerminalClipboardPreference()).toBe("ask");
    await waitFor(() =>
      expect(screen.queryAllByTestId("terminal-clipboard-consent")).toHaveLength(0),
    );
  });

  it("dismisses without granting permission or remembering a choice", async () => {
    await renderClipboardView();
    await requestClipboardConsent("dismissed text");
    clickConsentButton("Dismiss clipboard request");

    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
    expect(clipboardMock.copyText).not.toHaveBeenCalled();
    expect(readTerminalClipboardPreference()).toBe("ask");
    await requestClipboardConsent("new text");
    expect(screen.getByRole("checkbox")).toBeChecked();
  });

  it("reports failed persistence and grants only the mounted terminal", async () => {
    const view = render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    await requestClipboardConsent("first text");
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("storage denied");
    });
    clickConsentButton("Allow copying");
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));
    expect(readTerminalClipboardPreference()).toBe("ask");
    expect(await screen.findByText(/Couldn't remember your clipboard choice/)).toBeInTheDocument();

    await requestClipboard("second text");
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
    view.unmount();
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
    await requestClipboard("after remount", 1);
    expect(visibleClipboardConsent()).toBeInTheDocument();
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(2);
  });

  it.each(["allow", "block"] as const)(
    "offers only terminal-scoped %s when the server identity is unavailable",
    async (decision) => {
      vi.spyOn(host, "getOmnigentServerIdentity").mockReturnValue(null);
      const view = render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
      await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
      const prompt = await requestClipboardConsent("first selection");
      const remember = within(prompt).getByRole("checkbox", { name: "Remember my choice" });
      expect(remember).toBeDisabled();
      expect(remember).not.toBeChecked();
      expect(prompt).toHaveTextContent("This connection can’t remember clipboard permissions.");

      clickConsentButton(decision === "allow" ? "Allow for this session" : "Block");
      await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
      expect(screen.queryByText(/Couldn't remember your clipboard choice/)).not.toBeInTheDocument();
      expect(readTerminalClipboardPreference()).toBe("ask");
      await requestClipboard("second selection");
      expect(clipboardMock.copyText).toHaveBeenCalledTimes(decision === "allow" ? 2 : 0);
      expect(visibleClipboardConsent()).toBeNull();

      view.unmount();
      render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
      await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
      await requestClipboardConsent("selection after remount", 1);
      expect(clipboardMock.copyText).toHaveBeenCalledTimes(decision === "allow" ? 2 : 0);
    },
  );

  it("does not retain a grant when the mounted view changes servers", async () => {
    const identity = vi.spyOn(host, "getOmnigentServerIdentity").mockReturnValue("server-one");
    writeTerminalClipboardPreference("allow");
    const view = render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    await requestClipboard("trusted server");
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));

    identity.mockReturnValue("server-two");
    view.rerender(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
    await requestClipboard("stale server callback");
    expect(visibleClipboardConsent()).toBeNull();
    await requestClipboard("untrusted server", 1);
    expect(visibleClipboardConsent()).toBeInTheDocument();
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(1);
    expect(readTerminalClipboardPreference()).toBe("ask");
  });

  it.each([{ active: false }, { readOnly: true }])(
    "does not let remembered Allow bypass inactive or read-only guards: %j",
    async (props) => {
      writeTerminalClipboardPreference("allow");
      render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" {...props} />);
      await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
      await requestClipboard("not eligible");

      expect(clipboardMock.copyText).not.toHaveBeenCalled();
      expect(visibleClipboardConsent()).toBeNull();
    },
  );

  it.each(["ask", "block"] as const)(
    "revokes mounted consent and queued copies when Settings changes to %s",
    async (decision) => {
      await renderClipboardView();
      await requestClipboardConsent("initial text");
      await allowForThisTerminal();
      await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));

      let rejectInFlight: ((reason: Error) => void) | undefined;
      clipboardMock.copyText.mockImplementationOnce(
        () =>
          new Promise<void>((_resolve, reject) => {
            rejectInFlight = reject;
          }),
      );
      await requestClipboard("in flight");
      await requestClipboard("queued before revocation");
      act(() => writeTerminalClipboardPreference(decision));
      await act(async () => rejectInFlight?.(new Error("revoked")));

      expect(clipboardMock.copyText).toHaveBeenCalledTimes(2);
      expect(visibleClipboardConsent()).toBeNull();
      await requestClipboard("after revocation");
      expect(clipboardMock.copyText).toHaveBeenCalledTimes(2);
      if (decision === "ask") expect(visibleClipboardConsent()).toBeInTheDocument();
      else expect(visibleClipboardConsent()).toBeNull();
    },
  );

  it("revokes a remembered grant when another tab clears it", async () => {
    writeTerminalClipboardPreference("allow");
    await renderClipboardView();
    await requestClipboard("allowed");
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));

    act(() => {
      localStorage.clear();
      window.dispatchEvent(new StorageEvent("storage", { key: null, storageArea: localStorage }));
    });
    await requestClipboardConsent("after reset in another tab");
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(1);
  });

  it("resets clipboard consent when the view switches terminals", async () => {
    const { rerender } = render(
      <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />,
    );
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    await requestClipboardConsent("first terminal");
    await allowForThisTerminal();
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));

    let rejectOldCopy: ((reason: Error) => void) | undefined;
    clipboardMock.copyText.mockImplementationOnce(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectOldCopy = reject;
        }),
    );
    await requestClipboard("pending old terminal");
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(2));
    act(() => toast.dismiss());

    rerender(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s2" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
    await act(async () => rejectOldCopy?.(new Error("old terminal closed")));
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
    expect(screen.queryByText("Couldn't copy terminal selection to the clipboard.")).toBeNull();

    await requestClipboard("second terminal", 1);
    await waitFor(() => expect(visibleClipboardConsent()).not.toBeNull());
    expect(visibleClipboardConsent()).toBeInTheDocument();
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(2);
  });

  it("discards an unanswered prompt while the terminal is hidden", async () => {
    const { rerender } = render(
      <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />,
    );
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    await requestClipboardConsent("stale text");
    expect(visibleClipboardConsent()).toBeInTheDocument();

    rerender(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" active={false} />);
    rerender(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" active />);
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());

    await requestClipboardConsent("fresh text");
    expect(visibleClipboardConsent()).toBeInTheDocument();
  });

  it("resets clipboard consent after a read-only permission transition", async () => {
    const { rerender } = render(
      <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />,
    );
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    await requestClipboardConsent("initial text");
    await allowForThisTerminal();
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));

    rerender(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" readOnly />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
    rerender(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(3));
    await requestClipboard("after read-only", 2);
    await waitFor(() => expect(visibleClipboardConsent()).not.toBeNull());

    expect(visibleClipboardConsent()).toBeInTheDocument();
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(1);
  });

  it("suppresses stale clipboard completion after the terminal unmounts", async () => {
    const { unmount } = render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    await requestClipboardConsent("initial text");
    await allowForThisTerminal();
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));

    let rejectInFlight: ((reason: Error) => void) | undefined;
    clipboardMock.copyText.mockImplementationOnce(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectInFlight = reject;
        }),
    );
    await requestClipboard("pending text");
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(2));
    act(() => toast.dismiss());
    const terminal = terminalSessionMock.instances[0];

    unmount();
    await act(async () => rejectInFlight?.(new Error("unmounted")));

    expect(screen.queryByText("Couldn't copy terminal selection to the clipboard.")).toBeNull();
    expect(terminal.focus).toHaveBeenCalledTimes(1);
  });

  it("coalesces session-approved automatic copies to the newest pending text", async () => {
    await renderClipboardView();
    await requestClipboardConsent("initial text");
    await allowForThisTerminal();
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));

    let resolveInFlight: (() => void) | undefined;
    clipboardMock.copyText.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          resolveInFlight = resolve;
        }),
    );
    await requestClipboard("superseded text");
    await requestClipboard("newest text");
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(2));
    expect(clipboardMock.copyText).toHaveBeenLastCalledWith("superseded text");

    await act(async () => resolveInFlight?.());
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(3));
    expect(clipboardMock.copyText).toHaveBeenLastCalledWith("newest text");
  });

  it("blocks clipboard requests for the rest of the mounted terminal session", async () => {
    await renderClipboardView();
    await requestClipboardConsent("blocked text");

    uncheckRemember();
    clickConsentButton("Block");
    await requestClipboard("still blocked");

    expect(clipboardMock.copyText).not.toHaveBeenCalled();
    expect(readTerminalClipboardPreference()).toBe("ask");
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
  });

  it("keeps remembered consent when the browser requires a click to copy", async () => {
    await renderClipboardView();
    await requestClipboardConsent("first text");
    clickConsentButton("Allow copying");
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());

    clipboardMock.copyText
      .mockRejectedValueOnce(new Error("permission denied"))
      .mockResolvedValueOnce(undefined);
    act(() => toast.dismiss());
    await requestClipboard("retry text");

    await waitFor(() => expect(visibleClipboardConsent()).toHaveTextContent("Copy needs a click"));
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(readTerminalClipboardPreference()).toBe("allow");
    clickConsentButton("Copy now");

    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(3));
    expect(clipboardMock.copyText).toHaveBeenLastCalledWith("retry text");
    expect(readTerminalClipboardPreference()).toBe("allow");
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
  });

  it("keeps failed retries visible without retrying unsolicited writes", async () => {
    writeTerminalClipboardPreference("allow");
    await renderClipboardView();
    clipboardMock.copyText.mockRejectedValue(new Error("browser denied"));
    await requestClipboardConsent("first attempt");
    expect(visibleClipboardConsent()).toHaveTextContent("Copy needs a click");

    await requestClipboard("newer selection");
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(1);
    clickConsentButton("Copy now");
    await waitFor(() =>
      expect(visibleClipboardConsent()).toHaveTextContent(
        "Check your browser’s clipboard permissions",
      ),
    );
    expect(clipboardMock.copyText).toHaveBeenLastCalledWith("newer selection");
    expect(readTerminalClipboardPreference()).toBe("allow");

    clickConsentButton("Dismiss");
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
    await requestClipboard("next selection");
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(2);
    expect(visibleClipboardConsent()).toHaveTextContent("Copy needs a click");
  });
});

describe("hidden pre-warmed surface", () => {
  it("keeps one live session across an active flip and focuses on reveal", async () => {
    // Mount hidden (a pre-warmed attach behind the chat view): the
    // session dials immediately — that is the whole point of the
    // pre-warm — but must not take focus away from the composer.
    const { rerender } = render(
      <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" active={false} />,
    );
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    const inst = terminalSessionMock.instances[0];
    expect(inst.focus).not.toHaveBeenCalled();

    // Reveal: the SAME session is kept (no re-dial — a second instance
    // here means the flip reconnected the WebSocket) and focused, since
    // the WS-open auto-focus was a no-op while hidden.
    rerender(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" active />);
    await waitFor(() => expect(inst.focus).toHaveBeenCalledTimes(1));
    expect(terminalSessionMock.instances).toHaveLength(1);
    expect(inst.dispose).not.toHaveBeenCalled();

    // Hiding again neither disposes nor re-focuses.
    rerender(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" active={false} />);
    expect(inst.dispose).not.toHaveBeenCalled();
    expect(inst.focus).toHaveBeenCalledTimes(1);
  });

  it("toggles clipboard bridging when a warm surface is revealed", async () => {
    const { rerender } = render(
      <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" active={false} />,
    );
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    const inst = terminalSessionMock.instances[0];
    expect(inst.clipboardEnabled).toBe(false);

    rerender(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" active />);
    await waitFor(() => expect(inst.setClipboardEnabled).toHaveBeenCalledWith(true));
    expect(terminalSessionMock.instances).toHaveLength(1);
  });

  it("does not focus on a plain active mount (WS-open handles it)", async () => {
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    // No reveal edge — the session's own WS-open focus owns this case;
    // an extra explicit call would steal focus on every reconnect.
    expect(terminalSessionMock.instances[0].focus).not.toHaveBeenCalled();
  });
});

describe("late direct-attach advert", () => {
  it("retires the outgoing session when the advert lands on a live terminal", async () => {
    // The runner's loopback advert reaches the client on a terminals
    // refetch — after the terminal has already dialed. That prop change
    // re-runs the attach ref for the SAME mount node (React 18 hands the
    // node back rather than remounting, and skips the ref's cleanup), so
    // the attach itself has to retire its predecessor. Without that,
    // xterm stacks a second instance inside one node — two helper
    // textareas, two renderers, two live bridges.
    const { rerender } = render(
      <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />,
    );
    await act(async () => {});
    expect(terminalSessionMock.instances).toHaveLength(1);
    const relayed = terminalSessionMock.instances[0];

    rerender(
      <TerminalView
        sessionId="conv_abc"
        terminalId="terminal_bash_s1"
        directAttachUrl={
          "ws://127.0.0.1:54321/v1/sessions/conv_abc" +
          "/resources/terminals/terminal_bash_s1/attach?token=t"
        }
      />,
    );
    await act(async () => {});

    // Same node, so this is a re-attach rather than a remount — which is
    // exactly why the predecessor cannot be left running.
    const readvertised = terminalSessionMock.instances.at(-1)!;
    expect(readvertised).not.toBe(relayed);
    expect(readvertised.container).toBe(relayed.container);
    expect(relayed.dispose).toHaveBeenCalled();
  });
});

describe("closed bridge overlay", () => {
  it("renders a resume button beside the closed message and invokes the callback", async () => {
    const onResume = vi.fn().mockResolvedValue(undefined);
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" onResume={onResume} />);
    // One initial instance means the bridge mounted exactly once before the
    // closed state; zero would mean no terminal attached, two would mean a
    // duplicate WebSocket handshake before resume.
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));

    act(() => {
      terminalSessionMock.instances[0].onState({ kind: "closed", reason: "stopped", code: 4405 });
    });

    expect(screen.getByText("Bridge closed: stopped")).toBeInTheDocument();
    const button = screen.getByRole("button", { name: /^resume session$/i });
    expect(button).toBeEnabled();

    fireEvent.click(button);
    // Exactly one resume call proves the button is wired once; zero would
    // mean it is inert, while multiple calls would duplicate the server
    // relaunch request.
    await waitFor(() => expect(onResume).toHaveBeenCalledTimes(1));
    // One instance is the initial bridge; a second appears only after
    // successful resume, proving the xterm mount remounted to reconnect.
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
  });

  it("disables the resume button while resume is pending", async () => {
    render(
      <TerminalView
        sessionId="conv_abc"
        terminalId="terminal_bash_s1"
        onResume={vi.fn()}
        resumePending
      />,
    );
    // The pending-state assertion must run against the first bridge mount;
    // extra instances here would mean props alone caused an unwanted remount.
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));

    act(() => {
      terminalSessionMock.instances[0].onState({ kind: "closed", reason: "stopped", code: 4405 });
    });

    expect(screen.getByRole("button", { name: /^resuming/i })).toBeDisabled();
  });

  it("does not render a resume button when no action is provided", async () => {
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    // Without an onResume prop the terminal still mounts once, but the closed
    // overlay must not invent its own resume action.
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));

    act(() => {
      terminalSessionMock.instances[0].onState({ kind: "closed", reason: "stopped", code: 4405 });
    });

    expect(screen.queryByRole("button", { name: /^resume session$/i })).toBeNull();
  });

  it("keeps the closed overlay visible and surfaces resume failures", async () => {
    const onResume = vi.fn().mockRejectedValue(new Error("host offline"));
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" onResume={onResume} />);
    // Start from exactly one bridge so the later length check proves failed
    // resume did not remount xterm.
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));

    act(() => {
      terminalSessionMock.instances[0].onState({ kind: "closed", reason: "stopped", code: 4405 });
    });
    fireEvent.click(screen.getByRole("button", { name: /^resume session$/i }));

    // The failing action still fires exactly once; zero would hide the
    // failure, while multiple calls would duplicate a bad resume request.
    await waitFor(() => expect(onResume).toHaveBeenCalledTimes(1));
    await screen.findByText("Couldn't resume session: host offline");
    // Failed resume must not remount xterm: the original closed bridge stays
    // visible so the user can retry after fixing the host.
    expect(terminalSessionMock.instances).toHaveLength(1);
  });
});

describe("automatic reconnect", () => {
  beforeEach(() => {
    // Fake only what the backoff scheduling touches; promises and
    // queueMicrotask (which the mount path uses) stay real so React
    // act() flushes them naturally.
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  /** Mount the view and flush the deferred (microtask) session attach. */
  async function renderAndAttach(): Promise<void> {
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await act(async () => {});
    // Exactly one bridge per mount — see the closed-overlay tests.
    expect(terminalSessionMock.instances).toHaveLength(1);
  }

  /** Drive a close on the newest session instance. */
  function closeNewest(code: number): void {
    act(() => {
      terminalSessionMock.instances.at(-1)!.onState({
        kind: "closed",
        reason: `code ${code}`,
        code,
      });
    });
  }

  /** Advance past a backoff delay and flush the remount microtask. */
  async function elapse(ms: number): Promise<void> {
    act(() => {
      vi.advanceTimersByTime(ms);
    });
    await act(async () => {});
  }

  it("re-dials after a transport-level close (1006) once the backoff elapses", async () => {
    await renderAndAttach();

    closeNewest(1006);

    // Recovery is presented as recovery: the overlay must show the
    // reconnecting spinner, not the dead-end "Bridge closed" message.
    expect(screen.getByTestId("terminal-reconnecting")).toBeInTheDocument();
    expect(screen.queryByText(/Bridge closed/)).toBeNull();

    await elapse(RECONNECT_BACKOFF_MS[0]);
    // A second instance proves the keyed mount remounted and re-dialed;
    // still 1 would mean the close was treated as final.
    expect(terminalSessionMock.instances).toHaveLength(2);
    // The dead session was torn down explicitly. React 18 ignores the
    // callback-ref cleanup, so a missing dispose here means every
    // retry leaks an xterm instance and its listeners.
    expect(terminalSessionMock.instances[0].dispose).toHaveBeenCalled();
  });

  it("re-dials after a code-less close (1005) — the redeploy-behind-ingress case", async () => {
    // A server redeploy behind a fronting proxy tears the attach socket
    // down without a clean app code reaching the browser, which reports
    // 1005 ("no status"). It must recover exactly like 1006, not dead-end
    // on "Bridge closed: code 1005".
    await renderAndAttach();

    closeNewest(1005);

    expect(screen.getByTestId("terminal-reconnecting")).toBeInTheDocument();
    expect(screen.queryByText(/Bridge closed/)).toBeNull();

    await elapse(RECONNECT_BACKOFF_MS[0]);
    expect(terminalSessionMock.instances).toHaveLength(2);
    expect(terminalSessionMock.instances[0].dispose).toHaveBeenCalled();
  });

  it("does not re-dial after a deliberate server close (4405 terminal-detached)", async () => {
    await renderAndAttach();

    closeNewest(4405);

    // Far beyond every backoff step: any scheduled re-dial would have
    // fired by now. A second instance would mean the policy resurrects
    // terminals the server intentionally ended.
    await elapse(60_000);
    expect(terminalSessionMock.instances).toHaveLength(1);
    expect(screen.getByText("Bridge closed: code 4405")).toBeInTheDocument();
    expect(screen.queryByTestId("terminal-reconnecting")).toBeNull();
  });

  it("stops re-dialing once the retry budget is exhausted", async () => {
    await renderAndAttach();

    // Each close→backoff cycle burns one budget entry. The re-dialed
    // connections never reach "connected", so the budget never resets.
    // Cycles are inherently serial: each backoff must elapse before the
    // next close can be driven.
    for (const [attempt, delay] of RECONNECT_BACKOFF_MS.entries()) {
      closeNewest(1006);
      // oxlint-disable-next-line no-await-in-loop
      await elapse(delay);
      // One new instance per attempt; a missing one means a backoff
      // step was skipped, an extra one means double-scheduling.
      expect(terminalSessionMock.instances).toHaveLength(attempt + 2);
    }

    closeNewest(1006);
    await elapse(60_000);
    // Budget exhausted: the final close sticks as the dead-end overlay
    // and no further sessions are constructed.
    expect(terminalSessionMock.instances).toHaveLength(RECONNECT_BACKOFF_MS.length + 1);
    expect(screen.getByText("Bridge closed: code 1006")).toBeInTheDocument();
    expect(screen.queryByTestId("terminal-reconnecting")).toBeNull();
  });

  it("restores the retry budget after a connection that stayed up past the stability window", async () => {
    await renderAndAttach();

    // Exhaust the budget with instant drops (serial by nature: each
    // backoff must elapse before the next close can be driven).
    for (const [, delay] of RECONNECT_BACKOFF_MS.entries()) {
      closeNewest(1006);
      // oxlint-disable-next-line no-await-in-loop
      await elapse(delay);
    }
    const exhausted = terminalSessionMock.instances.length;

    // The last re-dial succeeds and stays connected past the stability
    // window — this drop is a fresh outage, not the same flapping one.
    act(() => {
      terminalSessionMock.instances.at(-1)!.onState({ kind: "connected" });
    });
    await elapse(RECONNECT_STABLE_MS);
    closeNewest(1006);
    await elapse(RECONNECT_BACKOFF_MS[0]);

    // One more instance proves the budget reset; staying at `exhausted`
    // would mean a long-lived terminal gets only 5 reconnects per page
    // load instead of 5 per outage.
    expect(terminalSessionMock.instances).toHaveLength(exhausted + 1);
  });

  it("re-dials as soon as the tab becomes visible, without waiting out the backoff", async () => {
    // Simulate the report: the drop is discovered while the tab is
    // hidden, and the user returns before any timer fires.
    const visibility = { value: "hidden" as DocumentVisibilityState };
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => visibility.value,
    });
    try {
      await renderAndAttach();
      closeNewest(1006);

      // Still hidden: a visibilitychange that is not a reveal (e.g.
      // another hide event) must not trigger the re-dial.
      act(() => {
        document.dispatchEvent(new Event("visibilitychange"));
      });
      await act(async () => {});
      expect(terminalSessionMock.instances).toHaveLength(1);

      visibility.value = "visible";
      act(() => {
        document.dispatchEvent(new Event("visibilitychange"));
      });
      await act(async () => {});
      // The reveal re-dialed immediately — no timer was advanced, so a
      // missing second instance means the visibility path isn't wired.
      expect(terminalSessionMock.instances).toHaveLength(2);
    } finally {
      // Restore the default prototype getter for later tests.
      delete (document as { visibilityState?: unknown }).visibilityState;
    }
  });

  it("re-dials with a fresh budget when a hidden warm surface is revealed", async () => {
    // A warm surface parked behind another session's view: the transport
    // flaps with nobody watching and the background reconnect loop burns
    // its whole budget.
    const { rerender } = render(
      <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" active={false} />,
    );
    await act(async () => {});
    expect(terminalSessionMock.instances).toHaveLength(1);

    for (const [, delay] of RECONNECT_BACKOFF_MS.entries()) {
      closeNewest(1006);
      // oxlint-disable-next-line no-await-in-loop
      await elapse(delay);
    }
    closeNewest(1006);
    await elapse(60_000);
    const exhausted = RECONNECT_BACKOFF_MS.length + 1;
    expect(terminalSessionMock.instances).toHaveLength(exhausted);

    // Reveal: a user is now looking at the dead pane — that is the retry
    // signal, same as a tab thaw. One fresh dial, budget restored.
    rerender(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" active />);
    await act(async () => {});
    expect(terminalSessionMock.instances).toHaveLength(exhausted + 1);
  });

  it("does not resurrect a deliberately closed terminal on reveal", async () => {
    const { rerender } = render(
      <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" active={false} />,
    );
    await act(async () => {});
    closeNewest(4405);
    await elapse(60_000);
    expect(terminalSessionMock.instances).toHaveLength(1);

    // The server ended this terminal on purpose; revealing the surface
    // must keep the dead-end overlay, not loop on the same answer.
    rerender(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" active />);
    await act(async () => {});
    expect(terminalSessionMock.instances).toHaveLength(1);
    expect(screen.getByText("Bridge closed: code 4405")).toBeInTheDocument();
  });
});
