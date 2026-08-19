// End-to-end style test for the in-chat Plan tracker.
//
// Unlike ChatPlanAccordion.test.tsx (which mocks the store module in
// isolation), this drives the REAL chat store — the same `todos` slice the
// live app writes from the session snapshot on bind and from `session.todos`
// SSE updates — and renders the real ChatPlanAccordion. So mocked data flows
// store → selector → UI exactly as it does in production, and a live task
// update re-renders the tracker the same way an agent's progress would.

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { useChatStore } from "@/store/chatStore";
import { ChatPlanAccordion } from "./ChatPlanAccordion";

interface Todo {
  content: string;
  status: "pending" | "in_progress" | "completed";
  activeForm: string;
}

// Mirrors how the live app populates the slice: both the snapshot-bind and the
// `session.todos` SSE handler write `todos` onto the active conversation
// projection (chatStore handleSessionEvent → applyToConversation).
function seedTodos(todos: Todo[]) {
  useChatStore.setState({ conversationId: "conv-e2e", todos });
}

afterEach(() => {
  cleanup();
  useChatStore.setState({ todos: [], conversationId: null });
});

describe("ChatPlanAccordion (e2e with mocked store data)", () => {
  it("surfaces the plan collapsed, expands to the task list, tracks live updates, and hides when cleared", () => {
    seedTodos([
      { content: "Read the code", status: "completed", activeForm: "Reading the code" },
      {
        content: "Write the accordion",
        status: "in_progress",
        activeForm: "Writing the accordion",
      },
      { content: "Ship it", status: "pending", activeForm: "Shipping it" },
    ]);

    const { container } = render(<ChatPlanAccordion />);

    // Summary reflects completed/total from the seeded data.
    expect(screen.getByText("Plan")).toBeInTheDocument();
    expect(screen.getByText("(1/3)")).toBeInTheDocument();

    // Collapsed by default: the <details> starts closed.
    const details = container.querySelector("details")!;
    expect(details.open).toBe(false);

    // The task list is wired through TodoPanel and rendered in the body.
    expect(screen.getByText("Read the code")).toBeInTheDocument();
    expect(screen.getByText("Write the accordion")).toBeInTheDocument();

    // Expanding via the summary opens the disclosure.
    fireEvent.click(container.querySelector("summary")!);
    expect(details.open).toBe(true);

    // A live update (the agent completes a task) flows through the store and
    // the tracker re-renders its count without a remount.
    act(() => {
      seedTodos([
        { content: "Read the code", status: "completed", activeForm: "Reading the code" },
        {
          content: "Write the accordion",
          status: "completed",
          activeForm: "Writing the accordion",
        },
        { content: "Ship it", status: "pending", activeForm: "Shipping it" },
      ]);
    });
    expect(screen.getByText("(2/3)")).toBeInTheDocument();

    // Clearing the list (session with no tasks) hides the tracker entirely.
    act(() => {
      seedTodos([]);
    });
    expect(container.querySelector("details")).toBeNull();
    expect(screen.queryByText("Plan")).toBeNull();
  });
});
