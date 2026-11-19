import { afterEach, describe, expect, it } from "vitest";

import { createThingyGestureGate } from "./thingyGestureGate";

// A page with a host control and a frame host holding the thingy's frame element. The browser's
// activation flag is the one input that jsdom lacks, so the test supplies it; focus and the host
// page's events are real DOM.
function setUp({ active = true }: { active?: boolean } = {}) {
  const hostButton = document.createElement("button");
  hostButton.textContent = "Composer";
  const frameHost = document.createElement("div");
  frameHost.tabIndex = -1;
  const frame = document.createElement("iframe");
  frameHost.append(frame);
  document.body.append(hostButton, frameHost);
  let clock = 10_000;
  const state = { active };
  const gate = createThingyGestureGate({
    frameHost,
    hasActivation: () => state.active,
    now: () => clock,
  });
  const advance = (ms: number) => {
    clock += ms;
  };
  /** A click or Tab into the thingy, as the host sees it: the frame takes focus. */
  const gestureInFrame = () => frame.focus();
  const clickHost = () => {
    hostButton.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true }));
    hostButton.focus();
  };
  return { gate, frame, frameHost, hostButton, state, advance, gestureInFrame, clickHost };
}

afterEach(() => {
  document.body.replaceChildren();
});

describe("thingy gesture gate", () => {
  it("allows one call after a click inside the thingy", () => {
    const { gate, gestureInFrame } = setUp();

    gestureInFrame();

    expect(gate.claim()).toBe(true);
    gate.dispose();
  });

  it("refuses calls when the activation came from a click elsewhere in Omnigent", () => {
    const { gate, clickHost } = setUp();

    clickHost();

    expect(gate.claim()).toBe(false);
    gate.dispose();
  });

  it("refuses a click elsewhere that kept focus in the thingy", () => {
    const { gate, gestureInFrame, hostButton, frame } = setUp();
    gestureInFrame();

    // A host control that prevents focus changes on mousedown leaves focus in the frame.
    hostButton.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));

    expect(document.activeElement).toBe(frame);
    expect(gate.claim()).toBe(false);
    gate.dispose();
  });

  it("refuses calls without transient activation, even with focus in the thingy", () => {
    const { gate, gestureInFrame } = setUp({ active: false });

    gestureInFrame();

    expect(gate.claim()).toBe(false);
    gate.dispose();
  });

  it("uses up a gesture: a timer in the thingy cannot make a second call", () => {
    const { gate, gestureInFrame, frameHost, advance } = setUp();
    gestureInFrame();
    expect(gate.claim()).toBe(true);

    // Focus left the frame, so a new gesture there is one the host can see.
    expect(document.activeElement).toBe(frameHost);
    advance(5_000);
    expect(gate.claim()).toBe(false);

    gestureInFrame();
    expect(gate.claim()).toBe(true);
    gate.dispose();
  });

  it("spaces calls apart even when the thingy refocuses itself at once", () => {
    const { gate, gestureInFrame, advance } = setUp();
    gestureInFrame();
    expect(gate.claim()).toBe(true);

    gestureInFrame();
    expect(gate.claim()).toBe(false);

    advance(1_000);
    gestureInFrame();
    expect(gate.claim()).toBe(true);
    gate.dispose();
  });

  it("reports focus in the frame, where a selection can come from", () => {
    const { gate, gestureInFrame, clickHost } = setUp();

    gestureInFrame();
    expect(gate.frameFocused()).toBe(true);
    clickHost();
    expect(gate.frameFocused()).toBe(false);
    gate.dispose();
  });
});
