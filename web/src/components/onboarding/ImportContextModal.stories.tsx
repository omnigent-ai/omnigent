import type { Meta, StoryObj } from "@storybook/react-vite";
import { fn } from "storybook/test";
import { MOCK_IMPORT_CONTEXT } from "./importContextMock";
import { ImportContextModal } from "./ImportContextModal";
import type { ImportContext } from "./ImportContextModal";

const meta = {
  title: "Components/Onboarding/ImportContextModal",
  component: ImportContextModal,
  args: {
    open: true,
    context: MOCK_IMPORT_CONTEXT,
    onOpenChange: fn(),
    onConfirm: fn(),
  },
} satisfies Meta<typeof ImportContextModal>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Every harness tab populated with its credential, MCPs, skills, and plugins. */
export const Default: Story = {};

/** Only credentials were detected; each harness tab shows the empty state. */
export const EmptyImports: Story = {
  args: {
    context: {
      credentials: MOCK_IMPORT_CONTEXT.credentials,
      mcps: [],
      skills: [],
      plugins: [],
    } satisfies ImportContext,
  },
};

/** Named for one of several machines. */
export const NamedHost: Story = { args: { hostName: "dev-laptop" } };

/** The host is still reporting its harnesses. */
export const Loading: Story = { args: { status: "loading" } };

/** The host went offline before it could report. */
export const Offline: Story = { args: { status: "offline", hostName: "dev-laptop" } };

/** Skills loaded but the host couldn't list its MCP servers. */
export const PartialFailure: Story = {
  args: {
    context: { ...MOCK_IMPORT_CONTEXT, mcps: [] } satisfies ImportContext,
    unavailable: ["mcps"],
  },
};
