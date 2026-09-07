// Tests for ChatPlanAccordion — the pinned "Plan (N/M)" tracker above the
// chat thread. We mock the store so the summary count logic and self-hide are
// exercised in isolation; TodoPanel (the expanded list) is covered separately.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

interface TodoItem {
  content: string;
  status: "pending" | "in_progress" | "completed";
  activeForm: string;
}

const h = vi.hoisted(() => ({ todos: [] as TodoItem[] }));
vi.mock("@/store/chatStore", () => ({
  useChatStore: (selector: (s: { todos: TodoItem[] }) => unknown) => selector({ todos: h.todos }),
}));

import { ChatPlanAccordion } from "./ChatPlanAccordion";

afterEach(() => {
  cleanup();
  h.todos = [];
});

describe("ChatPlanAccordion", () => {
  it("renders nothing when there are no tasks", () => {
    // WHY: the accordion must occupy no space for sessions with no plan — it
    // returns null so it never shrinks the chat scroll area needlessly.
    h.todos = [];
    const { container } = render(<ChatPlanAccordion />);
    expect(container.firstChild).toBeNull();
  });

  it("shows Plan with completed/total counts", () => {
    // WHY: the always-visible summary is the label the issue specifies —
    // "Plan (N/M)" where N is completed and M is the total task count.
    h.todos = [
      { content: "a", status: "completed", activeForm: "a" },
      { content: "b", status: "in_progress", activeForm: "b" },
      { content: "c", status: "pending", activeForm: "c" },
    ];
    render(<ChatPlanAccordion />);
    expect(screen.getByText("Plan")).toBeInTheDocument();
    expect(screen.getByText("(1/3)")).toBeInTheDocument();
  });

  it("keeps the native-shell stylesheet hook on the layout row", () => {
    // WHY: index.css offsets `.chat-plan-accordion` under [data-ios-native] /
    // [data-android-native] so the tracker clears the safe-area-shifted
    // header. Dropping the hook silently re-overlaps the floating controls
    // onto the Plan bar on notched phones.
    h.todos = [{ content: "a", status: "pending", activeForm: "a" }];
    const { container } = render(<ChatPlanAccordion />);
    expect(container.firstElementChild).toHaveClass("chat-plan-accordion");
  });

  it("lists the tasks in the expandable body", () => {
    // WHY: the expanded section reuses TodoPanel, so each task's content shows.
    h.todos = [
      { content: "Write tests", status: "pending", activeForm: "Writing tests" },
      { content: "Ship it", status: "completed", activeForm: "Shipping it" },
    ];
    render(<ChatPlanAccordion />);
    expect(screen.getByText("Write tests")).toBeInTheDocument();
    expect(screen.getByText("Ship it")).toBeInTheDocument();
  });
});
