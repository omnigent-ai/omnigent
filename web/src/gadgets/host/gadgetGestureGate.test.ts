import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { createGadgetGestureGate } from "./gadgetGestureGate";

// A page with a host control and a frame host holding the gadget's frame element. The browser's
// activation flag is the one input that jsdom lacks, so the test supplies it; focus and the host
// page's events are real DOM.
// Browsers report focus entering a frame differently: some fire focusin on the frame element,
// while Chrome fires none and only blurs the host window, with the frame already active.
type FrameFocusReport = "focusin" | "window blur";

let frameFocusReport: FrameFocusReport = "focusin";

function setUp({ active = true }: { active?: boolean | null } = {}) {
  const hostButton = document.createElement("button");
  hostButton.textContent = "Composer";
  const frameHost = document.createElement("div");
  frameHost.tabIndex = -1;
  const frame = document.createElement("iframe");
  frameHost.append(frame);
  document.body.append(hostButton, frameHost);
  let clock = 10_000;
  const state: { active: boolean | null } = { active };
  const gate = createGadgetGestureGate({
    frameHost,
    hasActivation: () => state.active,
    now: () => clock,
  });
  const advance = (ms: number) => {
    clock += ms;
  };
  /** A click or Tab into the gadget, as the host sees it: the frame takes focus. */
  const gestureInFrame = () => {
    if (frameFocusReport === "focusin") {
      frame.focus();
      return;
    }
    const hideFocusIn = (event: Event) => event.stopPropagation();
    window.addEventListener("focusin", hideFocusIn, true);
    frame.focus();
    window.removeEventListener("focusin", hideFocusIn, true);
    window.dispatchEvent(new FocusEvent("blur"));
  };
  const clickHost = () => {
    hostButton.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true }));
    hostButton.focus();
  };
  return { gate, frame, frameHost, hostButton, state, advance, gestureInFrame, clickHost };
}

afterEach(() => {
  document.body.replaceChildren();
});

describe.each<FrameFocusReport>(["focusin", "window blur"])(
  "gadget gesture gate (%s)",
  (report) => {
    beforeEach(() => {
      frameFocusReport = report;
    });

    it("allows one call after a click inside the gadget", () => {
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

    it("refuses a click elsewhere that kept focus in the gadget", () => {
      const { gate, gestureInFrame, hostButton, frame } = setUp();
      gestureInFrame();

      // A host control that prevents focus changes on mousedown leaves focus in the frame.
      hostButton.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));

      expect(document.activeElement).toBe(frame);
      expect(gate.claim()).toBe(false);
      gate.dispose();
    });

    it("refuses calls without transient activation, even with focus in the gadget", () => {
      const { gate, gestureInFrame } = setUp({ active: false });

      gestureInFrame();

      expect(gate.claim()).toBe(false);
      gate.dispose();
    });

    it("uses up a gesture: a timer in the gadget cannot make a second call", () => {
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

    it("spaces calls apart even when the gadget refocuses itself at once", () => {
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

    it("without the activation API, allows a call only just after the frame took focus", () => {
      const { gate, gestureInFrame, advance } = setUp({ active: null });

      gestureInFrame();
      advance(1_000);
      expect(gate.claim()).toBe(true);

      // A gadget left focused can't act for the viewer later.
      advance(1_000);
      gestureInFrame();
      advance(60_000);
      expect(gate.claim()).toBe(false);
      gate.dispose();
    });

    it("treats a browser without navigator.userActivation as unable to tell", () => {
      expect("userActivation" in navigator).toBe(false);
      const frameHost = document.createElement("div");
      frameHost.tabIndex = -1;
      const frame = document.createElement("iframe");
      frameHost.append(frame);
      document.body.append(frameHost);
      let clock = 10_000;
      const gate = createGadgetGestureGate({ frameHost, now: () => clock });

      frame.focus();
      if (report === "window blur") window.dispatchEvent(new FocusEvent("blur"));
      clock += 60_000;

      expect(gate.claim()).toBe(false);
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
  },
);
