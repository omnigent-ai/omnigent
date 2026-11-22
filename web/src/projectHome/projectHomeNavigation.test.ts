import { describe, expect, it } from "vitest";
import {
  PROJECT_PANEL_SESSION_PARAM,
  projectHomePath,
  projectNameFromHomePath,
  projectSessionPanelSearch,
  ticketSessionSearch,
  ticketPanelSearch,
  projectView,
  projectViewSearch,
  ticketBatchSearch,
  ticketSelectionIds,
} from "./projectHomeNavigation";

describe("project home navigation", () => {
  it.each(["", "/ml/omnigent-embed"])("round-trips a project name under mount %j", (mount) => {
    expect(projectHomePath("My Project")).toBe("/projects/My%20Project");
    expect(projectNameFromHomePath(`${mount}${projectHomePath("My Project")}`)).toBe("My Project");
    expect(projectNameFromHomePath(`${mount}${projectHomePath("a/b")}/`)).toBe("a/b");
    expect(projectNameFromHomePath(`${mount}/projects/%E0`)).toBeNull();
    expect(projectNameFromHomePath(`${mount}/projects`)).toBeNull();
    expect(projectNameFromHomePath(`${mount}/projects/a/extra`)).toBeNull();
    expect(projectNameFromHomePath(`${mount}/c/session`)).toBeNull();
  });

  it("links the side panel to a session by an encoded query parameter", () => {
    const search = projectSessionPanelSearch("a b/c&d");
    expect(search).toBe("?session=a+b%2Fc%26d");
    expect(new URLSearchParams(search).get(PROJECT_PANEL_SESSION_PARAM)).toBe("a b/c&d");
  });

  it("retains ticket context in worker and main chats", () => {
    const ticket = "a b/c&d";
    const search = new URLSearchParams(ticketSessionSearch(ticket, "worker/one"));
    expect(search.get("ticket")).toBe(ticket);
    expect(search.get("session")).toBe("worker/one");
    const lead = new URLSearchParams(ticketSessionSearch(ticket));
    expect(lead.get("view")).toBe("chat");
    expect(lead.has("agent")).toBe(false);
    expect(lead.has("orchestrator")).toBe(false);
    expect(new URLSearchParams(ticketPanelSearch(ticket)).get("ticket")).toBe(ticket);
    expect(new URLSearchParams(ticketPanelSearch(ticket)).has("session")).toBe(false);
  });

  it.each([
    ["", "chat"],
    ["orchestrator=1", "chat"],
    ["ticket=one", "board"],
    ["session=worker", "board"],
    ["ticket=one&orchestrator=1", "board"],
    ["view=chat&ticket=one", "chat"],
    ["view=board", "board"],
  ])("resolves the project view for %s", (search, view) => {
    expect(projectView(new URLSearchParams(search))).toBe(view);
  });

  it("retains a batch through ticket/worker navigation and clears it when returning to Chat", () => {
    const board = new URLSearchParams(ticketBatchSearch(["one", "two", "one"]));
    expect(ticketSelectionIds(board)).toEqual(["one", "two"]);
    const ticket = new URLSearchParams(ticketPanelSearch("two", board));
    const worker = new URLSearchParams(ticketSessionSearch("two", "worker", ticket));
    expect(ticketSelectionIds(worker)).toEqual(["one", "two"]);
    const back = new URLSearchParams(ticketPanelSearch("two", worker));
    expect(back.get("ticket")).toBe("two");
    expect(back.has("session")).toBe(false);
    expect(projectViewSearch("chat", back)).toBe("?view=chat");
  });
});
