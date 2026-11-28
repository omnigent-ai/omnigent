import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import type { SandboxResumeStatus } from "@/lib/sandboxResume";
import {
  AVAILABLE,
  type CommandAvailability,
  HIDDEN,
  unavailableBecause,
  usePaletteCommandsStore,
} from "@/store/paletteCommands";
import { registeredPaletteCommands } from "@/test/paletteCommands";

import {
  CHECKING_ACCESS_REASON,
  type CommandSession,
  CommandContextProvider,
} from "./commandContext";
import {
  RESUME_SANDBOX_LABEL,
  RESUME_SANDBOX_NEEDS_OWNER,
  resumeSandboxAvailability,
} from "./resumeSandboxAvailability";
import { ResumeSandboxPaletteCommand, resumeSandboxPaletteCommands } from "./resumeSandboxCommand";

const sandbox = vi.hoisted(() => ({
  availability: { available: true } as CommandAvailability,
  resume: vi.fn(),
  sessionIds: [] as (string | null | undefined)[],
}));
vi.mock("@/hooks/useSandboxResume", () => ({
  useSandboxResume: (sessionId: string | null | undefined) => {
    sandbox.sessionIds.push(sessionId);
    return {
      status: "asleep",
      resumeStatus: "asleep",
      failure: null,
      availability: sandbox.availability,
      resume: sandbox.resume,
    };
  },
}));

function session(permissionLevel: number | null, permissionResolved = true): CommandSession {
  return { id: "conv_1", permissionLevel, permissionResolved, topLevel: true, archived: false };
}

function mount(routed: CommandSession | null, quickPick = true) {
  return (
    <CapabilitiesProvider
      info={{ ...FALLBACK_SERVER_INFO, features: { quick_pick_palette: quickPick } }}
    >
      <CommandContextProvider session={routed} view="session" workspaceTab={null} filePath={null}>
        <ResumeSandboxPaletteCommand />
      </CommandContextProvider>
    </CapabilitiesProvider>
  );
}

beforeEach(() => {
  sandbox.availability = AVAILABLE;
  sandbox.resume.mockReset();
  sandbox.sessionIds = [];
});

afterEach(() => {
  cleanup();
  usePaletteCommandsStore.setState({ groups: {} });
});

describe("resumeSandboxAvailability", () => {
  it.each<SandboxResumeStatus>(["none", "resuming"])(
    "hides the action while the sandbox is %s",
    (status) => {
      expect(resumeSandboxAvailability(session(4), status)).toEqual(HIDDEN);
    },
  );

  it.each<SandboxResumeStatus>(["asleep", "failed"])(
    "offers it to the owner while %s",
    (status) => {
      expect(resumeSandboxAvailability(session(4), status)).toEqual(AVAILABLE);
      // A single-user server reports no level.
      expect(resumeSandboxAvailability(session(null), status)).toEqual(AVAILABLE);
    },
  );

  it.each([1, 2])("leaves it read-only for a non-owner at level %i", (level) => {
    expect(resumeSandboxAvailability(session(level), "asleep")).toEqual(
      unavailableBecause(RESUME_SANDBOX_NEEDS_OWNER),
    );
  });

  it("greys it out while access is unconfirmed", () => {
    expect(resumeSandboxAvailability(session(4, false), "asleep")).toEqual(
      unavailableBecause(CHECKING_ACCESS_REASON),
    );
    expect(resumeSandboxAvailability(null, "asleep")).toEqual(HIDDEN);
  });
});

describe("resumeSandboxPaletteCommands", () => {
  it("lists nothing without a session or while the action is hidden", () => {
    expect(resumeSandboxPaletteCommands(null, AVAILABLE, vi.fn())).toEqual([]);
    expect(resumeSandboxPaletteCommands("conv_1", HIDDEN, vi.fn())).toEqual([]);
  });
});

describe("ResumeSandboxPaletteCommand", () => {
  it("offers Resume sandbox for the open session's sandbox, running the shared resume", () => {
    render(mount(session(4)));

    expect(sandbox.sessionIds.at(-1)).toBe("conv_1");
    const [command, ...rest] = registeredPaletteCommands();
    expect(rest).toEqual([]);
    expect(command).toMatchObject({
      id: "conversation.resume-sandbox:conv_1",
      label: RESUME_SANDBOX_LABEL,
      availability: AVAILABLE,
    });
    command!.run();
    expect(sandbox.resume).toHaveBeenCalledOnce();
  });

  it("lists it greyed out with the reason for an editor who isn't the owner", () => {
    sandbox.availability = unavailableBecause(RESUME_SANDBOX_NEEDS_OWNER);
    render(mount(session(2)));

    const [command] = registeredPaletteCommands();
    expect(command?.availability).toEqual(unavailableBecause(RESUME_SANDBOX_NEEDS_OWNER));
  });

  it("withdraws it once the sandbox has nothing to resume", () => {
    const view = render(mount(session(4)));
    expect(registeredPaletteCommands()).toHaveLength(1);

    sandbox.availability = HIDDEN;
    view.rerender(mount(session(4)));

    expect(registeredPaletteCommands()).toEqual([]);
  });

  it("offers nothing without an open session, or with the QuickPick palette off", () => {
    render(mount(null));
    expect(registeredPaletteCommands()).toEqual([]);
    cleanup();

    render(mount(session(4), false));
    expect(registeredPaletteCommands()).toEqual([]);
  });
});
