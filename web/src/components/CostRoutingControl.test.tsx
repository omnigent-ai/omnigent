import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import {
  COST_CONTROL_PLAN_LABEL,
  IntelligentModelControl,
  type CostRoutingVerdict,
  isCostRoutingSession,
  parseCostRoutingVerdict,
  shortModelName,
} from "./CostRoutingControl";

afterEach(cleanup);

/** A fully-populated valid v3 verdict for component-state cases. */
const APPLIED_VERDICT: CostRoutingVerdict = {
  tier: "cheap",
  model: "databricks-claude-haiku-4-5",
  applied: true,
  rationale: "Routine lookup; a cheap model suffices.",
  turnAnchor: "2026-06-10T12:00:00+00:00",
};

/** Serialize a v3 plan payload into the labels dict shape the server returns. */
function planLabels(payload: Record<string, unknown>): Record<string, string> {
  return { [COST_CONTROL_PLAN_LABEL]: JSON.stringify(payload) };
}

const VALID_V3_PLAN = {
  version: 3,
  tier: "cheap",
  model: "databricks-claude-haiku-4-5",
  applied: true,
  rationale: "Routine lookup; a cheap model suffices.",
  turn_anchor: "2026-06-10T12:00:00+00:00",
};

describe("parseCostRoutingVerdict", () => {
  it("parses a valid v3 plan into a verdict", () => {
    expect(parseCostRoutingVerdict(planLabels(VALID_V3_PLAN))).toEqual({
      tier: "cheap",
      model: "databricks-claude-haiku-4-5",
      applied: true,
      rationale: "Routine lookup; a cheap model suffices.",
      turnAnchor: "2026-06-10T12:00:00+00:00",
    });
  });

  it("returns null for undefined labels (snapshot not loaded)", () => {
    expect(parseCostRoutingVerdict(undefined)).toBeNull();
  });

  it("returns null when the plan label is absent (no advisor on this server)", () => {
    expect(parseCostRoutingVerdict({ "omnigent.wrapper": "claude-code-native-ui" })).toBeNull();
  });

  it("returns null for unparseable JSON", () => {
    expect(parseCostRoutingVerdict({ [COST_CONTROL_PLAN_LABEL]: "{not json" })).toBeNull();
  });

  it("returns null for a non-object JSON payload", () => {
    expect(parseCostRoutingVerdict({ [COST_CONTROL_PLAN_LABEL]: "42" })).toBeNull();
  });

  it("silently ignores a legacy v2 plan (entries array)", () => {
    // v2 plans carry `entries` instead of flat fields; must read as "no verdict", not crash.
    const v2 = planLabels({
      version: 2,
      entries: [{ task: "review PR 42", tier: "cheap" }],
      rationale: "split",
      turn_anchor: "2026-06-10T12:00:00+00:00",
    });
    expect(parseCostRoutingVerdict(v2)).toBeNull();
  });

  it("returns null for an unknown future version", () => {
    expect(parseCostRoutingVerdict(planLabels({ ...VALID_V3_PLAN, version: 4 }))).toBeNull();
  });

  it("returns null for a tier outside the enum", () => {
    expect(parseCostRoutingVerdict(planLabels({ ...VALID_V3_PLAN, tier: "free" }))).toBeNull();
  });

  it("returns null for a missing or empty model", () => {
    expect(parseCostRoutingVerdict(planLabels({ ...VALID_V3_PLAN, model: "" }))).toBeNull();
    const { model: _dropped, ...withoutModel } = VALID_V3_PLAN;
    expect(parseCostRoutingVerdict(planLabels(withoutModel))).toBeNull();
  });

  it("returns null for a non-boolean applied flag", () => {
    expect(parseCostRoutingVerdict(planLabels({ ...VALID_V3_PLAN, applied: "yes" }))).toBeNull();
  });

  it("degrades missing rationale/turn_anchor to null fields, keeping the verdict", () => {
    // tier/model/applied are required for display; the prose fields are not.
    const { rationale: _r, turn_anchor: _t, ...minimal } = VALID_V3_PLAN;
    expect(parseCostRoutingVerdict(planLabels(minimal))).toEqual({
      tier: "cheap",
      model: "databricks-claude-haiku-4-5",
      applied: true,
      rationale: null,
      turnAnchor: null,
    });
  });
});

describe("isCostRoutingSession", () => {
  it("matches any top-level session with an agent name", () => {
    expect(isCostRoutingSession({ agentName: "polly", parentSessionId: null })).toBe(true);
    expect(isCostRoutingSession({ agentName: "debby", parentSessionId: null })).toBe(true);
  });

  it("rejects a child session", () => {
    expect(isCostRoutingSession({ agentName: "polly", parentSessionId: "conv_parent987" })).toBe(
      false,
    );
  });

  it("rejects a session with no agent name", () => {
    expect(isCostRoutingSession({ agentName: null, parentSessionId: null })).toBe(false);
  });

  it("rejects a missing session", () => {
    expect(isCostRoutingSession(null)).toBe(false);
    expect(isCostRoutingSession(undefined)).toBe(false);
  });
});

describe("shortModelName", () => {
  it("collapses Claude ids to their family token", () => {
    expect(shortModelName("databricks-claude-haiku-4-5")).toBe("haiku");
    expect(shortModelName("databricks-claude-sonnet-4-6")).toBe("sonnet");
    expect(shortModelName("claude-opus-4-7")).toBe("opus");
  });

  it("strips the databricks- prefix from non-Claude ids", () => {
    expect(shortModelName("databricks-gpt-5-4-mini")).toBe("gpt-5-4-mini");
  });

  it("passes unrecognized ids through unchanged (fallback to the id)", () => {
    expect(shortModelName("gpt-5.4")).toBe("gpt-5.4");
  });
});

type ControlProps = Partial<Parameters<typeof IntelligentModelControl>[0]>;

/** The control inside the TooltipProvider the app shell supplies globally. */
function controlTree(props: ControlProps = {}) {
  return (
    <TooltipProvider>
      <IntelligentModelControl value={null} onChange={() => {}} {...props} />
    </TooltipProvider>
  );
}

function renderControl(props: ControlProps = {}) {
  return render(controlTree(props));
}

const trigger = () => screen.getByTestId("cost-toggle-trigger");

/** Open the hover tooltip; Radix opens it on trigger focus in jsdom. */
function openTooltip() {
  fireEvent.focus(trigger());
}

describe("IntelligentModelControl — toggle semantics", () => {
  it("presents on as pressed with the lit data-mode", () => {
    renderControl({ value: "on" });
    expect(trigger()).toHaveAttribute("aria-pressed", "true");
    // data-mode drives the CSS cross-fade between the off/on glyph layers.
    expect(trigger()).toHaveAttribute("data-mode", "on");
  });

  it("renders the lit waypoints glyph monochrome — currentColor, no gradient defs", () => {
    // Owner-approved restyle: the lit glyph inherits the toggle's foreground
    // color; blue stays semantic elsewhere in the composer.
    const { container } = renderControl({ value: "on" });
    const litSvg = container.querySelector(".imc-layer-on svg");
    expect(litSvg).not.toBeNull();
    expect(litSvg!.querySelector("defs")).toBeNull();
    // Waypoints glyph: four filled nodes plus staged connector traces.
    expect(litSvg!.querySelectorAll("circle").length).toBe(4);
    for (const node of litSvg!.querySelectorAll("circle")) {
      expect(node.getAttribute("fill")).toBe("currentColor");
      expect(node.getAttribute("stroke")).toBe("currentColor");
    }
    const traces = litSvg!.querySelectorAll("path.imc-spark");
    expect(traces.length).toBeGreaterThan(0);
    for (const trace of traces) {
      expect(trace.getAttribute("stroke")).toBe("currentColor");
    }
  });

  it("presents off as unpressed and muted", () => {
    renderControl({ value: "off" });
    expect(trigger()).toHaveAttribute("aria-pressed", "false");
    expect(trigger()).toHaveAttribute("data-mode", "off");
  });

  it("presents unset (null) exactly like off", () => {
    // null = no per-session override recorded; the contract says it reads as off.
    renderControl({ value: null });
    expect(trigger()).toHaveAttribute("aria-pressed", "false");
    expect(trigger()).toHaveAttribute("data-mode", "off");
  });

  it("click on unset (null) flips to on — and never emits null", () => {
    const onChange = vi.fn();
    renderControl({ value: null, onChange });
    fireEvent.click(trigger());
    // Exactly [["on"]]: a "null" emission would PATCH a clear instead of
    // an explicit enable; an "off" emission would invert the flip.
    expect(onChange.mock.calls).toEqual([["on"]]);
  });

  it("click on off flips to on", () => {
    const onChange = vi.fn();
    renderControl({ value: "off", onChange });
    fireEvent.click(trigger());
    expect(onChange.mock.calls).toEqual([["on"]]);
  });

  it("click on on flips to off", () => {
    const onChange = vi.fn();
    renderControl({ value: "on", onChange });
    fireEvent.click(trigger());
    expect(onChange.mock.calls).toEqual([["off"]]);
  });

  it("disabled blocks interaction (read-only sessions)", () => {
    const onChange = vi.fn();
    renderControl({ value: "on", onChange, disabled: true });
    expect(trigger()).toBeDisabled();
    fireEvent.click(trigger());
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("IntelligentModelControl — tooltip", () => {
  it("primary line is exactly 'Intelligent model router'", () => {
    renderControl({ value: "off" });
    openTooltip();
    // getAllBy: radix can mount the open tooltip twice (portal content +
    // a visually-hidden a11y copy). Exact-match textContent guards
    // against suffixes creeping into the locked primary line.
    const titles = screen.getAllByTestId("imc-tooltip-title");
    expect(titles.length).toBeGreaterThan(0);
    for (const title of titles) {
      // Renamed from "Intelligent model control"; this also fails if any
      // user-visible "model control" string survives the rename sweep.
      expect(title.textContent).toBe("Intelligent model router");
    }
  });

  it("on + applied verdict: secondary line reads 'Picked haiku · cheap'", () => {
    renderControl({ value: "on", verdict: APPLIED_VERDICT });
    openTooltip();
    // Full-line equality: proves verb choice, model shortening, and the
    // "· tier" suffix all composed correctly — not just substring presence.
    expect(screen.getAllByTestId("imc-verdict-line")[0].textContent).toBe("Picked haiku · cheap");
  });

  it("on + shadow verdict (applied=false): reads 'Would pick haiku · cheap'", () => {
    renderControl({ value: "on", verdict: { ...APPLIED_VERDICT, applied: false } });
    openTooltip();
    expect(screen.getAllByTestId("imc-verdict-line")[0].textContent).toBe(
      "Would pick haiku · cheap",
    );
  });

  it("on without a verdict: primary line only", () => {
    renderControl({ value: "on" });
    openTooltip();
    expect(screen.getAllByTestId("imc-tooltip-title").length).toBeGreaterThan(0);
    expect(screen.queryAllByTestId("imc-verdict-line")).toEqual([]);
  });

  it("off suppresses the verdict line even when a verdict exists", () => {
    // A stale verdict must not advertise routing that is no longer active.
    renderControl({ value: "off", verdict: APPLIED_VERDICT });
    openTooltip();
    expect(screen.getAllByTestId("imc-tooltip-title").length).toBeGreaterThan(0);
    expect(screen.queryAllByTestId("imc-verdict-line")).toEqual([]);
  });

  it("unset (null) suppresses the verdict line, matching the off presentation", () => {
    renderControl({ value: null, verdict: APPLIED_VERDICT });
    openTooltip();
    expect(screen.queryAllByTestId("imc-verdict-line")).toEqual([]);
  });
});

describe("IntelligentModelControl — forbidden vocabulary", () => {
  // The locked interaction contract bans these words from anything
  // user-visible; rendered text is the user-visible surface.
  const FORBIDDEN = [/cost/i, /routing/i, /\bauto\b/i, /spec default/i];

  it.each([
    ["off", { value: "off" as const }],
    ["unset", { value: null }],
    ["on, no verdict", { value: "on" as const }],
    ["on with applied verdict", { value: "on" as const, verdict: APPLIED_VERDICT }],
    [
      "on with shadow verdict",
      { value: "on" as const, verdict: { ...APPLIED_VERDICT, applied: false } },
    ],
  ])("renders no forbidden words in state: %s", (_name, props) => {
    renderControl(props);
    openTooltip();
    const visibleText = document.body.textContent ?? "";
    for (const pattern of FORBIDDEN) {
      expect(visibleText).not.toMatch(pattern);
    }
  });
});

describe("IntelligentModelControl — verdict ping & motion", () => {
  it("does not ping on initial mount with a pre-existing verdict", () => {
    // A reload with last turn's verdict must stay quiet — the ping is a
    // "fresh verdict just landed" signal, not an "a verdict exists" one.
    renderControl({ value: "on", verdict: APPLIED_VERDICT });
    expect(screen.queryAllByTestId("imc-verdict-ping")).toEqual([]);
  });

  it("pings once when the first verdict arrives while mounted (turn end)", () => {
    const { rerender } = renderControl({ value: "on", verdict: null });
    rerender(controlTree({ value: "on", verdict: APPLIED_VERDICT }));
    // Exactly one ring: the key-remount replay renders a single span.
    expect(screen.getAllByTestId("imc-verdict-ping")).toHaveLength(1);
  });

  it("pings when a verdict is replaced by a fresh one", () => {
    const { rerender } = renderControl({ value: "on", verdict: APPLIED_VERDICT });
    rerender(
      controlTree({
        value: "on",
        verdict: { ...APPLIED_VERDICT, model: "databricks-claude-sonnet-4-6", tier: "medium" },
      }),
    );
    expect(screen.getAllByTestId("imc-verdict-ping")).toHaveLength(1);
  });

  it("never pings while the control is off", () => {
    const { rerender } = renderControl({ value: "off", verdict: null });
    rerender(controlTree({ value: "off", verdict: APPLIED_VERDICT }));
    // Off = routing inactive; a ping on a disabled-looking glyph would lie.
    expect(screen.queryAllByTestId("imc-verdict-ping")).toEqual([]);
  });

  it("drives all motion from CSS classes, never JS timers (reduced motion respected)", () => {
    // Reduced motion is honored structurally: every animated piece is a
    // class-driven CSS transition/animation declared in index.css, whose
    // universal prefers-reduced-motion gate collapses all durations. A
    // JS-timer implementation would bypass that gate — so prove there is
    // no timer behind the ping, and that the morph keeps both glyph
    // faces mounted (a conditional render couldn't cross-fade in CSS).
    vi.useFakeTimers();
    try {
      const { container, rerender } = renderControl({ value: "on", verdict: null });
      rerender(controlTree({ value: "on", verdict: APPLIED_VERDICT }));
      // Ping = a one-shot CSS animation class, replayed by key remount.
      expect(screen.getByTestId("imc-verdict-ping").className).toContain("imc-ping");
      // Both layers stay in the DOM; data-mode drives the CSS cross-fade.
      expect(container.querySelector(".imc-layer-off")).not.toBeNull();
      expect(container.querySelector(".imc-layer-on")).not.toBeNull();
      // No pending timer = nothing for prefers-reduced-motion to miss.
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });
});
