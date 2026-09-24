import type { Meta, StoryObj } from "@storybook/react-vite";
import { ComposerAttachments } from "./ComposerAttachments";

const file = (name: string, content = "x", type = "text/plain") =>
  new File([content], name, { type });

const meta = {
  title: "Components/Composer/ComposerAttachments",
  component: ComposerAttachments,
  tags: ["visual-snapshot"],
  args: {
    onRemove: () => undefined,
  },
  decorators: [
    (Story) => (
      <div className="w-[620px] rounded-2xl border border-border bg-background p-3">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof ComposerAttachments>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Short: Story = {
  args: {
    files: [file("notes.txt", "hello")],
  },
};

export const LongFilename: Story = {
  args: {
    files: [
      file("quarterly-infrastructure-review-notes-final-v3.txt", "truncated"),
      file("a.ts", "code", "text/typescript"),
    ],
  },
};

export const MissingMetadata: Story = {
  args: {
    // No extension: the tile's metadata line falls back to the bare size.
    files: [file("LICENSE", "license text")],
  },
};

export const MentionPathChip: Story = {
  args: {
    // "@"-mention attachments ride the same chip component with their
    // workspace-relative path as the name.
    files: [file("src/components/composer/ChatComposer.tsx", "mention", "text/typescript")],
  },
};

export const WrappedRows: Story = {
  args: {
    files: [
      file("alpha.md"),
      file("beta.ts", "code", "text/typescript"),
      file("gamma.json", "{}", "application/json"),
      file("delta.py", "code", "text/x-python"),
      file("epsilon.yaml"),
      file("zeta-longer-name-to-fill-the-row.css", "code", "text/css"),
      file("eta.md"),
      file("theta.rs", "code", "text/rust"),
    ],
  },
};
