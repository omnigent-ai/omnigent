import { describe, it, expect } from "vitest";
import {
  agentRootName,
  harnessFamily,
  isNativeHarness,
  forkTargetCarriesHistory,
  switchTargetCarriesHistory,
} from "./forkHarness";

describe("harnessFamily", () => {
  it.each([
    ["claude-native", "anthropic"],
    ["native-claude", "anthropic"],
    ["claude-sdk", "anthropic"],
    ["claude_sdk", "anthropic"],
    ["codex", "openai"],
    ["codex-native", "openai"],
    ["native-codex", "openai"],
    ["openai-agents", "openai"],
    ["openai-agents-sdk", "openai"],
    ["agents_sdk", "openai"],
    ["antigravity-native", "gemini"],
    ["native-antigravity", "gemini"],
    ["antigravity", "gemini"],
  ])("maps %s → %s", (harness, family) => {
    expect(harnessFamily(harness)).toBe(family);
  });

  it.each([["mystery"], [null], [undefined], [""]])(
    "returns null for unknown/empty %s",
    (harness) => {
      expect(harnessFamily(harness as string | null | undefined)).toBeNull();
    },
  );
});

describe("isNativeHarness", () => {
  it.each([
    ["claude-native", true],
    ["native-claude", true],
    ["codex-native", true],
    ["native-codex", true],
    ["cursor-native", true],
    ["native-cursor", true],
    ["pi-native", true],
    ["native-pi", true],
    // Antigravity-native spellings are native too — aligned with Python
    // NATIVE_HARNESSES (the in-process `antigravity` SDK harness is NOT).
    ["antigravity-native", true],
    ["native-antigravity", true],
    // qwen-native rebuilds qwen's on-disk chat recording from the copied
    // Omnigent items, so it carries fork/switch history (both spellings).
    ["qwen-native", true],
    ["native-qwen", true],
    ["claude-sdk", false],
    ["claude_sdk", false],
    ["openai-agents", false],
    ["codex", false],
    // The SDK `pi` harness is in-process, not a native CLI wrapper.
    ["pi", false],
    // The in-process Antigravity SDK harness is likewise not native.
    ["antigravity", false],
    [null, false],
  ])("classifies %s as native=%s", (harness, expected) => {
    expect(isNativeHarness(harness as string | null)).toBe(expected);
  });
});

describe("forkTargetCarriesHistory", () => {
  // SDK targets always carry history as context, regardless of source or
  // family — including native → SDK and cross-family. A false here would
  // wrongly hide a fully-supported switch from the picker.
  it.each([
    ["claude-sdk"],
    ["claude_sdk"],
    ["codex"],
    ["openai-agents"],
    ["agents_sdk"],
    // antigravity is the Gemini-family SDK target.
    ["antigravity"],
  ])("SDK target %s carries history", (target) => {
    expect(forkTargetCarriesHistory(target)).toBe(true);
  });

  // Native targets carry from ANY source: the runner clones the source's
  // native transcript when the source is same-family native, else rebuilds
  // the target's on-disk transcript from the copied Omnigent items. The
  // codex-native rebuild includes the session_meta fields codex ≥ 0.133
  // requires plus the event_msg mirrors it rebuilds visible turns from
  // (verified against codex 0.136.0), so cross-family forks into
  // codex-native are offered like claude-native always was.
  it.each([
    ["claude-native"],
    ["native-claude"],
    ["codex-native"],
    ["native-codex"],
    // Hermes is a native-rebuild harness (in _FORK_HISTORY_NATIVE_HARNESSES).
    ["hermes-native"],
    ["native-hermes"],
    // Cursor / OpenCode are native but server-backed: a FORK carries history
    // as a text preamble (text-prefix replay), so both must be offered in the
    // fork picker (switch differs — see switchTargetCarriesHistory).
    ["cursor-native"],
    ["native-cursor"],
    ["opencode-native"],
    ["native-opencode"],
    // Pi is native but multi-family (no single harnessFamily) — it must
    // still be offered, or the fork/switch-agent pickers silently drop it.
    ["pi-native"],
    ["native-pi"],
    ["antigravity-native"],
    ["native-antigravity"],
    // qwen-native rebuilds qwen's on-disk recording from the copied items.
    ["qwen-native"],
    ["native-qwen"],
  ])("native fork target %s carries history", (target) => {
    expect(forkTargetCarriesHistory(target)).toBe(true);
  });

  // Native harnesses with NO carry path on the server (neither rebuild nor
  // preamble) and no single provider family — forking into them would start
  // fresh, so the fork picker must not offer them. (qwen-native DOES carry —
  // it rebuilds from items — so it is intentionally absent here.)
  it.each([["kiro-native"], ["kimi-native"], ["goose-native"]])(
    "native target %s without a carry path does NOT carry on fork",
    (target) => {
      expect(forkTargetCarriesHistory(target)).toBe(false);
    },
  );

  it("does NOT offer a target whose harness is unknown (conservative; see TODO)", () => {
    // We can't classify an unrecognised harness (the catalog may report
    // harness=null when it couldn't load the agent's bundle), so we don't
    // offer a switch we can't verify preserves history.
    expect(forkTargetCarriesHistory("mystery")).toBe(false);
    expect(forkTargetCarriesHistory(null)).toBe(false);
    expect(forkTargetCarriesHistory(undefined)).toBe(false);
  });
});

describe("switchTargetCarriesHistory", () => {
  // Switch carries for native-rebuild targets (rebuilt from items) and SDK
  // targets (replayed as context) — same as fork for these.
  it.each([
    ["claude-native"],
    ["native-claude"],
    ["codex-native"],
    ["native-codex"],
    ["pi-native"],
    ["native-pi"],
    ["hermes-native"],
    ["native-hermes"],
    // qwen-native rebuilds its on-disk recording, so it carries on switch too.
    ["qwen-native"],
    ["native-qwen"],
    ["claude-sdk"],
    ["openai-agents"],
    ["antigravity"],
    // antigravity-native currently carries via the family proxy (see the
    // forkTargetCarriesHistory NOTE); kept offered until verified.
    ["antigravity-native"],
  ])("switch target %s carries history", (target) => {
    expect(switchTargetCarriesHistory(target)).toBe(true);
  });

  // The preamble path (cursor/opencode) is FORK-ONLY: an in-place switch has
  // no first-message injection point, so switching into one starts fresh and
  // must NOT be offered — even though forkTargetCarriesHistory returns true.
  it.each([["cursor-native"], ["native-cursor"], ["opencode-native"], ["native-opencode"]])(
    "preamble target %s carries on fork but NOT on switch",
    (target) => {
      expect(forkTargetCarriesHistory(target)).toBe(true);
      expect(switchTargetCarriesHistory(target)).toBe(false);
    },
  );

  // Native harnesses with no carry path are offered by neither picker.
  it.each([["kiro-native"], ["kimi-native"], ["goose-native"]])(
    "native target %s without a carry path does NOT carry on switch",
    (target) => {
      expect(switchTargetCarriesHistory(target)).toBe(false);
    },
  );

  it("does NOT offer an unknown/absent harness", () => {
    expect(switchTargetCarriesHistory("mystery")).toBe(false);
    expect(switchTargetCarriesHistory(null)).toBe(false);
    expect(switchTargetCarriesHistory(undefined)).toBe(false);
  });
});

describe("agentRootName", () => {
  it("returns a plain name unchanged", () => {
    expect(agentRootName("claude-native-ui")).toBe("claude-native-ui");
  });

  it("peels a single fork or switch layer", () => {
    expect(agentRootName("claude-native-ui (fork ag_3a9fa87)")).toBe("claude-native-ui");
    expect(agentRootName("nessie (switch conv_9f3c)")).toBe("nessie");
  });

  it("peels every layer of a fork-of-a-fork", () => {
    // A single-layer strip would stop at "claude-native-ui (fork ag_a)";
    // agentRootName recurses to the root so a multi-fork clone of a built-in
    // still matches the built-in catalog (and is dropped by the agent picker).
    expect(agentRootName("claude-native-ui (fork ag_a) (fork ag_b)")).toBe("claude-native-ui");
    expect(agentRootName("polly (fork conv_a) (switch conv_b)")).toBe("polly");
  });

  it("leaves interior or non-clone parentheses alone", () => {
    // Only trailing clone markers are peeled — user-chosen parens survive.
    expect(agentRootName("my-agent (beta)")).toBe("my-agent (beta)");
    expect(agentRootName("agent (fork pun) helper")).toBe("agent (fork pun) helper");
  });
});
