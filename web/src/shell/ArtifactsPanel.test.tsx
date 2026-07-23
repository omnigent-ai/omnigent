import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useManagedArtifacts", () => ({ useManagedArtifacts: vi.fn() }));
vi.mock("@/hooks/useArtifactPreview", () => ({ useArtifactPreview: vi.fn() }));
vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/nativeBridge")>()),
  hasNativeArtifactInspector: vi.fn(() => false),
  inspectNativeArtifactSurface: vi.fn().mockResolvedValue(true),
}));

import { useArtifactPreview } from "@/hooks/useArtifactPreview";
import { useManagedArtifacts } from "@/hooks/useManagedArtifacts";
import { hasNativeArtifactInspector, inspectNativeArtifactSurface } from "@/lib/nativeBridge";
import { artifactEntriesFromFiles, ArtifactsPanel } from "./ArtifactsPanel";

const managedArtifactsMock = vi.mocked(useManagedArtifacts);
const artifactPreviewMock = vi.mocked(useArtifactPreview);
const hasNativeArtifactInspectorMock = vi.mocked(hasNativeArtifactInspector);
const inspectNativeArtifactSurfaceMock = vi.mocked(inspectNativeArtifactSurface);

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

  it("offers the native inspect-element chooser in Electron", () => {
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

    fireEvent.click(screen.getByRole("button", { name: "Inspect artifact element" }));
    expect(inspectNativeArtifactSurfaceMock).toHaveBeenCalledWith(
      expect.stringContaining("artifact-surface-"),
    );
  });
});
