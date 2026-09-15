import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fetchExtensionCatalog } from "./catalog";
import {
  ExtensionProvider,
  loadExtensionCatalog,
  useExtensions,
  useExtensionsLoading,
} from "./ExtensionProvider";

vi.mock("./catalog", () => ({
  EXTENSIONS_QUERY_KEY: ["extensions"],
  fetchExtensionCatalog: vi.fn(),
}));

function Consumer() {
  const extensions = useExtensions();
  const loading = useExtensionsLoading();
  return <span>{loading ? "loading" : `extensions:${extensions.length}`}</span>;
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

beforeEach(() => {
  vi.mocked(fetchExtensionCatalog).mockReset();
});

describe("ExtensionProvider", () => {
  it("publishes loading until the catalog resolves", async () => {
    let resolveCatalog!: (value: []) => void;
    vi.mocked(fetchExtensionCatalog).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveCatalog = resolve;
        }),
    );
    renderProvider();

    expect(screen.getByText("loading")).toBeInTheDocument();
    resolveCatalog([]);
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
    warning.mockRestore();
  });

  it("preserves query cancellation so a remount can retry", async () => {
    const warning = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const controller = new AbortController();
    const abortError = new DOMException("aborted", "AbortError");
    const cancelledFetcher = vi.fn(async () => {
      throw abortError;
    });
    controller.abort();

    await expect(loadExtensionCatalog(controller.signal, cancelledFetcher)).rejects.toBe(
      abortError,
    );

    expect(cancelledFetcher).toHaveBeenCalledOnce();
    expect(warning).not.toHaveBeenCalled();
    warning.mockRestore();
  });
});
