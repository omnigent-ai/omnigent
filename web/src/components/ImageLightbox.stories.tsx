import type { Meta, StoryObj } from "@storybook/react-vite";
import { userEvent, within } from "storybook/test";
import { ImageLightboxProvider, ZoomableImage } from "./ImageLightbox";

const diagram = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(`
  <svg xmlns="http://www.w3.org/2000/svg" width="960" height="540" viewBox="0 0 960 540">
    <defs><linearGradient id="g" x1="0" x2="1"><stop stop-color="#e52671"/><stop offset="1" stop-color="#4dc5a0"/></linearGradient></defs>
    <rect width="960" height="540" rx="32" fill="#f7f7f8"/>
    <rect x="80" y="110" width="250" height="150" rx="20" fill="url(#g)"/>
    <rect x="630" y="280" width="250" height="150" rx="20" fill="url(#g)"/>
    <path d="M330 185 C500 185 460 355 630 355" fill="none" stroke="#52525b" stroke-width="12" stroke-linecap="round"/>
    <text x="205" y="195" text-anchor="middle" font-family="system-ui" font-size="30" fill="white">Story</text>
    <text x="755" y="365" text-anchor="middle" font-family="system-ui" font-size="30" fill="white">Snapshot</text>
  </svg>
`)}`;

const meta = {
  title: "Components/Media/ImageLightbox",
  tags: ["visual-snapshot"],
  decorators: [
    (Story) => (
      <ImageLightboxProvider>
        <Story />
      </ImageLightboxProvider>
    ),
  ],
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

const thumbnail = () => (
  <ZoomableImage
    src={diagram}
    alt="Story to snapshot workflow diagram"
    className="h-48 w-80 rounded-xl border object-cover shadow-sm"
  />
);

export const Thumbnail: Story = {
  render: thumbnail,
};

export const OpenAndZoomed: Story = {
  render: thumbnail,
  play: async ({ canvasElement }) => {
    await userEvent.click(
      within(canvasElement).getByRole("button", {
        name: "Zoom image: Story to snapshot workflow diagram",
      }),
    );
    const body = within(canvasElement.ownerDocument.body);
    await userEvent.click(body.getByRole("button", { name: "Zoom in" }));
    await userEvent.click(body.getByRole("button", { name: "Zoom in" }));
  },
};
