// Tests for the pure helpers in useGithub — the 404 → reason classifier that
// steers an outdated host to the "update your host" panel state.

import { describe, expect, it } from "vitest";
import { githubNotFoundReason } from "@/hooks/useGithub";

describe("githubNotFoundReason", () => {
  it("flags an outdated host from its 'resource not found' message", () => {
    // The exact shape an old runner's generic resource lookup returns.
    expect(githubNotFoundReason("Resource 'github' not found")).toBe("host_outdated");
    // Case-insensitive, tolerant of quoting.
    expect(githubNotFoundReason("resource github not found")).toBe("host_outdated");
  });

  it("treats every other 404 as a generic no-workspace reason", () => {
    expect(githubNotFoundReason("workspace directory does not exist on host")).toBe("no_os_env");
    expect(githubNotFoundReason("Resource 'terminal' not found")).toBe("no_os_env");
    expect(githubNotFoundReason(undefined)).toBe("no_os_env");
    expect(githubNotFoundReason("")).toBe("no_os_env");
  });
});
