import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ProactiveAgentPrompt, ProactiveAgentRuns } from "@/hooks/useProactiveAgentSettings";
import { ProactiveAgentAdminControls, parseRunUsers } from "./ProactiveAgentAdminControls";

const mocks = vi.hoisted(() => ({
  prompt: undefined as ProactiveAgentPrompt | undefined,
  save: vi.fn(),
  run: vi.fn(),
  runResult: undefined as ProactiveAgentRuns | undefined,
}));
vi.mock("@/hooks/useProactiveAgentSettings", () => ({
  useProactiveAgentPrompt: () => ({
    query: { data: mocks.prompt, isError: false },
    save: { mutate: mocks.save, isPending: false, isError: false },
  }),
  useRunProactiveAgent: () => ({
    mutate: mocks.run,
    isPending: false,
    isError: false,
    data: mocks.runResult,
  }),
}));

beforeEach(() => {
  mocks.prompt = { prompt: "Built in.", default_prompt: "Built in.", overridden: false };
  mocks.runResult = undefined;
  mocks.save.mockReset();
  mocks.run.mockReset();
});
afterEach(cleanup);

describe("ProactiveAgentAdminControls", () => {
  it("saves an edited prompt and restores the default only when overridden", () => {
    render(<ProactiveAgentAdminControls />);
    const restore = screen.getByRole("button", { name: "Restore default" });
    expect(restore).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save prompt" })).toBeDisabled();

    fireEvent.change(screen.getByRole("textbox", { name: "Prompt" }), {
      target: { value: "Post one item." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save prompt" }));

    expect(mocks.save).toHaveBeenCalledWith("Post one item.", expect.anything());
  });

  it("restores the built-in prompt", () => {
    mocks.prompt = { prompt: "Custom.", default_prompt: "Built in.", overridden: true };
    render(<ProactiveAgentAdminControls />);

    fireEvent.click(screen.getByRole("button", { name: "Restore default" }));

    expect(mocks.save).toHaveBeenCalledWith(null, expect.anything());
    expect(screen.getByText("Using a custom prompt")).toBeInTheDocument();
  });

  it("runs now for the listed users and summarizes the result", () => {
    mocks.runResult = {
      started: [{ user_id: "alice@example.com", task_id: "t", run_id: null }],
      skipped: [{ user_id: "bob@example.com", reason: "no_task" }],
      truncated: false,
    };
    render(<ProactiveAgentAdminControls />);

    fireEvent.change(screen.getByRole("textbox", { name: "Run now" }), {
      target: { value: "alice@example.com, bob@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Run now" }));

    expect(mocks.run).toHaveBeenCalledWith(["alice@example.com", "bob@example.com"]);
    expect(
      screen.getByText("Started 1 run. Skipped: bob@example.com (no task yet)."),
    ).toBeInTheDocument();
  });
});

describe("parseRunUsers", () => {
  it("splits on commas and spaces and drops repeats", () => {
    expect(parseRunUsers(" a, b  a\nc,, ")).toEqual(["a", "b", "c"]);
    expect(parseRunUsers("*")).toEqual(["*"]);
  });
});
