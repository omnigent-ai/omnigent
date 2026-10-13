import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { StaticSandboxWakeBanner, type PausedSandbox } from "./StaticSandboxWakeBanner";

function sandbox(overrides: Partial<PausedSandbox> = {}): PausedSandbox {
  return {
    name: "foo",
    state: "paused",
    waking: false,
    onResume: vi.fn(),
    onRemove: vi.fn(),
    ...overrides,
  };
}

describe("StaticSandboxWakeBanner", () => {
  it("offers Resume for a paused VM", () => {
    const s = sandbox();
    render(<StaticSandboxWakeBanner sandbox={s} />);
    expect(screen.getByTestId("paused-sandbox-chip").textContent).toContain("foo is paused");
    fireEvent.click(screen.getByTestId("paused-sandbox-resume"));
    expect(s.onResume).toHaveBeenCalled();
  });

  it("shows Waking while a wake runs", () => {
    render(<StaticSandboxWakeBanner sandbox={sandbox({ waking: true })} />);
    expect(screen.getByTestId("paused-sandbox-chip").textContent).toContain("Waking foo");
    expect(screen.queryByTestId("paused-sandbox-resume")).toBeNull();
  });

  it("offers Remove entry, never Resume, for a gone VM", () => {
    const s = sandbox({ state: "gone" });
    render(<StaticSandboxWakeBanner sandbox={s} />);
    expect(screen.getByTestId("paused-sandbox-chip").textContent).toContain(
      "VM foo no longer exists",
    );
    expect(screen.queryByTestId("paused-sandbox-resume")).toBeNull();
    fireEvent.click(screen.getByTestId("paused-sandbox-remove"));
    expect(s.onRemove).toHaveBeenCalled();
  });
});
