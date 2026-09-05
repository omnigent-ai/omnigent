import type { Meta, StoryObj } from "@storybook/react-vite";
import { ApprovalCard } from "./ApprovalCard";

const meta = {
  title: "Components/Blocks/ApprovalCard",
  component: ApprovalCard,
  tags: ["visual-snapshot"],
  args: {
    elicitationId: "elicitation-story",
    requestedSchema: {},
    status: "pending",
    response: null,
    onSubmit: () => undefined,
  },
} satisfies Meta<typeof ApprovalCard>;

export default meta;
type Story = StoryObj<typeof meta>;

export const PolicyApproval: Story = {
  args: {
    message: "Allow the agent to remove the generated cache directory?",
    phase: "tool_call",
    policyName: "approve_shell_commands",
    contentPreview: 'Bash({"command":"rm -rf .cache/generated"})',
  },
};

export const NativeEditPermission: Story = {
  args: {
    message: "Claude wants to update the authentication callback.",
    phase: "pre_tool_use",
    policyName: "claude_native_permission",
    contentPreview: 'Edit({"file_path":"src/auth/callback.ts"})',
    allowAllEdits: true,
  },
};

export const RememberWebDomain: Story = {
  args: {
    message: "Claude wants to fetch the Storybook documentation.",
    phase: "pre_tool_use",
    policyName: "claude_native_permission",
    contentPreview: 'WebFetch({"url":"https://storybook.js.org/docs"})',
    rememberScope: { tool: "WebFetch", host: "storybook.js.org" },
  },
};

export const CodexCommand: Story = {
  args: {
    message: "Codex wants to run the focused frontend checks.",
    phase: "codex_command_approval",
    policyName: "codex_native_command_approval",
    contentPreview: "",
    codexCommand: {
      command: "pnpm --filter web run type-check",
      cwd: "/workspace/omnigent",
      reason: "Verify the new component stories compile.",
      execPolicyAmendment: ["pnpm", "--filter", "web"],
    },
  },
};

export const SubmittedAnswers: Story = {
  args: {
    message: "Claude asked how the visual tests should run.",
    phase: "pre_tool_use",
    policyName: "claude_native_permission",
    contentPreview: "",
    status: "responded",
    askUserQuestion: {
      questions: [
        {
          id: "renderer",
          question: "Which renderer should capture the stories?",
          header: "Renderer",
          multiSelect: false,
          options: [{ label: "Pinned Chromium", description: "Use the CI container." }],
        },
        {
          id: "coverage",
          question: "Which states should be captured?",
          header: "Coverage",
          multiSelect: true,
          options: [
            { label: "Light theme", description: "Capture the default palette." },
            { label: "Component states", description: "Capture each named story." },
          ],
        },
      ],
    },
    response: {
      action: "accept",
      content: {
        renderer: "Pinned Chromium",
        coverage: ["Light theme", "Component states"],
      },
    },
  },
};
