import { expect, it } from "vitest";
import { sessionAgentIdentity } from "./sessionAgentIdentity";

it.each([
  [
    { agentName: "claude-native-ui (fork one) (switch two)", harness: "claude-native" },
    "Claude Code",
  ],
  [{ agentName: "codex-native-ui", harness: "codex-native" }, "Codex"],
  [{ agentName: "pi-native-ui" }, "Pi"],
  [{ agentName: "maestro (fork one)", harness: "codex-native" }, "Maestro"],
  [{ agentName: "debby (fork one)", harness: "claude-native" }, "Debby"],
  [{ harness: "claude-native" }, "Claude Code"],
  [{ labels: { "omnigent.wrapper": "codex-native-ui-subagent" } }, "Codex"],
] as const)("resolves agent identity from %j", (metadata, displayName) => {
  expect(sessionAgentIdentity(metadata)?.displayName).toBe(displayName);
  expect(sessionAgentIdentity(metadata)?.name).not.toMatch(/\((fork|switch)/);
});

it("leaves missing agent identity unknown", () => {
  expect(sessionAgentIdentity({})).toBeNull();
  expect(sessionAgentIdentity({ agentName: null, harness: null })).toBeNull();
});
