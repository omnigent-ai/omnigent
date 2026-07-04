// Unit tests for the WebSocket attach path builder and the closed
// bridge overlay.
//
// The production TerminalView component creates xterm + a real
// WebSocket bridge. These tests mock that bridge and drive its state
// callback directly, while still pinning the pure URL builder contract
// the server cares about.

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

const terminalSessionMock = vi.hoisted(() => ({
  instances: [] as Array<{
    url: string;
    nativeSelection: boolean;
    onState: (state: ConnectionState) => void;
    dispose: ReturnType<typeof vi.fn>;
    setTheme: ReturnType<typeof vi.fn>;
  }>,
}));

vi.mock("./TerminalSession", async (importOriginal) => ({
  // Keep the real module (isUnexpectedTerminalClose and friends) —
  // only the session class itself is replaced.
  ...(await importOriginal<typeof import("./TerminalSession")>()),
  TerminalSession: class {
    dispose = vi.fn();
    setTheme = vi.fn();

    constructor(
      _container: HTMLDivElement,
      url: string,
      onState: (state: ConnectionState) => void,
      _isDark?: boolean,
      _onActivity?: () => void,
      _onInput?: () => void,
      nativeSelection = false,
    ) {
      terminalSessionMock.instances.push({
        url,
        nativeSelection,
        onState,
        dispose: this.dispose,
        setTheme: this.setTheme,
      });
    }
  },
}));

beforeEach(() => {
  terminalSessionMock.instances = [];
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

  it("appends ?transport= when a transport override is given", () => {
    expect(buildAttachPath("conv_abc", "terminal_bash_s1", false, "control")).toBe(
      "/v1/sessions/conv_abc/resources/terminals/terminal_bash_s1/attach?transport=control",
    );
  });

  it("combines read_only and transport params", () => {
    const path = buildAttachPath("conv_abc", "terminal_bash_s1", true, "control");
    expect(path).toContain("read_only=true");
    expect(path).toContain("transport=control");
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
