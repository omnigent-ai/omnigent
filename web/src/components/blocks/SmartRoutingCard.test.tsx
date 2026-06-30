import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { SmartRoutingCard, parsePlannedTasks, parseRecommendations } from "./SmartRoutingCard";

afterEach(cleanup);

/** Args for a two-task fan-out, as the brain passes to sys_advise_models. */
const TWO_TASK_ARGS = {
  tasks: [
    { title: "review-security", agents: [{ agent: "codex", models: null }], task: "scan the diff" },
    {
      title: "refactor-auth",
      agents: [{ agent: "claude_code", models: null }],
      task: "refactor the auth flow",
    },
  ],
};

/** Success response for TWO_TASK_ARGS, shaped like _execute_advise_models_tool. */
const TWO_TASK_OUTPUT = JSON.stringify({
  recommendations: [
    {
      title: "review-security",
      agent: "codex",
      model: "databricks-claude-haiku-4-5",
      rationale: "Mechanical diff scan — cheap suffices.",
    },
    {
      title: "refactor-auth",
      agent: "claude_code",
      model: "databricks-claude-opus-4-8",
      rationale: "Multi-file refactor needs deep reasoning.",
    },
  ],
  enforced: true,
  note: "Dispatch each task with the SAME title…",
});

const card = () => screen.getByTestId("smart-routing-card");

describe("parsePlannedTasks", () => {
  it("returns one row per task title (agent comes from response)", () => {
    const tasks = parsePlannedTasks({
      tasks: [
        { title: "refactor", agents: [{ agent: "claude_code" }, { agent: "pi" }], task: "x" },
        { title: "review", agents: [{ agent: "codex" }], task: "y" },
        { title: "", agents: [{ agent: "codex" }] }, // empty title → dropped
        "not-an-object",
      ],
    });
    expect(tasks).toEqual([
      { title: "refactor", agent: "claude_code, pi" }, // hint from args for display during judging
      { title: "review", agent: "codex" },
    ]);
  });

  it("returns [] when tasks is missing or not an array", () => {
    expect(parsePlannedTasks({})).toEqual([]);
    expect(parsePlannedTasks({ tasks: "nope" })).toEqual([]);
  });
});

describe("parseRecommendations", () => {
  it("maps titles to model/rationale/agent (no tier)", () => {
    const recs = parseRecommendations(TWO_TASK_OUTPUT);
    expect(recs).not.toBeNull();
    expect(recs!.get("refactor-auth")).toEqual({
      model: "databricks-claude-opus-4-8",
      title: "refactor-auth",
      rationale: "Multi-file refactor needs deep reasoning.",
      agent: "claude_code",
    });
    expect(recs!.size).toBe(2);
  });

  it("returns null for the dispatcher's Error: strings and non-JSON", () => {
    // Every failure mode of the tool returns a plain string, not JSON —
    // null is what flips the card into its failure rendering.
    expect(parseRecommendations("Error: the intelligent model router is OFF")).toBeNull();
    expect(parseRecommendations("{}")).toBeNull();
  });
});

describe("SmartRoutingCard — judging (in-flight)", () => {
  it("shows the sizing header and per-task placeholders, no pills yet", () => {
    render(<SmartRoutingCard arguments={TWO_TASK_ARGS} output={null} state="input-available" />);
    expect(card().getAttribute("data-state-kind")).toBe("judging");
    expect(card()).toHaveTextContent("Intelligent routing");
    expect(card()).toHaveTextContent("Weighing 2 tasks…");
    // Rows render immediately from the args so the plan shape is visible
    // while the judge runs.
    expect(card()).toHaveTextContent("review-security");
    expect(card()).toHaveTextContent("→ codex"); // agent hint from args
    // Per-row shimmer verbs cycle the pool by row index: row 0 → weighing, row 1 → matching.
    expect(card()).toHaveTextContent("weighing…");
    expect(card()).toHaveTextContent("matching…");
    // No model pill can exist before the response.
    expect(card().textContent).not.toContain("haiku");
    // Nothing to expand yet.
    expect(screen.queryByTestId("smart-routing-raw-toggle")).toBeNull();
  });
});

describe("SmartRoutingCard — sized (success)", () => {
  it("renders one row per task with the model pill and rationale", () => {
    render(
      <SmartRoutingCard
        arguments={TWO_TASK_ARGS}
        output={TWO_TASK_OUTPUT}
        state="output-available"
      />,
    );
    expect(card().getAttribute("data-state-kind")).toBe("sized");
    expect(card()).toHaveTextContent("sized 2 tasks");
    // Short model names from the shared pill — proves the recommendation
    // content (not just the row scaffolding) made it through the parse.
    expect(card()).toHaveTextContent("haiku");
    expect(card()).toHaveTextContent("opus");
    expect(card()).toHaveTextContent("Mechanical diff scan — cheap suffices.");
    expect(card()).toHaveTextContent("Multi-file refactor needs deep reasoning.");
  });

  it("derives rows from the response when the args are unusable", () => {
    // Defensive path: a malformed/empty args dict (e.g. truncated frame)
    // must not blank the card when the response itself names the tasks.
    render(<SmartRoutingCard arguments={{}} output={TWO_TASK_OUTPUT} state="output-available" />);
    expect(card()).toHaveTextContent("review-security");
    expect(card()).toHaveTextContent("→ claude_code");
    expect(card()).toHaveTextContent("opus");
  });

  it("reveals the raw response JSON behind the chevron", () => {
    render(
      <SmartRoutingCard
        arguments={TWO_TASK_ARGS}
        output={TWO_TASK_OUTPUT}
        state="output-available"
      />,
    );
    // Collapsed by default: the structured rows ARE the surface.
    expect(screen.queryByText(/"enforced"/)).toBeNull();
    fireEvent.click(screen.getByTestId("smart-routing-raw-toggle"));
    // The pretty-printed response (a field the rows never show) is visible.
    expect(screen.getByText(/"enforced"/)).toBeInTheDocument();
  });
});

describe("SmartRoutingCard — failure", () => {
  it("shows the dispatcher's error string verbatim instead of fake rows", () => {
    const error =
      "Error: the routing advisor is unavailable right now (judge call failed); " +
      "dispatch with your own model choices.";
    render(<SmartRoutingCard arguments={TWO_TASK_ARGS} output={error} state="output-available" />);
    expect(card().getAttribute("data-state-kind")).toBe("failed");
    // The error text is the honest content — rendering rows would imply an
    // enforced plan exists when none was installed.
    expect(screen.getByTestId("smart-routing-error")).toHaveTextContent("judge call failed");
    expect(card().textContent).not.toContain("→ codex");
  });

  it("falls back to a quiet no-decision line when the turn died before output", () => {
    render(<SmartRoutingCard arguments={TWO_TASK_ARGS} output={null} state="cancelled" />);
    expect(card().getAttribute("data-state-kind")).toBe("failed");
    expect(screen.getByTestId("smart-routing-error")).toHaveTextContent(
      "No routing decision was recorded",
    );
  });
});
