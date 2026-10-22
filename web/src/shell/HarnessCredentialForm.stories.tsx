import type { Meta, StoryObj } from "@storybook/react-vite";
import type { Host } from "@/hooks/useHosts";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { HarnessCredentialForm } from "./HarnessCredentialForm";

const host: Host = {
  host_id: "host-credential-story",
  name: "developer-laptop",
  owner: "developer",
  status: "online",
};

const meta = {
  title: "Components/Harnesses/HarnessCredentialForm",
  component: HarnessCredentialForm,
  tags: ["visual-snapshot"],
  args: { host, onDone: () => undefined },
  decorators: [
    (Story) => (
      <div className="w-[680px] rounded-xl border bg-card p-5">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof HarnessCredentialForm>;

export default meta;
type Story = StoryObj<typeof meta>;

export const AllPathsOffered: Story = {
  args: { harness: "codex", label: "Codex", command: "codex login" },
  decorators: [
    (Story) => (
      <StoryQueryRouter
        seed={(queryClient) =>
          queryClient.setQueryData(
            ["detected-credentials", host.host_id],
            [{ family: "openai", source: "$OPENAI_API_KEY", env_var: "OPENAI_API_KEY" }],
          )
        }
      >
        <Story />
      </StoryQueryRouter>
    ),
  ],
};

export const KeyAndGatewayOnly: Story = {
  args: { harness: "pi", label: "Pi", command: null },
  decorators: [
    (Story) => (
      <StoryQueryRouter
        seed={(queryClient) => queryClient.setQueryData(["detected-credentials", host.host_id], [])}
      >
        <Story />
      </StoryQueryRouter>
    ),
  ],
};
