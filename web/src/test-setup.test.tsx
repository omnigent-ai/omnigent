import { render } from "@testing-library/react";
import { useEffect } from "react";
import { describe, expect, it } from "vitest";

import { githubPollFocus } from "@/lib/githubPollFocus";
import { githubPollIdle } from "@/lib/githubPollIdle";

const mounted = { count: 0, resetsWhileMounted: 0, focusResets: 0, idleResets: 0 };

// A component still mounted during the shared setup's reset could run a pending frame against
// state the test file's own teardown already destroyed.
const resetFocus = githubPollFocus.resetForTests;
githubPollFocus.resetForTests = () => {
  mounted.focusResets += 1;
  if (mounted.count > 0) mounted.resetsWhileMounted += 1;
  resetFocus();
};
const resetIdle = githubPollIdle.resetForTests;
githubPollIdle.resetForTests = () => {
  mounted.idleResets += 1;
  if (mounted.count > 0) mounted.resetsWhileMounted += 1;
  resetIdle();
};

function Probe() {
  useEffect(() => {
    mounted.count += 1;
    return () => {
      mounted.count -= 1;
    };
  }, []);
  return null;
}

describe("shared test setup", () => {
  it("renders a component that the setup's teardown must unmount", () => {
    render(<Probe />);
    expect(mounted.count).toBe(1);
  });

  it("reset both poll clocks after the previous test, once it was unmounted", () => {
    expect(mounted.count).toBe(0);
    expect(mounted.focusResets).toBe(1);
    expect(mounted.idleResets).toBe(1);
    expect(mounted.resetsWhileMounted).toBe(0);
  });
});
