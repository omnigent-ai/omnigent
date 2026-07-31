import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
  SUBAGENT_ROUTING_UNENFORCED,
  SessionWarningBanner,
  renderableWarnings,
} from "./SessionWarningBanner";

afterEach(cleanup);

describe("SessionWarningBanner", () => {
  it("renders nothing when the session has no warnings", () => {
    render(<SessionWarningBanner warnings={[]} />);
    expect(screen.queryByTestId("session-warning-banner")).toBeNull();
  });

  it("renders nothing when warnings are absent entirely (older server)", () => {
    render(<SessionWarningBanner />);
    expect(screen.queryByTestId("session-warning-banner")).toBeNull();
  });

  it("renders the unenforced-routing warning with harness and reason", () => {
    render(
      <SessionWarningBanner
        warnings={[
          {
            code: SUBAGENT_ROUTING_UNENFORCED,
            harness: "codex-native",
            reason: "hook canary never fired",
          },
        ]}
      />,
    );
    const banner = screen.getByTestId(`session-warning-${SUBAGENT_ROUTING_UNENFORCED}`);
    // Which harness lost enforcement is the actionable part — a generic
    // "routing degraded" line wouldn't tell the user where to look.
    expect(banner).toHaveTextContent("codex-native");
    expect(banner).toHaveTextContent("hook canary never fired");
  });

  it("renders without a reason line when the payload omits it", () => {
    render(<SessionWarningBanner warnings={[{ code: SUBAGENT_ROUTING_UNENFORCED }]} />);
    const banner = screen.getByTestId(`session-warning-${SUBAGENT_ROUTING_UNENFORCED}`);
    expect(banner).toHaveTextContent("Sub-agent routing isn't enforced");
  });

  // The banner floats over the chat instead of sitting in the flow: an
  // in-flow strip pushed the whole conversation down when a warning arrived
  // mid-session. It must also stay inside the chat column (no sidebar/panel
  // coverage) and let clicks and scrolling pass through outside its rows.
  it("floats over the chat column without reflowing it", () => {
    render(<SessionWarningBanner warnings={[{ code: SUBAGENT_ROUTING_UNENFORCED }]} />);
    const strip = screen.getByTestId("session-warning-banner");
    expect(strip).toHaveClass(
      "absolute",
      "top-14",
      "z-20",
      "pointer-events-none",
      "md:right-[var(--workspace-panel-offset,0px)]",
    );
    const row = screen.getByTestId(`session-warning-${SUBAGENT_ROUTING_UNENFORCED}`);
    // The row itself stays interactive and bounded, so it can't span into the
    // neighbouring panes.
    expect(row).toHaveClass("pointer-events-auto", "max-w-2xl");
  });

  it("stacks multiple warnings downward inside the overlay", () => {
    render(
      <SessionWarningBanner
        warnings={[
          { code: SUBAGENT_ROUTING_UNENFORCED, harness: "codex-native" },
          { code: SUBAGENT_ROUTING_UNENFORCED, harness: "claude-native" },
        ]}
      />,
    );
    const strip = screen.getByTestId("session-warning-banner");
    expect(strip).toHaveClass("flex", "flex-col");
    expect(strip.children).toHaveLength(2);
  });

  it("ignores unknown warning codes rather than leaking the raw code", () => {
    render(<SessionWarningBanner warnings={[{ code: "some_future_warning", reason: "x" }]} />);
    // Hidden, not rendered raw: the UI has no copy for it yet.
    expect(screen.queryByTestId("session-warning-banner")).toBeNull();
  });

  // Codes come off the wire, so a code that names an Object.prototype member
  // must be as inert as any other unknown code — never a render-time throw.
  it.each(["__proto__", "toString", "constructor", "hasOwnProperty"])(
    "renders nothing for the prototype-member code %s",
    (code) => {
      expect(() =>
        render(<SessionWarningBanner warnings={[{ code, reason: "x" }]} />),
      ).not.toThrow();
      expect(screen.queryByTestId("session-warning-banner")).toBeNull();
      expect(screen.queryByTestId(`session-warning-${code}`)).toBeNull();
    },
  );
});

describe("renderableWarnings", () => {
  it("keeps only the codes the banner has copy for", () => {
    const kept = renderableWarnings([
      { code: "some_future_warning" },
      { code: SUBAGENT_ROUTING_UNENFORCED, harness: "claude-native" },
    ]);
    expect(kept.map((w) => w.code)).toEqual([SUBAGENT_ROUTING_UNENFORCED]);
  });

  it("drops codes that only exist on Object.prototype", () => {
    const kept = renderableWarnings([
      { code: "__proto__" },
      { code: "toString" },
      { code: SUBAGENT_ROUTING_UNENFORCED },
    ]);
    expect(kept.map((w) => w.code)).toEqual([SUBAGENT_ROUTING_UNENFORCED]);
  });

  it("tolerates null/undefined", () => {
    expect(renderableWarnings(null)).toEqual([]);
    expect(renderableWarnings(undefined)).toEqual([]);
  });
});
