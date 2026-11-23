import { describe, expect, it } from "vitest";
import {
  PROJECT_PANEL_SESSION_PARAM,
  projectHomePath,
  projectOrchestratorPath,
  projectNameFromHomePath,
  ticketPanelSearch,
  isLegacyOrchestratorLink,
  ticketBatchSearch,
  ticketSelectionIds,
} from "./projectHomeNavigation";

describe("project home navigation", () => {
  it.each(["", "/ml/omnigent-embed"])("round-trips a project name under mount %j", (mount) => {
    expect(projectHomePath("My Project")).toBe("/projects/My%20Project");
    expect(projectNameFromHomePath(`${mount}${projectHomePath("My Project")}`)).toBe("My Project");
    expect(projectNameFromHomePath(`${mount}${projectHomePath("a/b")}/`)).toBe("a/b");
    expect(projectNameFromHomePath(`${mount}${projectOrchestratorPath("My Project")}`)).toBe(
      "My Project",
    );
    expect(projectNameFromHomePath(`${mount}/projects/%E0`)).toBeNull();
    expect(projectNameFromHomePath(`${mount}/projects`)).toBeNull();
    expect(projectNameFromHomePath(`${mount}/projects/a/extra`)).toBeNull();
    expect(projectNameFromHomePath(`${mount}/c/session`)).toBeNull();
  });

  it("opens the orchestrator panel with board filters and clears other panel targets", () => {
    expect(projectOrchestratorPath("My Project/One")).toBe(
      "/projects/My%20Project%2FOne/orchestrator",
    );
    expect(
      projectOrchestratorPath(
        "My Project",
        new URLSearchParams(
          "tickets=one,two&ticket=two&session=worker&view=chat&orchestrator=1&agent=primary",
        ),
      ),
    ).toBe("/projects/My%20Project/orchestrator?tickets=one%2Ctwo");
    const ticket = "a b/c&d";
    expect(new URLSearchParams(ticketPanelSearch(ticket)).get("ticket")).toBe(ticket);
    expect(new URLSearchParams(ticketPanelSearch(ticket)).has("session")).toBe(false);
  });

  it.each([
    ["", false],
    ["orchestrator=1", true],
    ["ticket=one", false],
    ["session=worker", false],
    ["ticket=one&orchestrator=1", true],
    ["view=chat&ticket=one", true],
    ["agent=primary", true],
    ["view=board", false],
  ])("recognizes legacy orchestrator navigation for %s", (search, legacy) => {
    expect(isLegacyOrchestratorLink(new URLSearchParams(search))).toBe(legacy);
  });

  it("retains a batch through ticket navigation while clearing legacy session targets", () => {
    const board = new URLSearchParams(ticketBatchSearch(["one", "two", "one"]));
    expect(ticketSelectionIds(board)).toEqual(["one", "two"]);
    const ticket = new URLSearchParams(ticketPanelSearch("two", board));
    const worker = new URLSearchParams(ticket);
    worker.set(PROJECT_PANEL_SESSION_PARAM, "worker");
    expect(ticketSelectionIds(worker)).toEqual(["one", "two"]);
    const back = new URLSearchParams(ticketPanelSearch("two", worker));
    expect(back.get("ticket")).toBe("two");
    expect(back.has("session")).toBe(false);
    expect(ticketSelectionIds(back)).toEqual(["one", "two"]);
  });
});
