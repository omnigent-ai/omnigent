import type { Meta, StoryObj } from "@storybook/react-vite";
import { userEvent, within } from "storybook/test";
import { ImageLightboxProvider } from "./ImageLightbox";
import { SessionImage } from "./SessionImage";

const landscapeSvg = `<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540"><rect width="960" height="540" fill="#f4f4f5"/><rect x="80" y="80" width="800" height="380" rx="28" fill="#e52671"/><text x="480" y="290" text-anchor="middle" font-family="system-ui" font-size="64" fill="white">Architecture</text></svg>`;
const portraitSvg = `<svg xmlns="http://www.w3.org/2000/svg" width="480" height="800"><rect width="480" height="800" fill="#111827"/><circle cx="240" cy="260" r="140" fill="#4dc5a0"/><text x="240" y="560" text-anchor="middle" font-family="system-ui" font-size="48" fill="white">Portrait</text></svg>`;
const dataUri = (svg: string) => `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;

const meta = {
  title: "Components/Media/SessionImage",
  component: SessionImage,
  tags: ["visual-snapshot"],
  decorators: [
    (Story) => (
      <ImageLightboxProvider>
        <div className="w-[700px] rounded-xl border bg-card p-4">
          <Story />
        </div>
      </ImageLightboxProvider>
    ),
  ],
} satisfies Meta<typeof SessionImage>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Landscape: Story = {
  args: { path: dataUri(landscapeSvg), alt: "Architecture diagram" },
};

export const PortraitLightboxOpen: Story = {
  args: { path: dataUri(portraitSvg), alt: "Portrait design preview" },
  play: async ({ canvasElement }) => {
    await userEvent.click(
      within(canvasElement).getByRole("button", { name: "Zoom image: Portrait design preview" }),
    );
  },
};
