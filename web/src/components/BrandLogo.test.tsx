import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ServerInfo } from "@/lib/capabilities";
import type { CapabilitiesContext as CapabilitiesContextType } from "@/lib/CapabilitiesContext";

const getOmnigentHostConfig = vi.fn();
const authenticatedFetch = vi.fn();
let hostGeneration = 0;

vi.mock("@/lib/host", () => ({
  getOmnigentHostConfig: () => getOmnigentHostConfig(),
  getOmnigentHostGeneration: () => hostGeneration,
}));
vi.mock("@/lib/identity", () => ({
  authenticatedFetch: (path: string) => authenticatedFetch(path),
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
let createObjectURL: ReturnType<typeof vi.fn>;
let revokeObjectURL: ReturnType<typeof vi.fn>;

function serverInfo(path: string): ServerInfo {
  return {
    branding: {
      app_name: "Acme Agent",
      heading: "Build with Acme",
      logos: { main: path, loading: null, favicon: null },
      powered_by: true,
    },
  } as ServerInfo;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

beforeEach(async () => {
  vi.resetModules();
  ({ CapabilitiesContext } = await import("@/lib/CapabilitiesContext"));
  ({ BrandLogo } = await import("./BrandLogo"));
  hostGeneration = 0;
  createObjectURL = vi.fn(() => "blob:brand-logo");
  revokeObjectURL = vi.fn();
  vi.stubGlobal("URL", { createObjectURL, revokeObjectURL });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function logoTree(info: ServerInfo) {
  return (
    <CapabilitiesContext.Provider value={info}>
      <BrandLogo className="brand" />
    </CapabilitiesContext.Provider>
  );
}

describe("BrandLogo", () => {
  it("uses the direct route in standalone deployments", () => {
    getOmnigentHostConfig.mockReturnValue({ fetcher: undefined });

    render(logoTree(serverInfo("/v1/branding/logo/standalone")));

    expect(screen.getByRole("img", { name: "Acme Agent" })).toHaveAttribute(
      "src",
      "/v1/branding/logo/standalone",
    );
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("falls back to Otto when a standalone image fails to load", () => {
    getOmnigentHostConfig.mockReturnValue({ fetcher: undefined });
    render(logoTree(serverInfo("/v1/branding/logo/missing")));

    fireEvent.error(screen.getByRole("img", { name: "Acme Agent" }));

    expect(screen.getByTestId("otto-eyes")).toBeInTheDocument();
  });

  it("uses the host transport and an object URL in embedded deployments", async () => {
    getOmnigentHostConfig.mockReturnValue({ fetcher: () => {} });
    const blob = new Blob(["logo"]);
    authenticatedFetch.mockResolvedValue({ ok: true, blob: () => Promise.resolve(blob) });

    render(logoTree(serverInfo("/v1/branding/logo/embed")));

    expect(screen.getByTestId("otto-eyes")).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: "Acme Agent" })).toHaveAttribute(
      "src",
      "blob:brand-logo",
    );
    expect(authenticatedFetch).toHaveBeenCalledWith("/v1/branding/logo/embed");
    expect(createObjectURL).toHaveBeenCalledWith(blob);
  });

  it("keeps the mascot fallback when the embedded fetch fails", async () => {
    getOmnigentHostConfig.mockReturnValue({ fetcher: () => {} });
    authenticatedFetch.mockResolvedValue({ ok: false, status: 404 });

    render(logoTree(serverInfo("/v1/branding/logo/missing")));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledOnce());
    expect(screen.getByTestId("otto-eyes")).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Acme Agent" })).not.toBeInTheDocument();
  });

  it("falls back and releases the URL when embedded image decoding fails", async () => {
    getOmnigentHostConfig.mockReturnValue({ fetcher: () => {} });
    authenticatedFetch.mockResolvedValue({
      ok: true,
      blob: () => Promise.resolve(new Blob(["not an image"])),
    });
    render(logoTree(serverInfo("/v1/branding/logo/broken")));
    const image = await screen.findByRole("img", { name: "Acme Agent" });

    fireEvent.error(image);

    expect(screen.getByTestId("otto-eyes")).toBeInTheDocument();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:brand-logo");
  });

  it("deduplicates concurrent consumers and revokes after the last release", async () => {
    getOmnigentHostConfig.mockReturnValue({ fetcher: () => {} });
    authenticatedFetch.mockResolvedValue({
      ok: true,
      blob: () => Promise.resolve(new Blob(["logo"])),
    });
    const first = render(logoTree(serverInfo("/v1/branding/logo/shared")));
    const second = render(logoTree(serverInfo("/v1/branding/logo/shared")));
    await waitFor(() => expect(screen.getAllByRole("img", { name: "Acme Agent" })).toHaveLength(2));

    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    first.unmount();
    expect(revokeObjectURL).not.toHaveBeenCalled();
    second.unmount();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:brand-logo");
  });

  it("revokes a URL that resolves after its consumer unmounts", async () => {
    getOmnigentHostConfig.mockReturnValue({ fetcher: () => {} });
    const blobResult = deferred<Blob>();
    authenticatedFetch.mockResolvedValue({ ok: true, blob: () => blobResult.promise });
    const view = render(logoTree(serverInfo("/v1/branding/logo/slow")));
    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledOnce());

    view.unmount();
    blobResult.resolve(new Blob(["late"]));

    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith("blob:brand-logo"));
  });

  it("releases the old URL and fetches again when the path changes", async () => {
    getOmnigentHostConfig.mockReturnValue({ fetcher: () => {} });
    createObjectURL.mockReturnValueOnce("blob:first").mockReturnValueOnce("blob:second");
    authenticatedFetch.mockResolvedValue({
      ok: true,
      blob: () => Promise.resolve(new Blob(["logo"])),
    });
    const view = render(logoTree(serverInfo("/v1/branding/logo/first")));
    await screen.findByRole("img", { name: "Acme Agent" });

    view.rerender(logoTree(serverInfo("/v1/branding/logo/second")));

    await waitFor(() =>
      expect(screen.getByRole("img", { name: "Acme Agent" })).toHaveAttribute("src", "blob:second"),
    );
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:first");
    expect(authenticatedFetch).toHaveBeenCalledTimes(2);
  });

  it("isolates identical paths across host generations", async () => {
    getOmnigentHostConfig.mockReturnValue({ fetcher: () => {} });
    createObjectURL.mockReturnValueOnce("blob:host-a").mockReturnValueOnce("blob:host-b");
    authenticatedFetch.mockResolvedValue({
      ok: true,
      blob: () => Promise.resolve(new Blob(["logo"])),
    });
    const info = serverInfo("/v1/branding/logo/main");
    const view = render(logoTree(info));
    await screen.findByRole("img", { name: "Acme Agent" });

    hostGeneration = 1;
    view.rerender(logoTree(info));

    await waitFor(() =>
      expect(screen.getByRole("img", { name: "Acme Agent" })).toHaveAttribute("src", "blob:host-b"),
    );
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:host-a");
    expect(authenticatedFetch).toHaveBeenCalledTimes(2);
  });
});
