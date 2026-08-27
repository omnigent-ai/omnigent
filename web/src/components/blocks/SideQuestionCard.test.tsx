// DOM smoke for the `/btw` side-question aside. Pure jsdom — no
// canvas, no clipboard, no animation timing.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { SideQuestionCard } from "./SideQuestionCard";

afterEach(cleanup);

describe("SideQuestionCard", () => {
  it("shows the question in the trigger row under a 'btw' label", () => {
    render(<SideQuestionCard question="which harness is this?" answer="claude-native" />);
    expect(screen.getByText("btw")).toBeDefined();
    expect(screen.getByText("which harness is this?")).toBeDefined();
  });

  it("keeps the answer collapsed until clicked", () => {
    // An aside was never in the model's context, so it must not
    // occupy the transcript like a turn the agent acted on.
    render(<SideQuestionCard question="which harness?" answer="It runs on claude-native." />);
    expect(screen.queryByText("It runs on claude-native.")).toBeNull();

    fireEvent.click(screen.getByTestId("side-question-card"));
    expect(screen.getByText("It runs on claude-native.")).toBeDefined();
  });

  it("says the exchange is outside the conversation's context", () => {
    // Without this the aside reads like a normal turn, and the user
    // has no way to tell the agent never saw it.
    render(<SideQuestionCard question="q" answer="a" />);
    fireEvent.click(screen.getByTestId("side-question-card"));
    expect(screen.getByText(/not part of the conversation's context/)).toBeDefined();
  });
});
