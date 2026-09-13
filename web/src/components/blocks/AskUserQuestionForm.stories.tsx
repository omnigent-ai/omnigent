import type { Meta, StoryObj } from "@storybook/react-vite";
import { userEvent } from "storybook/test";
import { AskUserQuestionForm } from "./AskUserQuestionForm";

const meta = {
  title: "Components/Blocks/AskUserQuestionForm",
  component: AskUserQuestionForm,
  tags: ["visual-snapshot"],
  args: {
    onSubmit: () => undefined,
    onReject: () => undefined,
  },
} satisfies Meta<typeof AskUserQuestionForm>;

export default meta;
type Story = StoryObj<typeof meta>;

export const SingleSelectWithPreview: Story = {
  args: {
    questions: [
      {
        id: "layout",
        question: "How should the visual comparison be presented?",
        header: "Layout",
        multiSelect: false,
        options: [
          {
            label: "Side by side",
            description: "Show the expected and actual render together.",
            preview: "EXPECTED        ACTUAL\n[component]  ↔  [component]",
          },
          {
            label: "Overlay",
            description: "Blend changed pixels over the baseline.",
          },
        ],
      },
    ],
  },
  play: async ({ canvasElement }) => {
    await userEvent.click(requiredElement(canvasElement, "#layout-Side\\ by\\ side"));
  },
};

export const MultiQuestionCarousel: Story = {
  args: {
    questions: [
      {
        id: "theme",
        question: "Which color scheme should be the baseline?",
        header: "Theme",
        multiSelect: false,
        options: [
          { label: "Light", description: "Use the default light palette." },
          { label: "Dark", description: "Use the dark palette." },
        ],
      },
      {
        id: "coverage",
        question: "Which states should this story cover?",
        header: "Coverage",
        multiSelect: true,
        options: [
          { label: "Default", description: "The resting component state." },
          { label: "Loading", description: "The state while work is in progress." },
          { label: "Error", description: "The recoverable failure state." },
        ],
      },
    ],
  },
  play: async ({ canvasElement }) => {
    await userEvent.click(requiredElement(canvasElement, "#theme-Light"));
    await userEvent.click(requiredElement(canvasElement, '[data-testid="ask-user-question-next"]'));
    await userEvent.click(requiredElement(canvasElement, "#coverage-Loading"));
    await userEvent.click(requiredElement(canvasElement, "#coverage-Error"));
  },
};

function requiredElement(root: HTMLElement, selector: string): HTMLElement {
  const element = root.querySelector<HTMLElement>(selector);
  if (!element) throw new Error(`Story element not found: ${selector}`);
  return element;
}
