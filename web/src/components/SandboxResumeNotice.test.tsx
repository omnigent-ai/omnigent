import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SandboxResumeView } from "@/hooks/useSandboxResume";

import { SandboxResumeNotice } from "./SandboxResumeNotice";

const view = vi.hoisted(() => ({ current: null as unknown as SandboxResumeView }));
vi.mock("@/hooks/useSandboxResume", () => ({ useSandboxResume: () => view.current }));

function sandbox(overrides: Partial<SandboxResumeView>): SandboxResumeView {
  return {
    status: "asleep",
    resumeStatus: "asleep",
    failure: null,
    availability: { available: true },
    resume: vi.fn(),
    ...overrides,
  };
}

beforeEach(() => {
  view.current = sandbox({});
});
afterEach(cleanup);

describe("SandboxResumeNotice", () => {
  it("renders nothing while the sandbox is up", () => {
    view.current = sandbox({ status: "none" });
    const { container } = render(<SandboxResumeNotice sessionId="conv_1" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("offers Resume sandbox for what the surface needs", () => {
    render(<SandboxResumeNotice sessionId="conv_1" purpose="browse files" />);
    expect(screen.getByRole("status")).toHaveTextContent("Sandbox is asleep");
    expect(screen.getByText("Resume it to browse files.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Resume sandbox" }));
    expect(view.current.resume).toHaveBeenCalledOnce();
  });

  it("pulses while resuming, with no action, and holds still when motion is reduced", () => {
    view.current = sandbox({ status: "resuming" });
    render(<SandboxResumeNotice sessionId="conv_1" />);
    expect(screen.getByRole("status")).toHaveTextContent("Resuming sandbox…");
    expect(screen.getByText(/Everything reconnects on its own/)).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    const halo = screen.getByTestId("sandbox-resume-pulse").firstElementChild!;
    expect(halo.className).toContain("animate-[sandbox-resume-breathe");
    expect(halo.className).toContain("motion-reduce:animate-none");
    // Theme tokens only, so it reads in light and dark.
    expect(halo.className).toContain("bg-primary/40");
  });

  it("announces a failure with Retry", () => {
    view.current = sandbox({ status: "failed", failure: "The sandbox couldn't start." });
    render(<SandboxResumeNotice sessionId="conv_1" layout="banner" />);
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Sandbox is asleepThe sandbox couldn't start.",
    );
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(view.current.resume).toHaveBeenCalledOnce();
  });

  it("explains, without an action, when the viewer can't resume it", () => {
    view.current = sandbox({
      availability: {
        available: false,
        reason: "Only the session’s owner can resume its sandbox.",
      },
    });
    render(<SandboxResumeNotice sessionId="conv_1" layout="banner" />);
    expect(screen.getByRole("status")).toHaveTextContent(
      "Only the session’s owner can resume its sandbox.",
    );
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
