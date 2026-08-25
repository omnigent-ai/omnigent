import type { Preview } from "@storybook/react-vite";
import { ThemeProvider } from "@/components/theme/ThemeProvider";
import { TooltipProvider } from "@/components/ui/tooltip";
import "katex/dist/katex.min.css";
import "streamdown/styles.css";
import "../src/index.css";

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
