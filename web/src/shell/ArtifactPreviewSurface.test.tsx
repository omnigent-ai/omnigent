import { act, render, screen, waitFor } from "@testing-library/react";
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
