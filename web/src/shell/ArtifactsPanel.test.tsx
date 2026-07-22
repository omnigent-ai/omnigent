import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/hooks/useWorkspaceChangedFiles")>();
  return { ...actual, useWorkspaceFileSearch: vi.fn() };
});
vi.mock("@/hooks/useArtifactPreview", () => ({ useArtifactPreview: vi.fn() }));

import { useArtifactPreview } from "@/hooks/useArtifactPreview";
import { useWorkspaceFileSearch } from "@/hooks/useWorkspaceChangedFiles";
import { artifactEntriesFromFiles, ArtifactsPanel } from "./ArtifactsPanel";

const fileSearchMock = vi.mocked(useWorkspaceFileSearch);
const artifactPreviewMock = vi.mocked(useArtifactPreview);

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
    fileSearchMock.mockReturnValue({
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
    } as ReturnType<typeof useWorkspaceFileSearch>);
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
        onOpenFile={vi.fn()}
      />,
    );

    const frame = screen.getByTitle("Revenue preview");
    expect(frame.getAttribute("src")).toContain("preview.localhost");
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts allow-same-origin");
  });
});
