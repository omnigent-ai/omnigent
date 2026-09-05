import type { Meta, StoryObj } from "@storybook/react-vite";
import { userEvent, within } from "storybook/test";
import {
  CompactionMarker,
  ErrorBanner,
  PolicyDeniedBanner,
  RetryIndicator,
  RoutingDecisionCard,
} from "./StatusBlocks";

const terminalError = [
  "Required terminal exited unexpectedly; the session runtime is no longer available.",
  "",
  "Terminal diagnostics:",
  "terminal: claude:main",
  "command: claude (arguments omitted)",
  "cwd: /workspace/omnigent",
  "pid: 48291",
  "runtime: claude-code 1.0.83",
  "exit_code: 0",
  "termination_reason: terminal pane no longer available",
  "",
  "Last captured terminal output:",
  "Pane is dead (status 0)",
].join("\n");

const meta = {
  title: "Components/Blocks/StatusIndicators",
  tags: ["visual-snapshot"],
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

export const PolicyDenied: Story = {
  render: () => (
    <PolicyDeniedBanner
      phase="before tool call"
      reason="Writing outside the selected workspace is not allowed."
    />
  ),
};

export const RetryingImmediately: Story = {
  render: () => <RetryIndicator source="runner" attempt={2} maxAttempts={4} delaySeconds={0} />,
};

export const RetryingAfterDelay: Story = {
  render: () => (
    <RetryIndicator source="model stream" attempt={2} maxAttempts={4} delaySeconds={1.5} />
  ),
};

export const Compacted: Story = {
  render: () => <CompactionMarker />,
};

export const ClassifiedError: Story = {
  render: () => (
    <ErrorBanner
      message="Claude Code refused to start because the host process is running as root."
      source="execution"
      code="required_terminal_exited"
      title="Claude Code can't run as root"
      cause="Claude Code refuses this launch mode when the host runs as root."
      remediation="Run the host as a non-root user."
    />
  ),
};

export const TerminalErrorExpanded: Story = {
  render: () => (
    <ErrorBanner message={terminalError} source="execution" code="required_terminal_exited" />
  ),
  play: async ({ canvasElement }) => {
    await userEvent.click(
      within(canvasElement).getByRole("button", { name: /terminal exited unexpectedly/i }),
    );
  },
};

export const Reconnecting: Story = {
  render: () => (
    <ErrorBanner
      message={terminalError}
      source="execution"
      code="required_terminal_exited"
      onRetry={() =>
        new Promise<void>(() => {
          // Keep the visual fixture in its reconnecting state.
        })
      }
    />
  ),
  play: async ({ canvasElement }) => {
    await userEvent.click(within(canvasElement).getByRole("button", { name: "Retry" }));
  },
};

export const RoutingApplied: Story = {
  render: () => (
    <RoutingDecisionCard
      model="databricks-claude-opus-4-8"
      applied
      rationale="The multi-file refactor needs deeper reasoning."
    />
  ),
};

export const RoutingAdvisoryFromGateway: Story = {
  render: () => (
    <RoutingDecisionCard
      model="databricks-claude-haiku-4-5"
      applied={false}
      rationale="A mechanical scan can use the smaller model."
      routing={{ routerSource: "databricks-aigw" }}
    />
  ),
};

export const ChildRoutingRawResponse: Story = {
  render: () => (
    <RoutingDecisionCard
      model="databricks-claude-sonnet-5"
      applied
      rationale="The child task benefits from a balanced model."
      agent="researcher"
      routing={{
        harness: "codex-native",
        scope: "child_session",
        decisionId: "decision-story",
        rawModel: "gpt-5-6-sol",
        attemptedOverride: "databricks-claude-opus-4-8",
      }}
    />
  ),
  play: async ({ canvasElement }) => {
    await userEvent.click(
      within(canvasElement).getByRole("button", { name: "Show raw routing verdict" }),
    );
  },
};
