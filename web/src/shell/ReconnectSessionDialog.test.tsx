// Unit tests for ReconnectSessionDialog — the affordance shown when the
// open session is unreachable (host offline, or not host-bound with the
// runner down). Two tabs: Reconnect (instruction + CLI command) and
// Clone (the shared ForkSessionForm).
//
// What we lock:
//   1. host_offline owner → `omnigent host` (no --resume / YAML),
//      Reconnect tab is the default.
//   2. host_offline non-owner → no command at all (only the owner can
//      reach the host machine), Clone tab is the default.
//   3. local_stranded → wrapper-specific resume command, with the conv
//      id + server URL substituted.
//   4. claude-native local_stranded → `omnigent claude --resume`.
//   5. The Clone tab embeds ForkSessionForm with the source props, and
//      its onClose closes the dialog.
//   6. open={false} renders nothing.
//
// ForkSessionForm itself (host picker, fork+launch flow, retry
// semantics) is covered by ForkSessionDialog.test.tsx — here it is
// stubbed so these tests pin the dialog's own contract: which tab is
// default, what each tab shows, and what props reach the form.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ReconnectSessionDialog, buildReconnectCommand } from "./ReconnectSessionDialog";

vi.mock("./ForkSessionDialog", () => ({
  ForkSessionForm: (props: {
    sourceSessionId: string;
    sourceTitle?: string | null;
    sourceWorkspace?: string | null;
    sourceHostId?: string | null;
    sourceGitBranch?: string | null;
    onClose: () => void;
  }) => (
    <div
      data-testid="fork-session-form-stub"
      data-source-session-id={props.sourceSessionId}
      data-source-title={props.sourceTitle ?? ""}
      data-source-workspace={props.sourceWorkspace ?? ""}
      data-source-host-id={props.sourceHostId ?? ""}
      data-source-git-branch={props.sourceGitBranch ?? ""}
    >
      <button type="button" data-testid="fork-session-form-close" onClick={props.onClose}>
        close
      </button>
    </div>
  ),
}));

afterEach(() => {
  cleanup();
});

describe("buildReconnectCommand", () => {
  it("emits `omnigent host` for host_offline (no --resume, no YAML)", () => {
    const cmd = buildReconnectCommand({
      conversationId: "conv_host1",
      serverUrl: "https://example.databricksapps.com",
      state: "host_offline",
    });
    expect(cmd).toContain("omnigent host");
    expect(cmd).toContain("--server https://example.databricksapps.com");
    // The --profile flag was removed from the CLI; emitting it here would
    // hand users a command that errors with "No such option".
    expect(cmd).not.toContain("--profile");
    // The server relaunches the runner on demand — nothing to --resume,
    // no local YAML, regardless of wrapper.
    expect(cmd).not.toContain("--resume");
    expect(cmd).not.toContain("path/to/agent.yaml");
  });

  it("prefers `omnigent host` for a host_offline claude-native session", () => {
    // A claude-native session can still be host-bound; while the host is
    // down the host relaunches whatever runtime it needs, so host wins.
    const cmd = buildReconnectCommand({
      conversationId: "conv_host_claude",
      serverUrl: "https://x.databricksapps.com",
      wrapper: "claude-code-native-ui",
      state: "host_offline",
    });
    expect(cmd).toContain("omnigent host");
    expect(cmd).not.toContain("omnigent claude");
  });

  it("emits the generic run form for a local_stranded session", () => {
    const cmd = buildReconnectCommand({
      conversationId: "conv_abc123",
      serverUrl: "https://example.databricksapps.com",
      state: "local_stranded",
    });
    expect(cmd).toContain("omnigent run path/to/agent.yaml");
    expect(cmd).toContain("--resume conv_abc123");
    expect(cmd).toContain("--server https://example.databricksapps.com");
    expect(cmd).not.toContain("--profile");
  });

  it("emits `omnigent claude --resume` for a claude-native local_stranded session", () => {
    const cmd = buildReconnectCommand({
      conversationId: "conv_claude1",
      serverUrl: "https://x.databricksapps.com",
      wrapper: "claude-code-native-ui",
      state: "local_stranded",
    });
    expect(cmd).toContain("omnigent claude");
    expect(cmd).toContain("--resume conv_claude1");
    // No agent YAML for claude-native — the wrapper has none.
    expect(cmd).not.toContain("path/to/agent.yaml");
    expect(cmd).not.toContain("omnigent run");
  });

  it("falls back to the run form for an unknown wrapper (local_stranded)", () => {
    const cmd = buildReconnectCommand({
      conversationId: "conv_other",
      serverUrl: "https://x.databricksapps.com",
      wrapper: "some-future-wrapper",
      state: "local_stranded",
    });
    expect(cmd).toContain("omnigent run path/to/agent.yaml");
    expect(cmd).not.toContain("omnigent claude");
  });
});

describe("<ReconnectSessionDialog />", () => {
  function renderDialog(props: Partial<React.ComponentProps<typeof ReconnectSessionDialog>> = {}) {
    const onOpenChange = vi.fn();
    render(
      <ReconnectSessionDialog
        open
        onOpenChange={onOpenChange}
        conversationId="conv_abc123"
        serverUrl="https://example.databricksapps.com"
        state="host_offline"
        isOwner
        {...props}
      />,
    );
    return { onOpenChange };
  }

  // Radix Tabs activates a trigger on mousedown (not click), so fire both.
  function switchToTab(testId: string): void {
    const trigger = screen.getByTestId(testId);
    fireEvent.mouseDown(trigger);
    fireEvent.click(trigger);
  }

  // The clone panel is forceMount-ed (it must keep the fork form's state
  // across tab switches), so it is always in the DOM and only hidden via
  // a Tailwind data-[state=inactive] class — which jsdom can't evaluate.
  // Assert on the panel's data-state instead of toBeVisible().
  function clonePanelState(): string | null | undefined {
    return screen
      .getByTestId("fork-session-form-stub")
      .closest('[role="tabpanel"]')
      ?.getAttribute("data-state");
  }

  it("defaults to the Reconnect tab with the host command for a host_offline owner", () => {
    renderDialog({ state: "host_offline", isOwner: true });
    expect(screen.getByText("Host is offline")).toBeInTheDocument();
    const block = screen.getByTestId("reconnect-session-command");
    expect(block.textContent).toContain("omnigent host");
    // The clone form stays mounted (forceMount) but its panel is the
    // inactive one while the Reconnect tab is the default.
    expect(clonePanelState()).toBe("inactive");
  });

  it("defaults to the Clone tab for a host_offline non-owner", () => {
    renderDialog({ state: "host_offline", isOwner: false });
    // A non-owner can't reach the host machine, so reconnecting is
    // impossible — cloning is the only action and must be front and
    // center. The fork form is the active panel.
    expect(clonePanelState()).toBe("active");
    // The Reconnect tab is inactive (it unmounts when not selected), so
    // no command renders anywhere.
    expect(screen.queryByTestId("reconnect-session-command")).toBeNull();
    expect(screen.getByText("Host is offline")).toBeInTheDocument();
  });

  it("explains owner-only reconnect (no command) on a non-owner's Reconnect tab", () => {
    renderDialog({ state: "host_offline", isOwner: false });
    switchToTab("reconnect-session-tab-reconnect");
    // Even when the non-owner opens the Reconnect tab, the command must
    // not render — only the explanation that the owner has to do it.
    expect(screen.queryByTestId("reconnect-session-command")).toBeNull();
    expect(screen.getByTestId("reconnect-session-description").textContent).toMatch(
      /only its owner can reconnect it/i,
    );
  });

  it("shows the run command for a local_stranded session (owner or not)", () => {
    renderDialog({ state: "local_stranded", isOwner: false });
    // local_stranded isn't about a host machine — whoever started it can
    // relaunch, so the command shows regardless of ownership.
    const block = screen.getByTestId("reconnect-session-command");
    expect(block.textContent).toContain("omnigent run path/to/agent.yaml");
    expect(block.textContent).toContain("--resume conv_abc123");
    expect(screen.getByText("Agent disconnected")).toBeInTheDocument();
    // The description testid pins the visible tab copy (the same string
    // also lives in the sr-only DialogDescription, so getByText can't).
    expect(screen.getByTestId("reconnect-session-description").textContent).toBe(
      "Run the command below from the machine where you started this session to reconnect.",
    );
  });

  it("shows the claude reattach command for a claude-native local_stranded session", () => {
    renderDialog({
      state: "local_stranded",
      wrapper: "claude-code-native-ui",
    });
    const block = screen.getByTestId("reconnect-session-command");
    expect(block.textContent).toContain("omnigent claude");
    expect(block.textContent).not.toContain("path/to/agent.yaml");
  });

  it("switching to the Clone tab reveals the fork form with the source props", () => {
    renderDialog({
      state: "local_stranded",
      sourceTitle: "My session",
      sourceWorkspace: "/Users/me/repo",
      sourceHostId: "host_1",
      sourceGitBranch: "main",
    });
    switchToTab("reconnect-session-tab-clone");
    const form = screen.getByTestId("fork-session-form-stub");
    expect(clonePanelState()).toBe("active");
    // The Reconnect panel unmounts when inactive — no stray command.
    expect(screen.queryByTestId("reconnect-session-command")).toBeNull();
    // The form receives the same source prefill the header-menu Clone
    // dialog gets — a missing prop here silently downgrades the clone
    // to a non-coding fork (no host/directory pickers).
    expect(form).toHaveAttribute("data-source-session-id", "conv_abc123");
    expect(form).toHaveAttribute("data-source-title", "My session");
    expect(form).toHaveAttribute("data-source-workspace", "/Users/me/repo");
    expect(form).toHaveAttribute("data-source-host-id", "host_1");
    expect(form).toHaveAttribute("data-source-git-branch", "main");
  });

  it("the fork form's onClose closes the dialog", () => {
    const { onOpenChange } = renderDialog();
    switchToTab("reconnect-session-tab-clone");
    fireEvent.click(screen.getByTestId("fork-session-form-close"));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("renders no dialog content when closed", () => {
    render(
      <ReconnectSessionDialog
        open={false}
        onOpenChange={() => {}}
        conversationId="conv_abc123"
        serverUrl="https://example.databricksapps.com"
        state="host_offline"
        isOwner
      />,
    );
    expect(screen.queryByTestId("reconnect-session-dialog")).toBeNull();
  });
});
