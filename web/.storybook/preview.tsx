import type { Preview } from "@storybook/react-vite";
import { addons } from "storybook/preview-api";
import {
  PLAY_FUNCTION_THREW_EXCEPTION,
  STORY_FINISHED,
  STORY_PREPARED,
  type StoryFinishedPayload,
} from "storybook/internal/core-events";
import { ThemeProvider } from "@/components/theme/ThemeProvider";
import { TooltipProvider } from "@/components/ui/tooltip";
import "katex/dist/katex.min.css";
import "streamdown/styles.css";
import "../src/index.css";

const storybookChannel = addons.getChannel();
storybookChannel.on(STORY_PREPARED, () => {
  delete document.documentElement.dataset.storybookPlayError;
  delete document.documentElement.dataset.storybookStoryId;
  delete document.documentElement.dataset.storybookStoryStatus;
});
storybookChannel.on(PLAY_FUNCTION_THREW_EXCEPTION, () => {
  document.documentElement.dataset.storybookPlayError = "true";
});
storybookChannel.on(STORY_FINISHED, ({ storyId, status }: StoryFinishedPayload) => {
  document.documentElement.dataset.storybookStoryId = storyId;
  document.documentElement.dataset.storybookStoryStatus = status;
});

const preview: Preview = {
  decorators: [
    (Story) => (
      <ThemeProvider>
        <TooltipProvider>
          <div className="min-w-80 max-w-3xl p-6">
            <Story />
          </div>
        </TooltipProvider>
      </ThemeProvider>
    ),
  ],
  parameters: {
    controls: {
      matchers: {
        color: /(background|color)$/i,
        date: /Date$/i,
      },
    },
    layout: "centered",
  },
};

export default preview;
