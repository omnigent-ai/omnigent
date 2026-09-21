import type { Meta, StoryObj } from "@storybook/react-vite";
import { useState } from "react";
import { userEvent, within } from "storybook/test";

import type {
  ComposerContextResourceState,
  ComposerRepositorySelection,
} from "@/lib/composerContext";
import { ComposerRepositorySelector } from "./ComposerRepositorySelector";

const repositories: ComposerRepositorySelection[] = [
  { id: "app", url: "https://github.com/omnigent-ai/omnigent.git", branch: "main" },
  { id: "docs", url: "https://github.com/omnigent-ai/docs.git", branch: null },
  {
    id: "long",
    url: "https://github.com/omnigent-ai/a-repository-with-a-deliberately-long-name.git",
    branch: "feature/responsive-repository-context-selector",
  },
];

const EMPTY_SELECTION: ComposerRepositorySelection[] = [];
const READY_REPOSITORIES: ComposerContextResourceState<readonly ComposerRepositorySelection[]> = {
  status: "ready",
  data: repositories,
  error: null,
};

const meta = {
  title: "Composer/ComposerRepositorySelector",
  decorators: [
    (Story) => (
      <div className="w-[420px] max-w-[calc(100vw-2rem)]">
        <Story />
      </div>
    ),
  ],
} satisfies Meta;

export default meta;
type Story = StoryObj;

function ControlledSelector({
  initialValue = EMPTY_SELECTION,
  resource = READY_REPOSITORIES,
}: {
  initialValue?: ComposerRepositorySelection[];
  resource?: ComposerContextResourceState<readonly ComposerRepositorySelection[]>;
}) {
  const [value, setValue] = useState(initialValue);
  return <ComposerRepositorySelector value={value} repositories={resource} onChange={setValue} />;
}

export const EmptyOpen: Story = {
  render: () => <ControlledSelector />,
  play: async ({ canvasElement }) => {
    await userEvent.click(
      within(canvasElement).getByRole("button", {
        name: "Repositories, no repositories selected",
      }),
    );
  },
};

export const OrderedSelections: Story = {
  render: () => <ControlledSelector initialValue={[repositories[2], repositories[0]]} />,
};

export const NarrowWidth: Story = {
  decorators: [
    (Story) => (
      <div className="w-[240px]">
        <Story />
      </div>
    ),
  ],
  render: () => <ControlledSelector initialValue={[repositories[2]]} />,
};

export const Loading: Story = {
  render: () => <ControlledSelector resource={{ status: "loading", data: null, error: null }} />,
};

export const Unavailable: Story = {
  render: () => (
    <ControlledSelector resource={{ status: "unavailable", data: null, error: null }} />
  ),
};

export const ErrorWithSelectedRepository: Story = {
  render: () => (
    <ControlledSelector
      initialValue={[repositories[1]]}
      resource={{ status: "error", data: null, error: new Error("Host disconnected") }}
    />
  ),
};
