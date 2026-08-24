// Unit tests for the WebSocket attach path builder and the closed
// bridge overlay.
//
// The production TerminalView component creates xterm + a real
// WebSocket bridge. These tests mock that bridge and drive its state
// callback directly, while still pinning the pure URL builder contract
// the server cares about.

import type * as TerminalSessionModule from "./TerminalSession";

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ConnectionState } from "./TerminalSession";
import {
  TerminalView,
  RECONNECT_BACKOFF_MS,
  RECONNECT_STABLE_MS,
  buildAttachPath,
  selectionHintText,
} from "./TerminalView";

const clipboardMock = vi.hoisted(() => ({
  copyText: vi.fn<(text: string) => Promise<void>>(),
  showToast: vi.fn(),
}));

vi.mock("@/lib/clipboard", () => ({ copyText: clipboardMock.copyText }));
vi.mock("@/components/ui/toast", () => ({ showToast: clipboardMock.showToast }));

const terminalSessionMock = vi.hoisted(() => ({
  instances: [] as {
    url: string;
    container: HTMLDivElement;
    nativeSelection: boolean;
    clipboardEnabled: boolean;
    onClipboardRequest?: (text: string) => void;
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
      nativeSelection = false,
      clipboardEnabled = true,
      onClipboardRequest?: (text: string) => void,
    ) {
      terminalSessionMock.instances.push({
        url,
        container,
        nativeSelection,
        clipboardEnabled,
        onClipboardRequest,
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
  terminalSessionMock.instances = [];
  clipboardMock.copyText.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
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

  it("appends ?transport= when a transport override is given", () => {
    expect(buildAttachPath("conv_abc", "terminal_bash_s1", false, undefined, "control")).toBe(
      "/v1/sessions/conv_abc/resources/terminals/terminal_bash_s1/attach?transport=control",
    );
  });

  it("combines read_only, slice_key, and transport params", () => {
    const path = buildAttachPath("conv_abc", "terminal_bash_s1", true, "host_789", "control");
    expect(path).toContain("read_only=true");
    expect(path).toContain("omnigent_slice_key=host_789");
    expect(path).toContain("transport=control");
  });

  it("omits ?omnigent_slice_key when no hostId is provided", () => {
    const path = buildAttachPath("conv_abc", "terminal_bash_s1", false);
    expect(path.includes("omnigent_slice_key")).toBe(false);
  });

  it("omits ?transport when no override is given", () => {
    expect(buildAttachPath("conv_abc", "terminal_bash_s1", false).includes("transport")).toBe(
      false,
    );
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

describe("control-mode transport", () => {
  it("forwards ?transport=control and enables native selection when transport=control", async () => {
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" transport="control" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    const inst = terminalSessionMock.instances[0];
    expect(inst.url).toContain("transport=control");
    expect(inst.nativeSelection).toBe(true);
  });

  it("disables clipboard bridging for read-only attaches", async () => {
    render(
      <TerminalView
        sessionId="conv_abc"
        terminalId="terminal_bash_s1"
        transport="control"
        readOnly
      />,
    );
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    expect(terminalSessionMock.instances[0].clipboardEnabled).toBe(false);
  });

  it("hides the selection hint bar in control mode", async () => {
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" transport="control" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    expect(screen.queryByTestId("terminal-selection-hint")).toBeNull();
  });

  it("keeps the hint bar and PTY behavior by default (no transport prop)", async () => {
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    const inst = terminalSessionMock.instances[0];
    expect(inst.url).not.toContain("transport");
    expect(inst.nativeSelection).toBe(false);
    expect(screen.getByTestId("terminal-selection-hint")).toBeInTheDocument();
  });
});

describe("tmux clipboard", () => {
  it("writes validated terminal copies through the shared clipboard helper", async () => {
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));

    terminalSessionMock.instances[0].onClipboardRequest?.("copied text");

    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledWith("copied text"));
    expect(clipboardMock.showToast).toHaveBeenCalledWith("Copied from terminal.", {
      duration: 1500,
    });
    expect(terminalSessionMock.instances[0].focus).toHaveBeenCalled();
  });

  it("offers a gesture-backed retry when automatic clipboard access fails", async () => {
    clipboardMock.copyText
      .mockRejectedValueOnce(new Error("permission denied"))
      .mockResolvedValueOnce(undefined);
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));

    terminalSessionMock.instances[0].onClipboardRequest?.("retry text");
    await waitFor(() => expect(clipboardMock.showToast).toHaveBeenCalled());
    const retryContent = clipboardMock.showToast.mock.calls[0][0];
    render(retryContent);
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));

    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(2));
    expect(clipboardMock.copyText).toHaveBeenLastCalledWith("retry text");
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

describe("selectionHintText", () => {
  it("tells macOS users to hold Option and copy with Command", () => {
    // On macOS the force-selection modifier is Option and Cmd+C copies
    // (Cmd isn't forwarded to the shell). Both must appear; a regression
    // to Shift/Ctrl here would print the wrong keys for Mac users.
    const hint = selectionHintText(true);
    expect(hint).toContain("⌥");
    expect(hint).toContain("⌘C");
  });

  it("tells non-macOS users to hold Shift and copy via right-click", () => {
    // Elsewhere the modifier is Shift, and Ctrl+C is SIGINT (not copy),
    // so the hint must say Shift and must point at right-click → Copy
    // rather than a Ctrl+C shortcut that would kill the user's process.
    const hint = selectionHintText(false);
    expect(hint).toContain("Shift");
    expect(hint).toContain("right-click");
    expect(hint).not.toContain("⌘");
    expect(hint.toLowerCase()).not.toContain("ctrl");
  });
});
