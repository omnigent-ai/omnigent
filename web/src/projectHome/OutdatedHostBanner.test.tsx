import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as identity from "@/lib/identity";
import { hostUpdateCommand, OutdatedHostBanner } from "./OutdatedHostBanner";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof identity>()),
  authenticatedFetch: vi.fn(),
}));

function respond(update: Record<string, unknown>) {
  vi.mocked(identity.authenticatedFetch).mockImplementation(
    async () => new Response(JSON.stringify(update), { status: 200 }),
  );
}

function renderBanner() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <OutdatedHostBanner sessionId="lead" name="the Lead" />
    </QueryClientProvider>,
  );
}

const OUTDATED = {
  needed: true,
  host_id: "host-a",
  host_version: "0.24.0.dev0",
  install_spec: "git+https://git.example.com/omnigent.git@project-build",
};

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
  window.localStorage.clear();
});

describe("OutdatedHostBanner", () => {
  it("is a compact notice with the fix behind a disclosure", async () => {
    respond(OUTDATED);
    renderBanner();
    expect(await screen.findByText(/The Lead's computer needs an Omnigent update/)).toBeTruthy();
    const banner = screen.getByTestId("outdated-host-banner");
    expect(banner.textContent).toContain("(0.24.0.dev0)");
    expect(banner.textContent).not.toContain("uv tool install");
    fireEvent.click(screen.getByRole("button", { name: /How to fix/ }));
    expect(banner.textContent).toContain(
      "uv tool install --force --python 3.12 'git+https://git.example.com/omnigent.git@project-build'",
    );
    expect(banner.textContent).toContain("the Lead's tickets");
    expect(banner.textContent).toContain("leave that host running");
    expect(identity.authenticatedFetch).toHaveBeenCalledWith("/v1/sessions/lead/host-update");
  });

  it("stays hidden when no update is needed", async () => {
    respond({ needed: false, host_id: "host-a" });
    renderBanner();
    await vi.waitFor(() => expect(identity.authenticatedFetch).toHaveBeenCalled());
    expect(screen.queryByTestId("outdated-host-banner")).toBeNull();
  });

  it("stays dismissed for that host build, and returns for a different one", async () => {
    respond(OUTDATED);
    renderBanner();
    fireEvent.click(await screen.findByRole("button", { name: "Dismiss update notice" }));
    expect(screen.queryByTestId("outdated-host-banner")).toBeNull();
    cleanup();
    renderBanner();
    await vi.waitFor(() => expect(identity.authenticatedFetch).toHaveBeenCalledTimes(2));
    expect(screen.queryByTestId("outdated-host-banner")).toBeNull();
    cleanup();
    respond({ ...OUTDATED, host_version: "0.25.0" });
    renderBanner();
    expect(await screen.findByTestId("outdated-host-banner")).toBeTruthy();
  });

  it("builds commands that restart the host from the uv-installed build", () => {
    expect(hostUpdateCommand("omnigent", "https://omnigent.example.com")).toBe(
      [
        "uv tool install --force --python 3.12 'omnigent'",
        'export OMNIGENT_WRAPPER_BYPASS=1; omnigent="$(uv tool dir --bin)/omnigent"',
        "\"$omnigent\" host stop --daemon-only --server 'https://omnigent.example.com'",
        "\"$omnigent\" host --background --server 'https://omnigent.example.com'",
        '"$omnigent" host status --all',
      ].join("\n"),
    );
  });
});
