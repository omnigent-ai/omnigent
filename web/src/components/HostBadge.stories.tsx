import type { Decorator, Meta, StoryObj } from "@storybook/react-vite";
import type { Host } from "@/hooks/useHosts";
import type { Session } from "@/lib/types";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { HostBadge } from "./HostBadge";

const sessionId = "conversation-host-story";

function hostEnvironment(host: Host, hostResumable = false): Decorator {
  return (Story) => (
    <StoryQueryRouter
      seed={(queryClient) => {
        queryClient.setQueryData(["session", sessionId], {
          id: sessionId,
          hostId: host.host_id,
          hostResumable,
        } as Session);
        queryClient.setQueryData(["hosts", { includeSandbox: true }], [host]);
      }}
    >
      <div className="flex h-16 w-[420px] items-center rounded-xl border bg-card px-4">
        <Story />
      </div>
    </StoryQueryRouter>
  );
}

const meta = {
  title: "Components/Hosts/HostBadge",
  component: HostBadge,
  tags: ["visual-snapshot"],
  args: { sessionId },
} satisfies Meta<typeof HostBadge>;

export default meta;
type Story = StoryObj<typeof meta>;

export const ConnectedOnline: Story = {
  decorators: [
    hostEnvironment({
      host_id: "host-online",
      name: "mac-laptop",
      owner: "alice",
      status: "online",
      sandbox_provider: null,
    }),
  ],
};

export const OfflineReconnect: Story = {
  args: { sessionId, onReconnect: () => undefined },
  decorators: [
    hostEnvironment({
      host_id: "host-offline",
      name: "mac-laptop",
      owner: "alice",
      status: "offline",
      sandbox_provider: null,
    }),
  ],
};

export const ManagedSandbox: Story = {
  decorators: [
    hostEnvironment(
      {
        host_id: "host-sandbox",
        name: "managed-runtime",
        owner: "system",
        status: "online",
        sandbox_provider: "lakebox",
      },
      true,
    ),
  ],
};
