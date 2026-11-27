import { render } from "@testing-library/react";
import { useEffect } from "react";
import { describe, expect, it } from "vitest";

import { githubPollFocus } from "@/lib/githubPollFocus";

const mounted = { count: 0, resetsWhileMounted: 0 };

// A component still mounted during the shared setup's reset could run a pending frame against
// state the test file's own teardown already destroyed.
const resetFocus = githubPollFocus.resetForTests;
githubPollFocus.resetForTests = () => {
  if (mounted.count > 0) mounted.resetsWhileMounted += 1;
  resetFocus();
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

  it("unmounted the previous test's components before resetting shared state", () => {
    expect(mounted.count).toBe(0);
    expect(mounted.resetsWhileMounted).toBe(0);
  });
});
