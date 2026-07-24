import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useManagedArtifacts", () => ({ useManagedArtifacts: vi.fn() }));
vi.mock("@/hooks/useArtifactPreview", () => ({ useArtifactPreview: vi.fn() }));
vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/nativeBridge")>()),
  hasNativeArtifactInspector: vi.fn(() => false),
  hasNativeArtifactReview: vi.fn(() => false),
  hasNativeArtifactSurface: vi.fn(() => false),
  inspectNativeArtifactSurface: vi.fn().mockResolvedValue(true),
  reloadNativeArtifactSurface: vi.fn().mockResolvedValue(true),
  reviewNativeArtifactSurface: vi.fn().mockResolvedValue(null),
  selectNativeArtifactElement: vi.fn().mockResolvedValue(null),
}));

import { useArtifactPreview } from "@/hooks/useArtifactPreview";
import { useManagedArtifacts } from "@/hooks/useManagedArtifacts";
import {
  hasNativeArtifactInspector,
  hasNativeArtifactReview,
  hasNativeArtifactSurface,
  inspectNativeArtifactSurface,
  reloadNativeArtifactSurface,
  reviewNativeArtifactSurface,
  selectNativeArtifactElement,
} from "@/lib/nativeBridge";
import { useChatStore } from "@/store/chatStore";
import { artifactEntriesFromFiles, ArtifactsPanel } from "./ArtifactsPanel";

const managedArtifactsMock = vi.mocked(useManagedArtifacts);
const artifactPreviewMock = vi.mocked(useArtifactPreview);
const hasNativeArtifactInspectorMock = vi.mocked(hasNativeArtifactInspector);
const hasNativeArtifactReviewMock = vi.mocked(hasNativeArtifactReview);
const hasNativeArtifactSurfaceMock = vi.mocked(hasNativeArtifactSurface);
const inspectNativeArtifactSurfaceMock = vi.mocked(inspectNativeArtifactSurface);
const reloadNativeArtifactSurfaceMock = vi.mocked(reloadNativeArtifactSurface);
const reviewNativeArtifactSurfaceMock = vi.mocked(reviewNativeArtifactSurface);
const selectNativeArtifactElementMock = vi.mocked(selectNativeArtifactElement);

beforeEach(() => {
  hasNativeArtifactInspectorMock.mockReturnValue(false);
  hasNativeArtifactReviewMock.mockReturnValue(false);
  hasNativeArtifactSurfaceMock.mockReturnValue(false);
  inspectNativeArtifactSurfaceMock.mockClear();
  reloadNativeArtifactSurfaceMock.mockClear();
  reviewNativeArtifactSurfaceMock.mockReset().mockResolvedValue(null);
  selectNativeArtifactElementMock.mockReset().mockResolvedValue(null);
  useChatStore.setState({ conversationId: "conv_preview", pendingComposerInserts: [] });
});

describe("artifactEntriesFromFiles", () => {
  it("keeps only canonical standalone and directory HTML entries", () => {
    expect(
      artifactEntriesFromFiles([
        {
          path: "artifacts/overview.html",
          name: "overview.html",
          type: "file",
          bytes: 100,
          modified_at: 1,
        },
        {
          path: "artifacts/revenue/index.html",
          name: "index.html",
          type: "file",
          bytes: 200,
          modified_at: 2,
        },
        {
          path: "artifacts/revenue/assets/help.html",
          name: "help.html",
          type: "file",
          bytes: 50,
          modified_at: 3,
        },
        {
          path: "docs/example.html",
          name: "example.html",
          type: "file",
          bytes: 50,
          modified_at: 4,
        },
      ]),
    ).toEqual([
      { entryPath: "artifacts/overview.html", title: "Overview", modifiedAt: 1 },
      { entryPath: "artifacts/revenue/index.html", title: "Revenue", modifiedAt: 2 },
    ]);
  });

  it("renders the selected artifact in the dedicated-origin sandbox", () => {
    managedArtifactsMock.mockReturnValue({
      data: [
        {
          path: "artifacts/revenue/index.html",
          name: "index.html",
          type: "file",
          bytes: 200,
          modified_at: 2,
        },
      ],
      isLoading: false,
      isError: false,
    } as ReturnType<typeof useManagedArtifacts>);
    artifactPreviewMock.mockReturnValue({
      data: {
        url: "http://preview.localhost:6767/p/grant/artifacts/revenue/index.html",
        expires_at: 1234,
      },
      isLoading: false,
      isError: false,
    } as ReturnType<typeof useArtifactPreview>);

    render(
      <ArtifactsPanel
        conversationId="conv_preview"
        selectedPath="artifacts/revenue/index.html"
        onSelect={vi.fn()}
      />,
    );

    const frame = screen.getByTitle("Revenue preview");
    expect(frame.getAttribute("src")).toContain("preview.localhost");
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts allow-same-origin");
    expect(screen.getByRole("combobox", { name: "Select artifact" })).toBeDefined();
    expect(screen.queryByText("Design artifacts")).toBeNull();
    expect(screen.queryByText("Managed by Omnigent")).toBeNull();
  });

  it("switches artifacts from the compact selector", () => {
    managedArtifactsMock.mockReturnValue({
      data: [
        {
          path: "artifacts/overview.html",
          name: "overview.html",
          type: "file",
          bytes: 100,
          modified_at: 1,
        },
        {
          path: "artifacts/revenue/index.html",
          name: "index.html",
          type: "file",
          bytes: 200,
          modified_at: 2,
        },
      ],
      isLoading: false,
      isError: false,
    } as ReturnType<typeof useManagedArtifacts>);
    artifactPreviewMock.mockReturnValue({
      data: {
        url: "http://preview.localhost:6767/p/grant/artifacts/revenue/index.html",
        expires_at: 1234,
      },
      isLoading: false,
      isError: false,
    } as ReturnType<typeof useArtifactPreview>);
    const onSelect = vi.fn();

    render(
      <ArtifactsPanel
        conversationId="conv_preview"
        selectedPath="artifacts/revenue/index.html"
        onSelect={onSelect}
      />,
    );

    const selector = screen.getByRole("combobox", { name: "Select artifact" });
    selector.focus();
    fireEvent.keyDown(selector, { key: "ArrowDown" });
    fireEvent.click(screen.getByRole("option", { name: "Overview" }));

    expect(onSelect).toHaveBeenCalledWith("artifacts/overview.html");
  });

  it("defaults to the most recently modified artifact", () => {
    managedArtifactsMock.mockReturnValue({
      data: [
        {
          path: "artifacts/older.html",
          name: "older.html",
          type: "file",
          bytes: 100,
          modified_at: 10,
        },
        {
          path: "artifacts/newest/index.html",
          name: "index.html",
          type: "file",
          bytes: 200,
          modified_at: 30,
        },
        {
          path: "artifacts/middle.html",
          name: "middle.html",
          type: "file",
          bytes: 150,
          modified_at: 20,
        },
      ],
      isLoading: false,
      isError: false,
    } as ReturnType<typeof useManagedArtifacts>);
    artifactPreviewMock.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: false,
    } as ReturnType<typeof useArtifactPreview>);
    const onSelect = vi.fn();

    render(
      <ArtifactsPanel conversationId="conv_preview" selectedPath={null} onSelect={onSelect} />,
    );

    expect(onSelect).toHaveBeenCalledWith("artifacts/newest/index.html");
  });

  it("replaces an invalid saved selection with the newest artifact", () => {
    managedArtifactsMock.mockReturnValue({
      data: [
        {
          path: "artifacts/current/index.html",
          name: "index.html",
          type: "file",
          bytes: 200,
          modified_at: 40,
        },
      ],
      isLoading: false,
      isError: false,
    } as ReturnType<typeof useManagedArtifacts>);
    artifactPreviewMock.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: false,
    } as ReturnType<typeof useArtifactPreview>);
    const onSelect = vi.fn();

    render(
      <ArtifactsPanel
        conversationId="conv_preview"
        selectedPath="artifacts/deleted/index.html"
        onSelect={onSelect}
      />,
    );

    expect(onSelect).toHaveBeenCalledWith("artifacts/current/index.html");
  });

  it("keeps an existing valid artifact selected", () => {
    managedArtifactsMock.mockReturnValue({
      data: [
        {
          path: "artifacts/selected.html",
          name: "selected.html",
          type: "file",
          bytes: 100,
          modified_at: 10,
        },
        {
          path: "artifacts/newest.html",
          name: "newest.html",
          type: "file",
          bytes: 200,
          modified_at: 20,
        },
      ],
      isLoading: false,
      isError: false,
    } as ReturnType<typeof useManagedArtifacts>);
    artifactPreviewMock.mockReturnValue({
      data: {
        url: "http://preview.localhost:6767/p/grant/artifacts/selected.html",
        expires_at: 1234,
      },
      isLoading: false,
      isError: false,
    } as ReturnType<typeof useArtifactPreview>);
    const onSelect = vi.fn();

    render(
      <ArtifactsPanel
        conversationId="conv_preview"
        selectedPath="artifacts/selected.html"
        onSelect={onSelect}
      />,
    );

    expect(onSelect).not.toHaveBeenCalled();
    expect(screen.getByTitle("Selected preview")).toBeDefined();
  });

  it("offers the native inspect-element chooser with a visible tooltip in Electron", async () => {
    hasNativeArtifactInspectorMock.mockReturnValue(true);
    managedArtifactsMock.mockReturnValue({
      data: [
        {
          path: "artifacts/revenue/index.html",
          name: "index.html",
          type: "file",
          bytes: 200,
          modified_at: 2,
        },
      ],
      isLoading: false,
      isError: false,
    } as ReturnType<typeof useManagedArtifacts>);
    artifactPreviewMock.mockReturnValue({
      data: {
        url: "http://preview.localhost:6767/p/grant/artifacts/revenue/index.html",
        expires_at: 1234,
      },
      isLoading: false,
      isError: false,
    } as ReturnType<typeof useArtifactPreview>);

    render(
      <ArtifactsPanel
        conversationId="conv_preview"
        selectedPath="artifacts/revenue/index.html"
        onSelect={vi.fn()}
      />,
    );

    const inspectButton = screen.getByRole("button", {
      name: "Open artifact DevTools inspector",
    });
    fireEvent.pointerMove(inspectButton.parentElement!, { pointerType: "mouse" });
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip).toHaveTextContent("Inspect in DevTools");
    expect(tooltip.closest('[data-side="top"]')).not.toBeNull();
    fireEvent.click(inspectButton);
    expect(inspectNativeArtifactSurfaceMock).toHaveBeenCalledWith(
      expect.stringContaining("artifact-surface-"),
    );
  });

  it("captures element feedback and queues it with a screenshot for the composer", async () => {
    hasNativeArtifactSurfaceMock.mockReturnValue(true);
    selectNativeArtifactElementMock.mockResolvedValue({
      selector: "main > button.primary",
      tagName: "button",
      role: "button",
      accessibleName: "Save changes",
      text: "Save",
      html: '<button class="primary">Save</button>',
      rect: { x: 12, y: 24, width: 120, height: 40 },
      viewport: { width: 1280, height: 800, devicePixelRatio: 1 },
      styles: { display: "inline-flex", color: "rgb(0, 0, 0)" },
      screenshotDataUrl: "data:image/png;base64,aW1hZ2U=",
    });
    managedArtifactsMock.mockReturnValue({
      data: [
        {
          path: "artifacts/revenue/index.html",
          name: "index.html",
          type: "file",
          bytes: 200,
          modified_at: 2,
        },
      ],
      isLoading: false,
      isError: false,
    } as ReturnType<typeof useManagedArtifacts>);
    artifactPreviewMock.mockReturnValue({
      data: {
        url: "http://preview.localhost:6767/p/grant/artifacts/revenue/index.html",
        expires_at: 1234,
      },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof useArtifactPreview>);

    render(
      <ArtifactsPanel
        conversationId="conv_preview"
        selectedPath="artifacts/revenue/index.html"
        onSelect={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Select artifact element for feedback" }));
    const note = await screen.findByRole("textbox", {
      name: "Feedback for main > button.primary",
    });
    fireEvent.change(note, { target: { value: "Make this the clear primary action." } });
    fireEvent.click(screen.getByRole("button", { name: "Add 1 annotation to prompt" }));

    const queued = useChatStore.getState().pendingComposerInserts;
    expect(queued).toHaveLength(1);
    expect(queued[0]?.conversationId).toBe("conv_preview");
    expect(queued[0]?.text).toContain("main > button.primary");
    expect(queued[0]?.text).toContain("Make this the clear primary action.");
    expect(queued[0]?.files[0]?.name).toBe("revenue-annotation-1.png");
  });

  it("switches the real preview surface to a mobile viewport", () => {
    managedArtifactsMock.mockReturnValue({
      data: [
        {
          path: "artifacts/revenue/index.html",
          name: "index.html",
          type: "file",
          bytes: 200,
          modified_at: 2,
        },
      ],
      isLoading: false,
      isError: false,
    } as ReturnType<typeof useManagedArtifacts>);
    artifactPreviewMock.mockReturnValue({
      data: {
        url: "http://preview.localhost:6767/p/grant/artifacts/revenue/index.html",
        expires_at: 1234,
      },
      isLoading: false,
      isError: false,
    } as ReturnType<typeof useArtifactPreview>);

    render(
      <ArtifactsPanel
        conversationId="conv_preview"
        selectedPath="artifacts/revenue/index.html"
        onSelect={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Mobile viewport" }));
    expect(screen.getByTestId("artifact-preview-viewport")).toHaveStyle({ width: "390px" });
  });

  it("does not clamp the tablet preview to the artifacts pane width", () => {
    managedArtifactsMock.mockReturnValue({
      data: [
        {
          path: "artifacts/revenue/index.html",
          name: "index.html",
          type: "file",
          bytes: 200,
          modified_at: 2,
        },
      ],
      isLoading: false,
      isError: false,
    } as ReturnType<typeof useManagedArtifacts>);
    artifactPreviewMock.mockReturnValue({
      data: {
        url: "http://preview.localhost:6767/p/grant/artifacts/revenue/index.html",
        expires_at: 1234,
      },
      isLoading: false,
      isError: false,
    } as ReturnType<typeof useArtifactPreview>);

    render(
      <ArtifactsPanel
        conversationId="conv_preview"
        selectedPath="artifacts/revenue/index.html"
        onSelect={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Tablet viewport" }));
    const viewport = screen.getByTestId("artifact-preview-viewport");
    expect(viewport).toHaveStyle({ width: "768px" });
    expect(viewport).not.toHaveClass("max-w-full");
    expect(viewport).toHaveClass("mx-auto", "shrink-0");
    expect(viewport.parentElement).not.toHaveClass("justify-center");
  });

  it("refreshes and reviews the active native artifact", async () => {
    hasNativeArtifactSurfaceMock.mockReturnValue(true);
    hasNativeArtifactReviewMock.mockReturnValue(true);
    const refetch = vi.fn().mockResolvedValue(undefined);
    reviewNativeArtifactSurfaceMock.mockResolvedValue({
      viewport: { width: 1280, height: 800 },
      issues: [
        {
          severity: "error",
          code: "missing-label",
          message: "Button has no accessible name",
          selector: "main > button:nth-of-type(2)",
        },
      ],
      consoleMessages: [{ level: "error", message: "Boom", line: 10, source: "app.js" }],
      loadErrors: [],
    });
    managedArtifactsMock.mockReturnValue({
      data: [
        {
          path: "artifacts/revenue/index.html",
          name: "index.html",
          type: "file",
          bytes: 200,
          modified_at: 22,
        },
      ],
      isLoading: false,
      isError: false,
    } as ReturnType<typeof useManagedArtifacts>);
    artifactPreviewMock.mockReturnValue({
      data: {
        url: "http://preview.localhost:6767/p/grant/artifacts/revenue/index.html",
        expires_at: 1234,
      },
      isLoading: false,
      isError: false,
      refetch,
    } as unknown as ReturnType<typeof useArtifactPreview>);

    render(
      <ArtifactsPanel
        conversationId="conv_preview"
        selectedPath="artifacts/revenue/index.html"
        onSelect={vi.fn()}
      />,
    );

    expect(artifactPreviewMock).toHaveBeenCalledWith(
      "conv_preview",
      "artifacts/revenue/index.html",
      22,
    );
    fireEvent.click(screen.getByRole("button", { name: "Refresh artifact preview" }));
    await waitFor(() => expect(refetch).toHaveBeenCalled());
    expect(reloadNativeArtifactSurfaceMock).toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Review artifact" }));
    expect(await screen.findByText("1 accessibility issue")).toBeDefined();
    expect(screen.getByText("1 console error")).toBeDefined();
    expect(screen.getByText("Button has no accessible name")).toBeDefined();
  });
});
