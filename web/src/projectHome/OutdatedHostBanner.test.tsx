import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as identity from "@/lib/identity";
import { hostUpdateCommand, OutdatedHostBanner } from "./OutdatedHostBanner";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof identity>()),
  authenticatedFetch: vi.fn(),
}));

function respond(session: Record<string, unknown>) {
  vi.mocked(identity.authenticatedFetch).mockResolvedValue(
    new Response(JSON.stringify({ status: "ok", session }), { status: 200 }),
  );
}

function renderBanner() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <OutdatedHostBanner sessionId="lead" name="the Lead" />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("OutdatedHostBanner", () => {
  it("tells the owner how to update a host without the board tools", async () => {
    respond({
      host_project_tools: false,
      host_version: "0.23.0",
      host_install_spec: "git+https://git.example.com/omnigent.git@project-build",
    });
    renderBanner();
    expect(await screen.findByText("The Lead's computer needs an Omnigent update")).toBeTruthy();
    const banner = screen.getByTestId("outdated-host-banner");
    expect(banner.textContent).toContain("(0.23.0)");
    expect(banner.textContent).toContain(
      "uv tool install --force --python 3.12 'git+https://git.example.com/omnigent.git@project-build'",
    );
    expect(banner.textContent).toContain("omnigent host --server");
    expect(identity.authenticatedFetch).toHaveBeenCalledWith("/health?session_id=lead");
  });

  it.each([true, null])("stays hidden when the host's board tools are %s", async (value) => {
    respond({ host_project_tools: value });
    renderBanner();
    await vi.waitFor(() => expect(identity.authenticatedFetch).toHaveBeenCalled());
    expect(screen.queryByTestId("outdated-host-banner")).toBeNull();
  });

  it("builds a pasteable command", () => {
    expect(hostUpdateCommand("omnigent", "https://omnigent.example.com")).toBe(
      "uv tool install --force --python 3.12 'omnigent'\nomnigent host --server 'https://omnigent.example.com'",
    );
  });
});
