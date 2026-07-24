import { act, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ArtifactPreviewSurface } from "./ArtifactPreviewSurface";

const syncArtifactSurface = vi.fn().mockResolvedValue(true);
const destroyArtifactSurface = vi.fn().mockResolvedValue(undefined);

class ResizeObserverMock {
  private readonly callback: ResizeObserverCallback;

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
  }
  observe(element: Element) {
    this.callback([{ target: element } as ResizeObserverEntry], this);
  }
  disconnect() {}
  unobserve() {}
}

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  vi.restoreAllMocks();
  syncArtifactSurface.mockClear();
  destroyArtifactSurface.mockClear();
});

describe("ArtifactPreviewSurface", () => {
  it("renders the sandboxed iframe in a normal browser", () => {
    render(
      <ArtifactPreviewSurface
        surfaceId="surface"
        title="Revenue"
        url="http://preview.localhost/p/grant/a"
        visible
      />,
    );
    const frame = screen.getByTitle("Revenue preview");
    expect(frame.tagName).toBe("IFRAME");
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts allow-same-origin");
  });

  it("uses the iframe fallback in macOS Electron", () => {
    vi.spyOn(window.navigator, "userAgent", "get").mockReturnValue(
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    );
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      setBadgeCount: vi.fn(),
      notify: vi.fn(),
      syncArtifactSurface,
      destroyArtifactSurface,
    };

    render(
      <ArtifactPreviewSurface
        surfaceId="surface"
        title="Revenue"
        url="http://preview.localhost/p/grant/a"
        visible
      />,
    );

    expect(screen.getByTitle("Revenue preview").tagName).toBe("IFRAME");
    expect(syncArtifactSurface).not.toHaveBeenCalled();
    expect(destroyArtifactSurface).not.toHaveBeenCalled();
  });

  it("creates the native surface once across a StrictMode mount cycle", async () => {
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      setBadgeCount: vi.fn(),
      notify: vi.fn(),
      syncArtifactSurface,
      destroyArtifactSurface,
    };
    vi.stubGlobal("ResizeObserver", ResizeObserverMock);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      x: 12,
      y: 34,
      width: 500,
      height: 360,
      top: 34,
      right: 512,
      bottom: 394,
      left: 12,
      toJSON: () => ({}),
    });

    const { unmount } = render(
      <StrictMode>
        <ArtifactPreviewSurface
          surfaceId="surface"
          title="Revenue"
          url="http://preview.localhost/p/grant/a"
          visible
        />
      </StrictMode>,
    );

    await waitFor(() => expect(syncArtifactSurface).toHaveBeenCalledTimes(2));
    expect(destroyArtifactSurface).toHaveBeenCalledTimes(1);
    act(() => unmount());
    expect(destroyArtifactSurface).toHaveBeenCalledTimes(2);
  });

  it("syncs a native surface instead of rendering an iframe in Electron", async () => {
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      setBadgeCount: vi.fn(),
      notify: vi.fn(),
      syncArtifactSurface,
      destroyArtifactSurface,
    };
    vi.stubGlobal("ResizeObserver", ResizeObserverMock);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      x: 12,
      y: 34,
      width: 500,
      height: 360,
      top: 34,
      right: 512,
      bottom: 394,
      left: 12,
      toJSON: () => ({}),
    });

    const { rerender, unmount } = render(
      <ArtifactPreviewSurface
        surfaceId="surface"
        title="Revenue"
        url="http://preview.localhost/p/grant/a"
        visible
      />,
    );

    expect(screen.queryByTitle("Revenue preview")).toBeNull();
    await waitFor(() =>
      expect(syncArtifactSurface).toHaveBeenCalledWith(
        expect.objectContaining({
          url: "http://preview.localhost/p/grant/a",
          visible: true,
          bounds: { x: 12, y: 34, width: 500, height: 360 },
        }),
      ),
    );

    rerender(
      <ArtifactPreviewSurface
        surfaceId="surface"
        title="Revenue"
        url="http://preview.localhost/p/grant/a"
        visible={false}
      />,
    );
    await waitFor(() =>
      expect(syncArtifactSurface).toHaveBeenLastCalledWith(
        expect.objectContaining({ visible: false }),
      ),
    );
    expect(destroyArtifactSurface).not.toHaveBeenCalled();

    act(() => unmount());
    expect(destroyArtifactSurface).toHaveBeenCalled();
  });
});
