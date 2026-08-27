import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fetchExtensionCatalog } from "./catalog";
import { ExtensionProvider, loadExtensionCatalog, useExtensions } from "./ExtensionProvider";

vi.mock("./catalog", () => ({
  EXTENSIONS_QUERY_KEY: ["extensions"],
  fetchExtensionCatalog: vi.fn(),
}));

function Consumer() {
  return <span>extensions:{useExtensions().length}</span>;
}

function renderProvider() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ExtensionProvider>
        <Consumer />
      </ExtensionProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => vi.mocked(fetchExtensionCatalog).mockReset());

describe("ExtensionProvider", () => {
  it("publishes the loaded catalog", async () => {
    vi.mocked(fetchExtensionCatalog).mockResolvedValue([]);
    renderProvider();

    expect(await screen.findByText("extensions:0")).toBeInTheDocument();
    await waitFor(() => expect(fetchExtensionCatalog).toHaveBeenCalledOnce());
  });

  it("degrades a catalog failure once without unmounting the app", async () => {
    const warning = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const failingFetcher = vi.fn(async () => {
      throw new Error("offline");
    });

    await expect(loadExtensionCatalog(undefined, failingFetcher)).resolves.toEqual([]);

    expect(failingFetcher).toHaveBeenCalledOnce();
    expect(warning).toHaveBeenCalledOnce();
  });
});
