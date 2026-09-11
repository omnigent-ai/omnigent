import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ContextSaverCard, parseContextSaverResult } from "./ContextSaverCard";

afterEach(cleanup);

const RESULT = {
  technique: "focused_read",
  model_routing: {
    primary_models: "all",
    worker_route: "databricks/context-saver-cheap",
    worker_model_reported: "databricks-glm-5-2",
    route_provider: "databricks",
    non_databricks_source_sharing_allowed: false,
  },
  content: "The relevant code is on line 6.",
  failure: null,
};

describe("parseContextSaverResult", () => {
  it("keeps the configured route separate from the provider-reported model", () => {
    const parsed = parseContextSaverResult(JSON.stringify(RESULT));

    expect(parsed?.routing).toEqual({
      primaryModels: "all",
      workerRoute: "databricks/context-saver-cheap",
      workerModelReported: "databricks-glm-5-2",
      routeProvider: "databricks",
      nonDatabricksSourceSharingAllowed: false,
    });
  });

  it("unwraps the Claude SDK MCP text envelope", () => {
    const wrapped = JSON.stringify([{ type: "text", text: JSON.stringify(RESULT) }]);

    expect(parseContextSaverResult(wrapped)?.routing.workerRoute).toBe(
      "databricks/context-saver-cheap",
    );
  });

  it("rejects output without transparent routing metadata", () => {
    expect(parseContextSaverResult("{}")).toBeNull();
    expect(parseContextSaverResult("not json")).toBeNull();
  });
});

describe("ContextSaverCard", () => {
  it("shows the primary-to-worker route and the model reported by the provider", () => {
    render(<ContextSaverCard output={JSON.stringify(RESULT)} state="output-available" />);

    const card = screen.getByTestId("context-saver-card");
    expect(card).toHaveTextContent("Context Saver");
    expect(card).toHaveTextContent("All primary models");
    expect(card).toHaveTextContent("databricks/context-saver-cheap");
    expect(card).toHaveTextContent("databricks-glm-5-2");
    expect(card).toHaveTextContent("Route provider");
    expect(card).toHaveTextContent("databricks");
    expect(card).toHaveTextContent("your primary model does not change");
  });

  it("states when the provider does not report a backing model", () => {
    const output = JSON.stringify({
      ...RESULT,
      model_routing: { ...RESULT.model_routing, worker_model_reported: null },
    });

    render(<ContextSaverCard output={output} state="output-available" />);

    expect(screen.getByTestId("context-saver-card")).toHaveTextContent("Not reported");
  });

  it("warns when non-Databricks source sharing is allowed", () => {
    const output = JSON.stringify({
      ...RESULT,
      model_routing: {
        ...RESULT.model_routing,
        worker_route: "openai/gpt-4o-mini",
        worker_model_reported: "gpt-4o-mini-2024-07-18",
        route_provider: "openai",
        non_databricks_source_sharing_allowed: true,
      },
    });

    render(<ContextSaverCard output={output} state="output-available" />);

    expect(screen.getByTestId("context-saver-card")).toHaveTextContent(
      "Non-Databricks source sharing is allowed for this worker route",
    );
    expect(screen.getByTestId("context-saver-card")).not.toHaveTextContent("Source route");
    expect(screen.getByTestId("context-saver-card")).toHaveTextContent("openai");
  });

  it("keeps the full response behind a disclosure", () => {
    render(<ContextSaverCard output={JSON.stringify(RESULT)} state="output-available" />);

    expect(screen.queryByText(/The relevant code/)).toBeNull();
    fireEvent.click(screen.getByTestId("context-saver-raw-toggle"));
    expect(screen.getByText(/The relevant code/)).toBeInTheDocument();
  });

  it("shows an honest running state before routing metadata arrives", () => {
    render(<ContextSaverCard output={null} state="input-available" />);

    expect(screen.getByTestId("context-saver-card")).toHaveTextContent("Running Focused Read");
  });
});
