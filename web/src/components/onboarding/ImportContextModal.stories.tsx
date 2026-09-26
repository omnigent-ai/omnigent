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
