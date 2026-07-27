import { describe, expect, it } from "vitest";

import {
  harnessAuthableOnHost,
  harnessCredentialAdoptFamilies,
  harnessCredentialFamily,
  harnessInstallableOnHost,
  harnessUnavailableReasonOnHost,
  harnessUnconfiguredOnHost,
  resolveSetupSteps,
} from "./harnessSetup";
import type { SetupStepWire } from "@/lib/agentLabels";
import type { Host } from "@/hooks/useHosts";
import type { ServerInfo } from "@/lib/capabilities";

const hostWith = (configured: Record<string, boolean | string> | null | undefined): Host =>
  ({
    host_id: "host_1",
    name: "laptop",
    owner: "alice",
    status: "online",
    configured_harnesses: configured,
  }) as Host;

const info = (overrides: Partial<ServerInfo> = {}): ServerInfo =>
  ({
    harness_install_enabled: true,
    installable_harnesses: ["codex", "codex-native", "pi", "pi-native"],
    ...overrides,
  }) as ServerInfo;

// A codex-shaped server descriptor: one-click install, then a UI-authable auth
// step (the form lists subscription / API key / gateway; `command` is the
// subscription option carried into it).
const CODEX_STEPS: SetupStepWire[] = [
  {
    kind: "install",
    title: "Install Codex",
    detail: "We'll install Codex on the host for you.",
    action: "install",
    command: null,
    status_key: "installed",
  },
  {
    kind: "auth",
    title: "Set up authentication",
    detail: "Sign in with your ChatGPT subscription, an API key, or a gateway.",
    action: "auth",
    command: "codex login",
    status_key: "authed",
  },
];

// A pi-shaped descriptor: install, then a UI-authable credential step
// (action "auth", tracked via status_key "authed", no CLI login command).
const PI_STEPS: SetupStepWire[] = [
  {
    kind: "install",
    title: "Install Pi",
    detail: "We'll install Pi on the host for you.",
    action: "install",
    command: null,
    status_key: "installed",
  },
  {
    kind: "auth",
    title: "Set up authentication",
    detail: "Add an API key or a gateway so Pi can run.",
    action: "auth",
    command: null,
    status_key: "authed",
  },
];

// A qwen-shaped descriptor: install, then an UNTRACKED "omni setup"
// signpost (env-auth, not UI-authable) — the case that should get dropped when
// a trackable step anchors the list.
const QWEN_STEPS: SetupStepWire[] = [
  {
    kind: "install",
    title: "Install Qwen",
    detail: "We'll install Qwen on the host for you.",
    action: "install",
    command: null,
    status_key: "installed",
  },
  {
    kind: "auth",
    title: "Add a Qwen credential",
    detail: "Qwen needs an API key or gateway. Set it up on the host for now.",
    action: "setup",
    command: "omni setup",
    status_key: null,
  },
];

describe("harnessUnavailableReasonOnHost", () => {
  it("classifies structured reasons and generic unconfigured", () => {
    expect(harnessUnavailableReasonOnHost("codex", hostWith({ codex: "binary-missing" }))).toBe(
      "binary-missing",
    );
    expect(harnessUnavailableReasonOnHost("codex", hostWith({ codex: "needs-auth" }))).toBe(
      "needs-auth",
    );
    expect(
      harnessUnavailableReasonOnHost("cursor-native", hostWith({ "cursor-native": false })),
    ).toBe("cursor-cli-missing");
    expect(harnessUnavailableReasonOnHost("pi", hostWith({ pi: false }))).toBe("unconfigured");
  });

  it("returns null when ready, unknown, or no host", () => {
    expect(harnessUnavailableReasonOnHost("codex", hostWith({ codex: true }))).toBe(null);
    expect(harnessUnavailableReasonOnHost("codex", hostWith({ codex: "future" }))).toBe(null);
    expect(harnessUnavailableReasonOnHost("codex", hostWith(null))).toBe(null);
    expect(harnessUnavailableReasonOnHost(null, hostWith({ codex: false }))).toBe(null);
  });
});

describe("harnessUnconfiguredOnHost", () => {
  it("is true exactly when there's an unavailable reason", () => {
    expect(harnessUnconfiguredOnHost("codex", hostWith({ codex: false }))).toBe(true);
    expect(harnessUnconfiguredOnHost("codex", hostWith({ codex: true }))).toBe(false);
  });
});

describe("harnessInstallableOnHost", () => {
  const online = hostWith({ codex: false });

  it("true only when feature on, host online, and harness in the set", () => {
    expect(harnessInstallableOnHost(info(), "codex-native", online)).toBe(true);
  });

  it("false when the feature is off, harness not listed, host offline, or loading", () => {
    expect(
      harnessInstallableOnHost(
        info({ harness_install_enabled: false, installable_harnesses: [] }),
        "codex",
        online,
      ),
    ).toBe(false);
    expect(harnessInstallableOnHost(info(), "cursor-native", online)).toBe(false);
    expect(
      harnessInstallableOnHost(info(), "codex", { ...online, status: "offline" } as Host),
    ).toBe(false);
    expect(harnessInstallableOnHost("loading", "codex", online)).toBe(false);
  });
});

describe("harnessCredentialFamily", () => {
  it("maps Claude/Codex/Pi spellings to their provider family", () => {
    expect(harnessCredentialFamily("claude-native")).toBe("anthropic");
    expect(harnessCredentialFamily("native-claude")).toBe("anthropic");
    expect(harnessCredentialFamily("codex")).toBe("openai");
    expect(harnessCredentialFamily("codex-native")).toBe("openai");
    // Pi resolves to its preferred anthropic fallback family.
    expect(harnessCredentialFamily("pi")).toBe("anthropic");
    expect(harnessCredentialFamily("pi-native")).toBe("anthropic");
  });

  it("returns null for harnesses the UI can't authenticate", () => {
    expect(harnessCredentialFamily("opencode-native")).toBe(null);
    expect(harnessCredentialFamily("qwen")).toBe(null);
    expect(harnessCredentialFamily("cursor-native")).toBe(null);
    expect(harnessCredentialFamily(null)).toBe(null);
    expect(harnessCredentialFamily(undefined)).toBe(null);
  });
});

describe("harnessCredentialAdoptFamilies", () => {
  it("returns the single own family for Claude/Codex", () => {
    expect(harnessCredentialAdoptFamilies("claude-native")).toEqual(["anthropic"]);
    expect(harnessCredentialAdoptFamilies("codex")).toEqual(["openai"]);
    expect(harnessCredentialAdoptFamilies("codex-native")).toEqual(["openai"]);
  });

  it("returns BOTH families for Pi (it consumes anthropic + openai)", () => {
    expect(harnessCredentialAdoptFamilies("pi")).toEqual(["anthropic", "openai"]);
    expect(harnessCredentialAdoptFamilies("pi-native")).toEqual(["anthropic", "openai"]);
  });

  it("returns an empty list for harnesses the UI can't authenticate", () => {
    expect(harnessCredentialAdoptFamilies("opencode-native")).toEqual([]);
    expect(harnessCredentialAdoptFamilies(null)).toEqual([]);
    expect(harnessCredentialAdoptFamilies(undefined)).toEqual([]);
  });
});

describe("harnessAuthableOnHost", () => {
  const online = hostWith({ codex: "needs-auth" });

  it("true for Claude/Codex/Pi families when feature on and host online", () => {
    expect(harnessAuthableOnHost(info(), "codex-native", online)).toBe(true);
    expect(harnessAuthableOnHost(info(), "claude-native", online)).toBe(true);
    expect(harnessAuthableOnHost(info(), "pi", online)).toBe(true);
  });

  it("false for env-auth / own-login harnesses, flag off, offline, or loading", () => {
    // OpenCode/Qwen (env-auth) and Cursor (own-login) are NOT UI-authable.
    expect(harnessAuthableOnHost(info(), "opencode-native", online)).toBe(false);
    expect(harnessAuthableOnHost(info(), "qwen", online)).toBe(false);
    expect(harnessAuthableOnHost(info(), "cursor-native", online)).toBe(false);
    expect(harnessAuthableOnHost(info({ harness_install_enabled: false }), "codex", online)).toBe(
      false,
    );
    expect(harnessAuthableOnHost(info(), "codex", { ...online, status: "offline" } as Host)).toBe(
      false,
    );
    expect(harnessAuthableOnHost("loading", "codex", online)).toBe(false);
  });
});

describe("resolveSetupSteps", () => {
  it("marks install todo + auth todo when the binary is missing", () => {
    const steps = resolveSetupSteps(CODEX_STEPS, "codex", hostWith({ codex: "binary-missing" }));
    expect(steps.map((s) => [s.kind, s.status])).toEqual([
      ["install", "todo"],
      ["auth", "todo"],
    ]);
    expect(steps[0].action).toBe("install");
    expect(steps[1].command).toBe("codex login");
  });

  it("marks install done + auth todo when installed but not signed in", () => {
    const steps = resolveSetupSteps(CODEX_STEPS, "codex", hostWith({ codex: "needs-auth" }));
    expect(steps.map((s) => s.status)).toEqual(["done", "todo"]);
  });

  it("marks both done when the harness is ready", () => {
    const steps = resolveSetupSteps(CODEX_STEPS, "codex", hostWith({ codex: true }));
    expect(steps.map((s) => s.status)).toEqual(["done", "done"]);
  });

  it("drops an untrackable step when a trackable step anchors the list", () => {
    // Qwen's credential step (status_key: null) can't be tracked; showing it
    // pre-install then vanishing post-install is confusing, so it's dropped —
    // leaving just the trackable install step.
    const steps = resolveSetupSteps(QWEN_STEPS, "qwen", hostWith({ qwen: false }));
    expect(steps).toHaveLength(1);
    expect(steps[0].kind).toBe("install");
  });

  it("keeps Pi's tracked auth step (needs-auth → todo, not dropped)", () => {
    // Regression: Pi's auth step is now status-tracked ("authed"), so an
    // installed-but-no-credential Pi must show BOTH steps — install ✓ and the
    // auth step to-do — not collapse to a "ready" one-step list.
    const steps = resolveSetupSteps(PI_STEPS, "pi", hostWith({ pi: "needs-auth" }));
    expect(steps.map((s) => [s.kind, s.status])).toEqual([
      ["install", "done"],
      ["auth", "todo"],
    ]);
    expect(steps[1].action).toBe("auth");
  });

  it("keeps a sole untrackable step (non-installable harness fallback)", () => {
    // A generic "run omni setup" step is untrackable but must still show —
    // it's the only guidance for a harness the UI can't install.
    const generic: SetupStepWire[] = [
      {
        kind: "install",
        title: "Set up on the host",
        detail: "Run omni setup on the host.",
        action: "setup",
        command: "omni setup",
        status_key: null,
      },
    ];
    const steps = resolveSetupSteps(generic, "cursor-native", hostWith({ "cursor-native": false }));
    expect(steps).toHaveLength(1);
    expect(steps[0].command).toBe("omni setup");
  });

  it("returns [] with no descriptor or no harness", () => {
    expect(resolveSetupSteps(undefined, "codex", hostWith({ codex: false }))).toEqual([]);
    expect(resolveSetupSteps(CODEX_STEPS, null, hostWith({ codex: false }))).toEqual([]);
  });
});
