import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ComputerUseViewModel } from "@/lib/computerUse";
import { useChatStore } from "@/store/chatStore";
import { ComputerUsePanel } from "./ComputerUsePanel";

vi.mock("@/components/SessionImage", () => ({
  SessionImage: ({ path, alt, eager }: { path?: string; alt: string; eager?: boolean }) => (
    <img src={path} alt={alt} data-testid="session-image" loading={eager ? "eager" : "lazy"} />
  ),
}));

const base: ComputerUseViewModel = {
  callId: "call_1",
  presentation: {
    kind: "computer_use",
    provider: "codex",
    appName: "TextEdit",
    appId: "com.apple.TextEdit",
    actionLabel: "Inspect document",
    actionKinds: ["inspect"],
  },
  status: "completed",
  frame: {
    kind: "computer_frame",
    fileId: "file/frame 1",
    contentType: "image/png",
    width: 1280,
    height: 800,
  },
  error: null,
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("ComputerUsePanel", () => {
  it("renders provider-neutral app, action, lifecycle, and authorized frame path", () => {
    render(<ComputerUsePanel conversationId="conv/1" viewModel={base} />);

    expect(screen.getByText("Codex")).toBeInTheDocument();
    expect(screen.getByText("TextEdit")).toBeInTheDocument();
    expect(screen.getByText("Inspect document")).toBeInTheDocument();
    expect(screen.getByRole("status", { name: /codex computer use completed/i })).toBeVisible();
    expect(screen.getByRole("list", { name: "Computer actions" })).toHaveTextContent("Inspecting");
    expect(screen.getByTestId("session-image")).toHaveAttribute(
      "src",
      "/v1/sessions/conv%2F1/resources/files/file%2Fframe%201/content",
    );
    // The frame is the reason the panel is open, so it must not wait for a
    // lazy-load trigger this layout may never produce.
    expect(screen.getByTestId("session-image")).toHaveAttribute("loading", "eager");
    expect(screen.queryByText("Latest frame")).toBeNull();
    expect(screen.queryByText("1280 × 800")).toBeNull();
  });

  it("renders normalized interaction icons and a generic fallback", () => {
    const { rerender } = render(
      <ComputerUsePanel
        conversationId="conv_1"
        viewModel={{
          ...base,
          presentation: {
            ...base.presentation,
            actionKinds: ["click", "scroll", "type", "select", "drag", "key", "interact"],
          },
        }}
      />,
    );

    const actions = screen.getByRole("list", { name: "Computer actions" });
    for (const label of [
      "Clicking",
      "Scrolling",
      "Typing",
      "Selecting",
      "Dragging",
      "Pressing keys",
      "Interacting",
    ]) {
      expect(actions).toHaveTextContent(label);
    }

    rerender(
      <ComputerUsePanel
        conversationId="conv_1"
        viewModel={{ ...base, presentation: { ...base.presentation, actionKinds: undefined } }}
      />,
    );
    expect(screen.getByRole("list", { name: "Computer actions" })).toHaveTextContent(
      "Using computer",
    );
  });

  it("shows a loader while the first frame is pending", () => {
    render(
      <ComputerUsePanel
        conversationId="conv_1"
        viewModel={{ ...base, status: "running", frame: null }}
      />,
    );

    expect(screen.getByRole("status", { name: "Loading computer preview" })).toHaveClass("h-64");
    expect(screen.queryByRole("img", { name: "No computer preview available" })).toBeNull();
  });

  it("shows a bounded no-frame fallback after an action finishes without an image", () => {
    render(<ComputerUsePanel conversationId="conv_1" viewModel={{ ...base, frame: null }} />);

    expect(screen.getByRole("img", { name: "No computer preview available" })).toHaveClass("h-64");
    expect(screen.queryByTestId("session-image")).toBeNull();
  });

  it("exposes failed output as an alert and distinguishes interrupted state", () => {
    const { rerender } = render(
      <ComputerUsePanel
        conversationId="conv_1"
        viewModel={{ ...base, status: "failed", error: "Screen Recording is unavailable" }}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Screen Recording is unavailable");
    expect(screen.getByRole("status", { name: /failed/i })).toBeVisible();

    rerender(
      <ComputerUsePanel
        conversationId="conv_1"
        viewModel={{ ...base, status: "interrupted", error: null }}
      />,
    );
    expect(screen.getByRole("status", { name: /interrupted/i })).toBeVisible();
  });

  it("uses the existing session interrupt path for Stop", () => {
    const stop = vi.fn();
    useChatStore.setState({ stop });
    render(
      <ComputerUsePanel
        conversationId="conv_1"
        viewModel={{ ...base, status: "running", frame: null }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(stop).toHaveBeenCalledOnce();
  });

  it("renders an explicit unavailable state", () => {
    render(<ComputerUsePanel conversationId="conv_1" viewModel={null} />);
    expect(screen.getByRole("status")).toHaveTextContent("Computer Use unavailable");
  });
});
