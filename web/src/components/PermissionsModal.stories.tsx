import type { Decorator, Meta, StoryObj } from "@storybook/react-vite";
import { fireEvent } from "storybook/test";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO, type SharingMode } from "@/lib/capabilities";
import type { Permission } from "@/lib/permissionsApi";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { PermissionsModal } from "./PermissionsModal";

const sessionId = "conversation-permissions-story";

function modalEnvironment({
  mode,
  permissions = [],
  publicSharing = true,
}: {
  mode: SharingMode;
  permissions?: Permission[];
  publicSharing?: boolean;
}): Decorator {
  return (Story) => (
    <CapabilitiesProvider
      info={{
        ...FALLBACK_SERVER_INFO,
        sharing_mode: mode,
        public_sharing_enabled: publicSharing,
      }}
    >
      <StoryQueryRouter
        route={`/c/${sessionId}`}
        seed={(queryClient) => {
          queryClient.setQueryData(["permissions", sessionId], permissions);
        }}
      >
        <Story />
      </StoryQueryRouter>
    </CapabilitiesProvider>
  );
}

const meta = {
  title: "Components/Sharing/PermissionsModal",
  component: PermissionsModal,
  tags: ["visual-snapshot"],
  args: { sessionId, open: true, onOpenChange: () => undefined },
} satisfies Meta<typeof PermissionsModal>;

export default meta;
type Story = StoryObj<typeof meta>;

export const SharingDisabled: Story = {
  decorators: [modalEnvironment({ mode: "off" })],
};

export const GrantSpectrum: Story = {
  decorators: [
    modalEnvironment({
      mode: "on",
      permissions: [
        { user_id: "owner@example.com", conversation_id: sessionId, level: 4 },
        {
          user_id: "alexandra.reyes@very-long-company-domain.example.com",
          conversation_id: sessionId,
          level: 3,
        },
        { user_id: "bob@example.com", conversation_id: sessionId, level: 2 },
        { user_id: "carol@example.com", conversation_id: sessionId, level: 1 },
      ],
    }),
  ],
  play: async ({ canvasElement }) => {
    const input = canvasElement.ownerDocument.querySelector<HTMLInputElement>("#perm-user");
    if (!input) throw new Error("Permission user input not found");
    await fireEvent.change(input, { target: { value: "dave@example.com" } });
  },
};

export const PublicReadOnly: Story = {
  decorators: [
    modalEnvironment({
      mode: "read_only",
      permissions: [
        { user_id: "owner@example.com", conversation_id: sessionId, level: 4 },
        { user_id: "__public__", conversation_id: sessionId, level: 1 },
        { user_id: "alice@example.com", conversation_id: sessionId, level: 1 },
      ],
    }),
  ],
};
