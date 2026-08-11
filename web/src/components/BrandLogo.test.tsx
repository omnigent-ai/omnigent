import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ServerInfo } from "@/lib/capabilities";
import type { CapabilitiesContext as CapabilitiesContextType } from "@/lib/CapabilitiesContext";

const getOmnigentHostConfig = vi.fn();
const hostFetch = vi.fn();

vi.mock("@/lib/host", () => ({
  getOmnigentHostConfig: () => getOmnigentHostConfig(),
  hostFetch: (path: string) => hostFetch(path),
}));
vi.mock("@/components/OttoEyes", () => ({
  OttoEyes: () => <span data-testid="otto-eyes" />,
}));
vi.mock("@/components/icons/OttoIcon", () => ({
  OttoIcon: () => <span data-testid="otto-icon" />,
}));

import type { BrandLogo as BrandLogoComponent } from "./BrandLogo";

let BrandLogo: typeof BrandLogoComponent;
let CapabilitiesContext: typeof CapabilitiesContextType;
const infoByPath = Object.fromEntries(
  ["standalone", "embed", "missing"].map((name) => [
    name,
    {
      branding: {
        app_name: "Acme Agent",
        heading: "Build with Acme",
        logos: { main: `/v1/branding/logo/${name}`, loading: null, favicon: null },
        powered_by: true,
      },
    } as ServerInfo,
  ]),
) as Record<"standalone" | "embed" | "missing", ServerInfo>;

beforeEach(async () => {
  vi.resetModules();
  ({ CapabilitiesContext } = await import("@/lib/CapabilitiesContext"));
  ({ BrandLogo } = await import("./BrandLogo"));
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:brand-logo") });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function renderLogo(info: ServerInfo) {
  return render(
    <CapabilitiesContext.Provider value={info}>
      <BrandLogo className="brand" />
    </CapabilitiesContext.Provider>,
  );
}

describe("BrandLogo", () => {
  it("uses the direct route in standalone deployments", () => {
    getOmnigentHostConfig.mockReturnValue({ fetcher: undefined });

    renderLogo(infoByPath.standalone);

    expect(screen.getByRole("img", { name: "Acme Agent" })).toHaveAttribute(
      "src",
      "/v1/branding/logo/standalone",
    );
    expect(hostFetch).not.toHaveBeenCalled();
  });

  it("uses hostFetch and an object URL in embedded deployments", async () => {
    getOmnigentHostConfig.mockReturnValue({ fetcher: () => {} });
    const blob = new Blob(["logo"]);
    hostFetch.mockResolvedValue({ ok: true, blob: () => Promise.resolve(blob) });

    renderLogo(infoByPath.embed);

    expect(screen.getByTestId("otto-eyes")).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: "Acme Agent" })).toHaveAttribute(
      "src",
      "blob:brand-logo",
    );
    expect(hostFetch).toHaveBeenCalledWith("/v1/branding/logo/embed");
    expect(URL.createObjectURL).toHaveBeenCalledWith(blob);
  });

  it("keeps the mascot fallback when the embedded fetch fails", async () => {
    getOmnigentHostConfig.mockReturnValue({ fetcher: () => {} });
    hostFetch.mockResolvedValue({ ok: false, status: 404 });

    renderLogo(infoByPath.missing);

    expect(await screen.findByTestId("otto-eyes")).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Acme Agent" })).not.toBeInTheDocument();
  });
});
