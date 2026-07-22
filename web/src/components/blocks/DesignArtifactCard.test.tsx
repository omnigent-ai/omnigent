import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { DesignArtifactCard, parseDesignArtifactResult } from "./DesignArtifactCard";

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
  ])("rejects %s", (_label, output) => {
    expect(
      parseDesignArtifactResult({ entry_path: "artifacts/revenue-dashboard/index.html" }, output),
    ).toBeNull();
  });
});

describe("DesignArtifactCard", () => {
  it("renders the reviewed artifact metadata", () => {
    const data = parseDesignArtifactResult(
      { entry_path: "artifacts/revenue-dashboard/index.html" },
      VALID_OUTPUT,
    );
    expect(data).not.toBeNull();

    render(<DesignArtifactCard data={data!} />);

    expect(screen.getByTestId("design-artifact-card")).toBeDefined();
    expect(screen.getByText("Revenue dashboard")).toBeDefined();
    expect(screen.getByText("artifacts/revenue-dashboard/index.html")).toBeDefined();
    expect(screen.getByText("4 files")).toBeDefined();
    expect(screen.getByText("Created")).toBeDefined();
  });
});
