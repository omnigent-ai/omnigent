// Tests for TodoPanel — the small presentational panel that mirrors Claude
// Code's todo list from useChatStore.todos. We mock the store so the panel's
// own rendering (empty state, per-status icon/strikethrough, activeForm
// subtitle) is exercised in isolation.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

type TodoItem = {
  content: string;
  status: "pending" | "in_progress" | "completed";
  activeForm: string;
};

// The store is a selector hook: useChatStore((s) => s.todos). Mock it to feed a
// controllable todos array per test.
const h = vi.hoisted(() => ({ todos: [] as TodoItem[] }));
vi.mock("@/store/chatStore", () => ({
  useChatStore: (selector: (s: { todos: TodoItem[] }) => unknown) => selector({ todos: h.todos }),
}));

import { TodoPanel } from "./TodoPanel";

afterEach(() => {
  cleanup();
  h.todos = [];
});

describe("TodoPanel", () => {
  it("renders nothing when the todo list is empty", () => {
    // WHY: the panel must occupy no space for sessions with no todos — it
    // returns null, so the container has no DOM children.
    h.todos = [];
    const { container } = render(<TodoPanel />);
    expect(container.firstChild).toBeNull();
  });

  it("renders one list item per todo with its content", () => {
    // WHY: confirms the map over todos renders every item's content text.
    h.todos = [
      { content: "Write tests", status: "pending", activeForm: "Writing tests" },
      { content: "Ship it", status: "completed", activeForm: "Shipping it" },
    ];
    render(<TodoPanel />);
    expect(screen.getByText("Write tests")).toBeInTheDocument();
    expect(screen.getByText("Ship it")).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
  });

  it("strikes through and dims a completed todo", () => {
    // WHY: completed todos get line-through + opacity-50; a regression in the
    // status-conditional classes would leave them looking active.
    h.todos = [{ content: "Done thing", status: "completed", activeForm: "Doing thing" }];
    render(<TodoPanel />);
    const span = screen.getByText("Done thing");
    expect(span.className).toContain("line-through");
    expect(span.closest("li")?.className).toContain("opacity-50");
  });

  it("shows the activeForm subtitle for an in_progress todo when it differs", () => {
    // WHY: an in-progress item surfaces its activeForm ("Doing X") under the
    // content — this is the live-status affordance.
    h.todos = [{ content: "Build feature", status: "in_progress", activeForm: "Building feature" }];
    render(<TodoPanel />);
    expect(screen.getByText("Build feature")).toBeInTheDocument();
    expect(screen.getByText("Building feature")).toBeInTheDocument();
  });

  it("omits the activeForm subtitle when it equals the content", () => {
    // WHY: the guard `activeForm !== content` prevents a redundant duplicate
    // line; identical text must appear exactly once.
    h.todos = [{ content: "Same text", status: "in_progress", activeForm: "Same text" }];
    render(<TodoPanel />);
    expect(screen.getAllByText("Same text")).toHaveLength(1);
  });

  it("does not show the activeForm subtitle for a non-in_progress todo", () => {
    // WHY: the subtitle is gated on in_progress; a pending todo with a distinct
    // activeForm must not render it.
    h.todos = [{ content: "Pending thing", status: "pending", activeForm: "Pending action" }];
    render(<TodoPanel />);
    expect(screen.queryByText("Pending action")).toBeNull();
  });
});
