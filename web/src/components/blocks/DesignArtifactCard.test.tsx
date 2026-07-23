import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useArtifactPreview } from "@/hooks/useArtifactPreview";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { DesignArtifactCard, parseDesignArtifactResult } from "./DesignArtifactCard";

vi.mock("@/hooks/useArtifactPreview", () => ({ useArtifactPreview: vi.fn() }));

const artifactPreviewMock = vi.mocked(useArtifactPreview);

const FILE_VIEWER_CONTEXT_VALUE = {
  openFile: vi.fn(),
  isChangedPath: () => false,
  conversationId: "conv_preview",
  workspaceRoot: null,
  workspaceHome: null,
};

afterEach(cleanup);

const VALID_OUTPUT = JSON.stringify({
  ok: true,
  entry_path: "artifacts/revenue-dashboard/index.html",
  artifact_root: "artifacts/revenue-dashboard",
  title: "Revenue dashboard",
  operation: "created",
  language: "html",
  resource_count: 4,
  summary: "Revenue health and account-level drill-downs.",
});

describe("parseDesignArtifactResult", () => {
  it("accepts a successful publish result whose path matches the input", () => {
    expect(
      parseDesignArtifactResult(
        {
          entry_path: "artifacts/revenue-dashboard/index.html",
          title: "Revenue dashboard",
          operation: "created",
        },
        VALID_OUTPUT,
      ),
    ).toEqual({
      entryPath: "artifacts/revenue-dashboard/index.html",
      artifactRoot: "artifacts/revenue-dashboard",
      title: "Revenue dashboard",
      operation: "created",
      language: "html",
      resourceCount: 4,
      summary: "Revenue health and account-level drill-downs.",
    });
  });

  it.each([
    ["malformed JSON", "not-json"],
    ["failed result", JSON.stringify({ ok: false })],
    [
      "mismatched path",
      JSON.stringify({
        ...JSON.parse(VALID_OUTPUT),
        entry_path: "artifacts/other.html",
      }),
    ],
    [
      "unsupported nested non-index path",
      JSON.stringify({
        ...JSON.parse(VALID_OUTPUT),
        entry_path: "artifacts/team/dashboard.html",
        artifact_root: "artifacts/team/dashboard.html",
      }),
    ],
  ])("rejects %s", (_label, output) => {
    const inputPath =
      _label === "unsupported nested non-index path"
        ? "artifacts/team/dashboard.html"
        : "artifacts/revenue-dashboard/index.html";
    expect(parseDesignArtifactResult({ entry_path: inputPath }, output)).toBeNull();
  });
});

describe("DesignArtifactCard", () => {
  it("renders the current full-row SP2K HTML card hierarchy", () => {
    const data = parseDesignArtifactResult(
      { entry_path: "artifacts/revenue-dashboard/index.html" },
      VALID_OUTPUT,
    );
    expect(data).not.toBeNull();

    render(<DesignArtifactCard data={data!} />);

    const card = screen.getByTestId("design-artifact-card");
    expect(card.className).toContain("w-full");
    expect(card.className).toContain("bg-card");
    expect(screen.getByTestId("design-artifact-preview-well")).toBeDefined();
    expect(screen.getByText("Revenue dashboard")).toBeDefined();
    expect(screen.getByText("HTML")).toBeDefined();
    const htmlIcon = screen.getByTestId("design-artifact-html-icon");
    expect(htmlIcon.querySelectorAll("polyline")).toHaveLength(2);
    expect(htmlIcon.querySelector("path")).toBeNull();
    expect(screen.getByText("4 files")).toBeDefined();
    expect(screen.getByText("Created")).toBeDefined();
    expect(screen.queryByText("artifacts/revenue-dashboard/index.html")).toBeNull();
    expect(screen.queryByText("Revenue health and account-level drill-downs.")).toBeNull();
  });

  it("renders the real capability URL inside the recessed preview well", () => {
    artifactPreviewMock.mockReturnValue({
      data: {
        url: "http://preview.localhost:6767/p/grant/artifacts/revenue-dashboard/index.html",
        expires_at: 1234,
      },
      isLoading: false,
      isError: false,
    } as ReturnType<typeof useArtifactPreview>);
    const data = parseDesignArtifactResult(
      { entry_path: "artifacts/revenue-dashboard/index.html" },
      VALID_OUTPUT,
    );

    render(
      <FileViewerContext.Provider value={FILE_VIEWER_CONTEXT_VALUE}>
        <DesignArtifactCard data={data!} />
      </FileViewerContext.Provider>,
    );

    const frame = screen.getByTitle("Revenue dashboard card preview");
    expect(frame.getAttribute("src")).toContain("preview.localhost");
    expect(frame.getAttribute("sandbox")).toBe("allow-same-origin");
  });
});
