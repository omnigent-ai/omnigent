import { describe, expect, it } from "vitest";

import {
  harnessInstallableOnHost,
  harnessUnavailableReasonOnHost,
  harnessUnconfiguredOnHost,
  resolveSetupSteps,
  setupProgress,
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

// A codex-shaped server descriptor: one-click install, then a login command.
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
    title: "Sign in to Codex",
    detail: "Uses your ChatGPT subscription — sign in on the host.",
    action: "command",
    command: "codex login",
    status_key: "authed",
  },
];

// A pi-shaped descriptor: install, then an untracked "omnigent setup" credential step.
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
    title: "Add a Pi credential",
    detail: "Pi needs an API key or gateway. Set it up on the host for now.",
    action: "setup",
    command: "omnigent setup",
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
    // Pi's credential step (status_key: null) can't be tracked; showing it
    // pre-install then vanishing post-install is confusing, so it's dropped —
    // leaving just the trackable install step.
    const steps = resolveSetupSteps(PI_STEPS, "pi", hostWith({ pi: false }));
    expect(steps).toHaveLength(1);
    expect(steps[0].kind).toBe("install");
  });

  it("keeps a sole untrackable step (non-installable harness fallback)", () => {
    // A generic "run omnigent setup" step is untrackable but must still show —
    // it's the only guidance for a harness the UI can't install.
    const generic: SetupStepWire[] = [
      {
        kind: "install",
        title: "Set up on the host",
        detail: "Run omnigent setup on the host.",
        action: "setup",
        command: "omnigent setup",
        status_key: null,
      },
    ];
    const steps = resolveSetupSteps(generic, "cursor-native", hostWith({ "cursor-native": false }));
    expect(steps).toHaveLength(1);
    expect(steps[0].command).toBe("omnigent setup");
  });

  it("returns [] with no descriptor or no harness", () => {
    expect(resolveSetupSteps(undefined, "codex", hostWith({ codex: false }))).toEqual([]);
    expect(resolveSetupSteps(CODEX_STEPS, null, hostWith({ codex: false }))).toEqual([]);
  });
});

describe("setupProgress", () => {
  it("counts tracked steps for the progress header", () => {
    // Codex needs-auth: install done, auth todo → 1 of 2.
    const codex = resolveSetupSteps(CODEX_STEPS, "codex", hostWith({ codex: "needs-auth" }));
    expect(setupProgress(codex)).toEqual({ done: 1, total: 2 });
    // Pi (install-only after dropping the untrackable step): 0 of 1 when missing.
    const pi = resolveSetupSteps(PI_STEPS, "pi", hostWith({ pi: false }));
    expect(setupProgress(pi)).toEqual({ done: 0, total: 1 });
  });
});
