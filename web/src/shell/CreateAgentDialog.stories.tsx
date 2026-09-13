import type { Meta, StoryObj } from "@storybook/react-vite";
import { fireEvent, userEvent, within } from "storybook/test";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { CreateAgentDialog } from "./CreateAgentDialog";

const harnessCatalog = {
  labels: {
    "claude-sdk": "Claude SDK",
    codex: "Codex",
    cursor: "Cursor",
    pi: "Pi",
    antigravity: "Antigravity",
  },
  setupSteps: {},
  acpHarnesses: new Set<string>(),
};

const meta = {
  title: "Components/Agents/CreateAgentDialog",
  component: CreateAgentDialog,
  tags: ["visual-snapshot"],
  args: { open: true, onOpenChange: () => undefined, onCreate: () => undefined },
  decorators: [
    (Story) => (
      <StoryQueryRouter
        seed={(queryClient) => queryClient.setQueryData(["harness-labels"], harnessCatalog)}
      >
        <Story />
      </StoryQueryRouter>
    ),
  ],
} satisfies Meta<typeof CreateAgentDialog>;

export default meta;
type Story = StoryObj<typeof meta>;

export const EmptyForm: Story = {};

export const FilledReadyToCreate: Story = {
  play: async ({ canvasElement }) => {
    const body = within(canvasElement.ownerDocument.body);
    await fireEvent.change(body.getByTestId("create-agent-name"), {
      target: { value: "polly-helper" },
    });
    await fireEvent.change(body.getByTestId("create-agent-description"), {
      target: { value: "Helps triage sessions" },
    });
    await fireEvent.change(body.getByTestId("create-agent-model"), {
      target: { value: "claude-sonnet-5" },
    });
    await fireEvent.change(body.getByTestId("create-agent-instructions"), {
      target: { value: "Summarize failed runs and recommend the next action." },
    });
  },
};

export const TwoMcpServers: Story = {
  play: async ({ canvasElement }) => {
    canvasElement.ownerDocument.body.style.pointerEvents = "auto";
    const body = within(canvasElement.ownerDocument.body);
    await fireEvent.click(body.getByTestId("create-agent-add-mcp"));
    await fireEvent.click(body.getByTestId("create-agent-add-mcp"));
    const entries = body.getAllByTestId("create-agent-mcp-entry");
    const first = within(entries[0]!);
    await fireEvent.change(first.getByTestId("create-agent-mcp-name"), {
      target: { value: "github" },
    });
    await fireEvent.change(first.getByTestId("create-agent-mcp-command"), {
      target: { value: "npx" },
    });
    await fireEvent.change(first.getByTestId("create-agent-mcp-args"), {
      target: { value: "-y @modelcontextprotocol/server-github" },
    });
    await fireEvent.change(first.getByTestId("create-agent-mcp-env"), {
      target: { value: "GITHUB_TOKEN=example" },
    });

    const second = within(entries[1]!);
    await fireEvent.change(second.getByTestId("create-agent-mcp-name"), {
      target: { value: "remote-tools" },
    });
    await userEvent.click(second.getByTestId("create-agent-mcp-transport"));
    await userEvent.click(await body.findByRole("option", { name: "http" }));
    await fireEvent.change(second.getByTestId("create-agent-mcp-url"), {
      target: { value: "https://mcp.example.com/sse" },
    });
    await fireEvent.change(second.getByTestId("create-agent-mcp-headers"), {
      target: { value: "Authorization: Bearer example" },
    });
    const scrollRegion = body
      .getByTestId("create-agent-dialog")
      .querySelector<HTMLElement>(".overflow-y-auto");
    if (!scrollRegion) throw new Error("Create-agent scroll region not found");
    scrollRegion.scrollTop = scrollRegion.scrollHeight;
  },
};
