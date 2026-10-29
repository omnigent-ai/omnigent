import { useEffect, useState, type ReactNode } from "react";
import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, userEvent, waitFor, within } from "storybook/test";
import type { ExecutionTopology } from "@/hooks/useExecutionTopology";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { SwitchHostDialog } from "./SwitchHostDialog";

const sessionId = "conversation-switch-story";

const HOSTS = [
  { host_id: "host_studio", name: "studio-mac", owner: "alice", status: "online" },
  { host_id: "host_linux", name: "linux-box", owner: "alice", status: "online" },
].map((h) => ({ ...h, configured_harnesses: { "claude-native": true, "codex-native": true } }));

const MODELS = [
  { id: "story-large", displayName: "Story Large", isDefault: true },
  { id: "story-small", displayName: "Story Small" },
];

const binding = (host_id: string, machine_id: string, account_id: string) => ({
  host_id,
  machine_id,
  account_id,
  harness: "claude-native" as const,
  available: true,
  reason: null,
});

const TOPOLOGY: ExecutionTopology = {
  configured: true,
  machines: [
    { id: "studio", label: "Studio" },
    { id: "linux", label: "Linux" },
  ],
  accounts: [
    {
      id: "claude-personal",
      label: "Claude Personal",
      provider: "anthropic",
      harness: "claude-native",
    },
    { id: "claude-lab", label: "Claude Lab", provider: "anthropic", harness: "claude-native" },
    {
      id: "openai-personal",
      label: "OpenAI Personal",
      provider: "openai",
      harness: "codex-native",
    },
  ],
  bindings: [
    binding("host_studio", "studio", "claude-personal"),
    binding("host_linux", "linux", "claude-personal"),
    binding("host_linux", "linux", "claude-lab"),
  ],
};

const UNCONFIGURED: ExecutionTopology = {
  configured: false,
  machines: [],
  accounts: [],
  bindings: [],
};

const PENDING_MOVE = {
  id: "move-1",
  account_id: "claude-personal",
  host_id: "host_linux",
  workspace: "/home/alice/repo",
  harness: "claude-native",
  model: "story-large",
  effort: "high",
};

function sessionWire(pending: boolean) {
  return {
    id: sessionId,
    agent_id: "agent-switch-story",
    status: "idle",
    runner_id: pending ? null : "runner-1",
    host_id: "host_studio",
    workspace: "/home/alice/repo",
    harness: "claude-native",
    model_override: "story-large",
    reasoning_effort: "high",
    llm_model: "story-large",
    cost_control_mode_override: null,
    labels: { "omnigent.wrapper": "claude-code-native-ui" },
    account_transition: pending ? PENDING_MOVE : null,
    items: [],
    created_at: 1_700_000_000,
    title: "Story session",
  };
}

const json = (payload: unknown, status = 200) =>
  new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });

/** Answers the dialog's requests from memory and restores fetch on unmount. */
function StubbedApi({
  topology,
  pending,
  children,
}: {
  topology: ExecutionTopology;
  pending: boolean;
  children: ReactNode;
}) {
  const [original] = useState(() => window.fetch);
  // Installed during the first render, before any child query starts.
  useState(() => {
    window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(
        typeof input === "string" ? input : String(input),
        window.location.origin,
      );
      const path = url.pathname;
      const method = (init?.method ?? "GET").toUpperCase();
      if (method !== "GET") return json({ detail: "read-only story" }, 405);
      if (path === "/v1/execution-topology") {
        return topology.configured ? json(topology) : json({ detail: "Not Found" }, 404);
      }
      if (path === `/v1/sessions/${sessionId}`) return json(sessionWire(pending));
      if (path === "/v1/hosts") return json({ hosts: HOSTS });
      if (path.endsWith("/model-options")) {
        return json({
          models: MODELS.map((m) => ({
            ...m,
            source: { kind: "subscription", label: "Story sign-in" },
          })),
        });
      }
      if (path.includes("/filesystem")) return json({ data: [], has_more: false });
      return json({ data: [], has_more: false });
    }) as typeof window.fetch;
    return null;
  });
  useEffect(
    () => () => {
      window.fetch = original;
    },
    [original],
  );
  return children;
}

function environment(topology: ExecutionTopology, pending = false) {
  return (Story: () => ReactNode) => (
    <StubbedApi topology={topology} pending={pending}>
      <StoryQueryRouter>
        <Story />
      </StoryQueryRouter>
    </StubbedApi>
  );
}

const meta = {
  title: "Shell/SwitchHostDialog",
  component: SwitchHostDialog,
  tags: ["visual-snapshot"],
  args: {
    open: true,
    onOpenChange: () => undefined,
    sessionId,
    currentHostId: "host_studio",
    currentWorkspace: "/home/alice/repo",
  },
} satisfies Meta<typeof SwitchHostDialog>;

export default meta;
type Story = StoryObj<typeof meta>;

async function accountDialogReady(canvasElement: HTMLElement) {
  const body = within(canvasElement.ownerDocument.body);
  await waitFor(() => expect(body.getByTestId("switch-model-select")).toBeTruthy(), {
    timeout: 5000,
  });
  return body;
}

export const SameMachineAccount: Story = {
  decorators: [environment(TOPOLOGY)],
  play: async ({ canvasElement }) => {
    await accountDialogReady(canvasElement);
  },
};

export const CrossMachineMove: Story = {
  decorators: [environment(TOPOLOGY)],
  play: async ({ canvasElement }) => {
    canvasElement.ownerDocument.body.style.pointerEvents = "auto";
    const body = await accountDialogReady(canvasElement);
    await userEvent.click(body.getByTestId("switch-machine-select"));
    await userEvent.click(await body.findByTestId("switch-machine-option-linux"));
    await waitFor(() =>
      expect(body.getByTestId("switch-host-button").textContent).toBe("Move session"),
    );
  },
};

export const InterruptedMove: Story = {
  decorators: [environment(TOPOLOGY, true)],
  play: async ({ canvasElement }) => {
    const body = within(canvasElement.ownerDocument.body);
    await body.findByTestId("switch-cancel-move-button", undefined, { timeout: 5000 });
  },
};

export const NoAccountRegistry: Story = {
  decorators: [environment(UNCONFIGURED)],
  play: async ({ canvasElement }) => {
    const body = within(canvasElement.ownerDocument.body);
    await body.findByTestId("switch-host-button", undefined, { timeout: 5000 });
  },
};
