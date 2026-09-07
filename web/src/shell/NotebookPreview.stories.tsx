import type { Meta, StoryObj } from "@storybook/react-vite";
import { waitFor } from "storybook/test";
import brokenNotebook from "./__fixtures__/03_broken.ipynb?raw";
import edgeCasesNotebook from "./__fixtures__/02_edgecases.ipynb?raw";
import typicalNotebook from "./__fixtures__/01_typical.ipynb?raw";
import { NotebookPreview } from "./NotebookPreview";

const meta = {
  title: "Components/Shell/NotebookPreview",
  component: NotebookPreview,
  tags: ["visual-snapshot"],
  decorators: [
    (Story) => (
      <div className="h-[600px] w-[700px] overflow-hidden rounded-lg border bg-card">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof NotebookPreview>;

export default meta;
type Story = StoryObj<typeof meta>;

async function waitForHighlight(canvasElement: HTMLElement): Promise<void> {
  await waitFor(
    () => {
      const blocks = canvasElement.querySelectorAll("[data-code-highlighted]");
      if (blocks.length === 0) throw new Error("Notebook rendered no code cells");
      if ([...blocks].some((block) => block.getAttribute("data-code-highlighted") !== "true")) {
        throw new Error("Notebook syntax highlighting is still pending");
      }
    },
    { timeout: 15_000 },
  );
}

export const TypicalNotebook: Story = {
  args: { content: typicalNotebook },
  play: async ({ canvasElement }) => waitForHighlight(canvasElement),
};

export const EdgeCaseOutputs: Story = {
  args: { content: edgeCasesNotebook },
  play: async ({ canvasElement }) => waitForHighlight(canvasElement),
};

export const ParseError: Story = {
  args: { content: brokenNotebook },
};
