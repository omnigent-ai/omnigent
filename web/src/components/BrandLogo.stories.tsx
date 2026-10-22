import type { Meta, StoryObj } from "@storybook/react-vite";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { BrandLogo } from "./BrandLogo";

const logoSvg = `<svg xmlns="http://www.w3.org/2000/svg" width="520" height="180" viewBox="0 0 520 180"><rect width="520" height="180" rx="36" fill="#111827"/><circle cx="90" cy="90" r="52" fill="#e52671"/><text x="165" y="108" font-family="system-ui" font-size="54" font-weight="700" fill="white">Acme Agent</text></svg>`;
const logoDataUri = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(logoSvg)}`;

const meta = {
  title: "Components/Branding/BrandLogo",
  component: BrandLogo,
  tags: ["visual-snapshot"],
  decorators: [
    (Story) => (
      <div className="flex min-h-40 w-[620px] items-center justify-center rounded-xl border bg-card">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof BrandLogo>;

export default meta;
type Story = StoryObj<typeof meta>;

export const DefaultEyes: Story = {
  args: { className: "h-20 w-auto", variant: "eyes" },
  decorators: [
    (Story) => (
      <CapabilitiesProvider info={FALLBACK_SERVER_INFO}>
        <Story />
      </CapabilitiesProvider>
    ),
  ],
};

export const DefaultIcon: Story = {
  args: { className: "size-16", variant: "icon" },
  decorators: [
    (Story) => (
      <CapabilitiesProvider info={FALLBACK_SERVER_INFO}>
        <Story />
      </CapabilitiesProvider>
    ),
  ],
};

export const CustomOperatorLogo: Story = {
  args: { className: "h-20 max-w-[520px] object-contain", variant: "eyes" },
  decorators: [
    (Story) => (
      <CapabilitiesProvider
        info={{
          ...FALLBACK_SERVER_INFO,
          branding: {
            app_name: "Acme Agent",
            heading: "What should we build?",
            logos: { main: logoDataUri, loading: logoDataUri, favicon: null },
            powered_by: true,
          },
        }}
      >
        <Story />
      </CapabilitiesProvider>
    ),
  ],
};
