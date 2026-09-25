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

/** Full import context: credentials, MCPs, and skills all populated. */
export const Default: Story = {};

/** Only credentials were detected; MCP and skill lists are empty. */
export const EmptyImports: Story = {
  args: {
    context: {
      credentials: MOCK_IMPORT_CONTEXT.credentials,
      mcps: [],
      skills: [],
    } satisfies ImportContext,
  },
};
