import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ANIMATION_STEP_MS, throttleAnimations } from "./throttledAnimations";

function fakeAnimation(iterations: number) {
  return {
    currentTime: null as number | null,
    pause: vi.fn(),
    effect: { getTiming: () => ({ iterations }) },
  };
}

function animatedElement(animations: ReturnType<typeof fakeAnimation>[]): HTMLElement {
  const element = document.createElement("span");
  // jsdom has no Web Animations API.
  Object.assign(element, { getAnimations: vi.fn(() => animations) });
  document.body.append(element);
  return element;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(performance, "now").mockReturnValue(1234);
});

afterEach(() => {
  document.body.replaceChildren();
  vi.advanceTimersByTime(ANIMATION_STEP_MS);
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("throttleAnimations", () => {
  it("pauses infinite animations and advances them on the shared step", () => {
    const looping = fakeAnimation(Infinity);
    const reducedMotion = fakeAnimation(1);
    const element = animatedElement([looping, reducedMotion]);

    throttleAnimations(element);
    expect(looping.pause).not.toHaveBeenCalled();
    vi.advanceTimersByTime(ANIMATION_STEP_MS);

    expect(element.getAnimations).toHaveBeenCalledWith({ subtree: true });
    expect(looping.pause).toHaveBeenCalled();
    expect(looping.currentTime).toBe(1234);
    expect(reducedMotion.pause).not.toHaveBeenCalled();
    expect(reducedMotion.currentTime).toBeNull();

    vi.mocked(performance.now).mockReturnValue(1334);
    vi.advanceTimersByTime(ANIMATION_STEP_MS);
    expect(looping.currentTime).toBe(1334);
  });

  it("seeks an animation once when nested registrations share it", () => {
    const shared = fakeAnimation(Infinity);

    throttleAnimations(animatedElement([shared]));
    throttleAnimations(animatedElement([shared]));
    vi.advanceTimersByTime(ANIMATION_STEP_MS);

    expect(shared.pause).toHaveBeenCalledTimes(1);
  });

  it("skips steps while the document is hidden", () => {
    const looping = fakeAnimation(Infinity);
    vi.spyOn(document, "hidden", "get").mockReturnValue(true);

    throttleAnimations(animatedElement([looping]));
    vi.advanceTimersByTime(ANIMATION_STEP_MS);

    expect(looping.pause).not.toHaveBeenCalled();
  });

  it("runs one timer for every element and stops it once they leave the document", () => {
    const setIntervalSpy = vi.spyOn(globalThis, "setInterval");
    const clearIntervalSpy = vi.spyOn(globalThis, "clearInterval");
    const first = animatedElement([fakeAnimation(Infinity)]);
    const second = animatedElement([fakeAnimation(Infinity)]);

    throttleAnimations(first);
    throttleAnimations(second);
    expect(setIntervalSpy).toHaveBeenCalledTimes(1);

    first.remove();
    vi.advanceTimersByTime(ANIMATION_STEP_MS);
    expect(first.getAnimations).not.toHaveBeenCalled();
    expect(second.getAnimations).toHaveBeenCalledTimes(1);
    expect(clearIntervalSpy).not.toHaveBeenCalled();

    second.remove();
    vi.advanceTimersByTime(ANIMATION_STEP_MS);
    expect(clearIntervalSpy).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("does nothing where the Web Animations API is unavailable", () => {
    const setIntervalSpy = vi.spyOn(globalThis, "setInterval");
    const element = document.createElement("span");
    document.body.append(element);

    throttleAnimations(element);
    throttleAnimations(null);

    expect(setIntervalSpy).not.toHaveBeenCalled();
  });
});
