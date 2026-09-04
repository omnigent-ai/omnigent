import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EXTENSION_RPC_SOURCE, EXTENSION_RPC_VERSION } from "./rpc/protocol";
import type { ExtensionCatalogItem, ExtensionPage } from "./types";

const { loadBundleMock, buildDocumentMock } = vi.hoisted(() => ({
  loadBundleMock: vi.fn(),
  buildDocumentMock: vi.fn(),
}));

vi.mock("./rpc/host", () => ({
  createExtensionNonce: () => "nonce",
  loadExtensionBundle: loadBundleMock,
  buildExtensionDocument: buildDocumentMock,
}));

import { ExtensionViewHost } from "./ExtensionViewHost";

const page: ExtensionPage = {
  id: "acme.review.dashboard",
  title: "Dashboard",
  route: "dashboard",
  view: "dashboard",
};
const extension = {
  id: "acme.review",
  browser: { digest: "digest", script_url: "/script" },
} as ExtensionCatalogItem;
const identity = {
  extensionId: extension.id,
  pageId: page.id,
  view: page.view,
  nonce: "nonce",
  apiVersion: EXTENSION_RPC_VERSION,
};

class FakePort {
  onmessage: ((event: MessageEvent<unknown>) => void) | null = null;
  postMessage = vi.fn();
  start = vi.fn();
  close = vi.fn();
}

class FakeMessageChannel {
  static latest: FakeMessageChannel | null = null;
  port1 = new FakePort();
  port2 = new FakePort();

  constructor() {
    FakeMessageChannel.latest = this;
  }
}

const refresh = vi.fn(async () => [extension]);

beforeEach(() => {
  vi.stubGlobal("MessageChannel", FakeMessageChannel);
  loadBundleMock.mockReset().mockResolvedValue({ extension, script: "", styles: "" });
  buildDocumentMock.mockReset().mockReturnValue({ srcDoc: "<html></html>", identity });
  refresh.mockClear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("ExtensionViewHost", () => {
  it("uses an opaque sandbox and becomes ready only after the nonce-bound handshake", async () => {
    render(<ExtensionViewHost extension={extension} page={page} refresh={refresh} />);
    const iframe = await screen.findByTitle("Dashboard");
    expect(iframe).toHaveAttribute("sandbox", "allow-scripts");
    expect(iframe).toHaveAttribute("allow", "");
    expect(iframe).toHaveClass("min-h-0", "flex-1");
    expect(iframe.parentElement).toHaveClass("extension-view-host", "pt-14", "md:pt-12");
    expect(screen.getByRole("status")).toHaveTextContent("Starting extension");
    await waitFor(() => expect(FakeMessageChannel.latest).not.toBeNull());
    act(() => {
      FakeMessageChannel.latest!.port1.onmessage?.({
        data: { ...identity, source: EXTENSION_RPC_SOURCE, type: "ready" },
      } as MessageEvent<unknown>);
    });
    await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
  });

  it("rejects an incompatible extension SDK explicitly", async () => {
    render(<ExtensionViewHost extension={extension} page={page} refresh={refresh} />);
    await screen.findByTitle("Dashboard");
    await waitFor(() => expect(FakeMessageChannel.latest).not.toBeNull());

    act(() => {
      FakeMessageChannel.latest!.port1.onmessage?.({
        data: {
          ...identity,
          source: EXTENSION_RPC_SOURCE,
          type: "incompatible",
          sdkApiVersion: 2,
        },
      } as MessageEvent<unknown>);
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "SDK API 2 is incompatible with host API 1",
    );
  });

  it("rejects a second load instead of handing out another port", async () => {
    render(<ExtensionViewHost extension={extension} page={page} refresh={refresh} />);
    const iframe = await screen.findByTitle("Dashboard");
    fireEvent.load(iframe);
    fireEvent.load(iframe);

    expect(await screen.findByRole("alert")).toHaveTextContent("reloaded during activation");
  });

  it("times out even when the iframe never finishes loading", async () => {
    vi.useFakeTimers();
    render(<ExtensionViewHost extension={extension} page={page} refresh={refresh} />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByTitle("Dashboard")).toBeInTheDocument();

    act(() => vi.advanceTimersByTime(10_000));

    expect(screen.getByRole("alert")).toHaveTextContent("activation timed out");
  });

  it("rejects prototype properties that are not registered host methods", async () => {
    render(<ExtensionViewHost extension={extension} page={page} refresh={refresh} />);
    await screen.findByTitle("Dashboard");
    await waitFor(() => expect(FakeMessageChannel.latest).not.toBeNull());

    act(() => {
      FakeMessageChannel.latest!.port1.onmessage?.({
        data: {
          ...identity,
          source: EXTENSION_RPC_SOURCE,
          type: "request",
          requestId: "prototype",
          method: "constructor",
          params: {},
        },
      } as MessageEvent<unknown>);
    });

    expect(FakeMessageChannel.latest!.port1.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        requestId: "prototype",
        error: { code: "MethodNotFound", message: "Host method is not available" },
      }),
    );
  });

  it("dispatches through the latest host method implementation", async () => {
    const light = vi.fn(() => ({ theme: "light" }));
    const dark = vi.fn(() => ({ theme: "dark" }));
    const rendered = render(
      <ExtensionViewHost
        extension={extension}
        page={page}
        refresh={refresh}
        methods={{ "theme.getCurrent": light }}
      />,
    );
    await screen.findByTitle("Dashboard");
    await waitFor(() => expect(FakeMessageChannel.latest).not.toBeNull());
    rendered.rerender(
      <ExtensionViewHost
        extension={extension}
        page={page}
        refresh={refresh}
        methods={{ "theme.getCurrent": dark }}
      />,
    );

    act(() => {
      FakeMessageChannel.latest!.port1.onmessage?.({
        data: {
          ...identity,
          source: EXTENSION_RPC_SOURCE,
          type: "request",
          requestId: "theme",
          method: "theme.getCurrent",
          params: {},
        },
      } as MessageEvent<unknown>);
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(light).not.toHaveBeenCalled();
    expect(dark).toHaveBeenCalledOnce();
    expect(FakeMessageChannel.latest!.port1.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ requestId: "theme", result: { theme: "dark" } }),
    );
  });

  it("caps concurrent host requests per extension frame", async () => {
    const pending = vi.fn(() => new Promise<void>(() => {}));
    render(
      <ExtensionViewHost
        extension={extension}
        page={page}
        refresh={refresh}
        methods={{ "test.pending": pending }}
      />,
    );
    await screen.findByTitle("Dashboard");
    await waitFor(() => expect(FakeMessageChannel.latest).not.toBeNull());

    act(() => {
      for (let index = 0; index < 33; index += 1) {
        FakeMessageChannel.latest!.port1.onmessage?.({
          data: {
            ...identity,
            source: EXTENSION_RPC_SOURCE,
            type: "request",
            requestId: `request-${index}`,
            method: "test.pending",
            params: {},
          },
        } as MessageEvent<unknown>);
      }
    });

    expect(FakeMessageChannel.latest!.port1.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        requestId: "request-32",
        error: { code: "Busy", message: "Too many extension host requests" },
      }),
    );
  });

  it("cancels outstanding host calls and sends dispose on unmount", async () => {
    const captured: { signal?: AbortSignal } = {};
    const pending = vi.fn((_params: unknown, signal: AbortSignal) => {
      captured.signal = signal;
      return new Promise<void>(() => {});
    });
    const rendered = render(
      <ExtensionViewHost
        extension={extension}
        page={page}
        refresh={refresh}
        methods={{ "test.pending": pending }}
      />,
    );
    await screen.findByTitle("Dashboard");
    await waitFor(() => expect(FakeMessageChannel.latest).not.toBeNull());
    act(() => {
      FakeMessageChannel.latest!.port1.onmessage?.({
        data: {
          ...identity,
          source: EXTENSION_RPC_SOURCE,
          type: "request",
          requestId: "request-1",
          method: "test.pending",
          params: {},
        },
      } as MessageEvent<unknown>);
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(pending).toHaveBeenCalledOnce();

    rendered.unmount();

    expect(captured.signal?.aborted).toBe(true);
    expect(FakeMessageChannel.latest!.port1.postMessage).toHaveBeenCalledWith({
      ...identity,
      source: EXTENSION_RPC_SOURCE,
      type: "dispose",
    });
    expect(FakeMessageChannel.latest!.port1.close).toHaveBeenCalledOnce();
    expect(FakeMessageChannel.latest!.port1.onmessage).toBeNull();
  });

  it("times out an unfinished host request", async () => {
    vi.useFakeTimers();
    const pending = vi.fn(() => new Promise<void>(() => {}));
    render(
      <ExtensionViewHost
        extension={extension}
        page={page}
        refresh={refresh}
        methods={{ "test.pending": pending }}
      />,
    );
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    act(() => {
      FakeMessageChannel.latest!.port1.onmessage?.({
        data: { ...identity, source: EXTENSION_RPC_SOURCE, type: "ready" },
      } as MessageEvent<unknown>);
      FakeMessageChannel.latest!.port1.onmessage?.({
        data: {
          ...identity,
          source: EXTENSION_RPC_SOURCE,
          type: "request",
          requestId: "request-1",
          method: "test.pending",
          params: {},
        },
      } as MessageEvent<unknown>);
      vi.advanceTimersByTime(10_000);
    });

    expect(FakeMessageChannel.latest!.port1.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        type: "response",
        requestId: "request-1",
        error: { code: "RequestTimeout", message: "Host request timed out" },
      }),
    );
  });

  it("does not let one failed extension prevent another from mounting", async () => {
    const healthy = { ...extension, id: "acme.healthy" };
    const healthyIdentity = { ...identity, extensionId: healthy.id };
    loadBundleMock.mockImplementation((item: ExtensionCatalogItem) =>
      item.id === extension.id
        ? Promise.reject(new Error("bundle missing"))
        : Promise.resolve({ extension: healthy, script: "", styles: "" }),
    );
    buildDocumentMock.mockImplementation((_bundle: unknown, mountedPage: ExtensionPage) => ({
      srcDoc: "<html></html>",
      identity: { ...healthyIdentity, pageId: mountedPage.id },
    }));

    render(
      <>
        <ExtensionViewHost extension={extension} page={page} refresh={refresh} />
        <ExtensionViewHost extension={healthy} page={page} refresh={refresh} />
      </>,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("bundle missing");
    expect(await screen.findByTitle("Dashboard")).toBeInTheDocument();
  });

  it("isolates bundle load failures behind an extension-local error", async () => {
    loadBundleMock.mockRejectedValue(new Error("bundle missing"));

    render(<ExtensionViewHost extension={extension} page={page} refresh={refresh} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("bundle missing");
  });
});
