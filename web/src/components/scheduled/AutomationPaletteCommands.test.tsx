import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import {
  isCommandAvailable,
  isCommandListed,
  usePaletteCommandsStore,
} from "@/store/paletteCommands";

import { AutomationPaletteCommands } from "./AutomationPaletteCommands";
import { type AutomationDetailFacts, automationDetailActions } from "./automationDetailActions";
import { OWNER_ONLY_REASONS } from "./ownerOnlyActions";

const FACTS: AutomationDetailFacts = {
  readOnly: false,
  busy: false,
  triggerMode: false,
  runStarting: false,
  paused: false,
  canShare: true,
};

const handlers = () => ({
  "run-now": vi.fn(),
  edit: vi.fn(),
  share: vi.fn(),
  pause: vi.fn(),
  resume: vi.fn(),
});

function renderCommands(facts: AutomationDetailFacts, run = handlers(), flag = true) {
  const info = { ...FALLBACK_SERVER_INFO, features: { quick_pick_palette: flag } };
  render(
    <CapabilitiesProvider info={info}>
      <AutomationPaletteCommands taskId="t1" actions={automationDetailActions(facts)} run={run} />
    </CapabilitiesProvider>,
  );
  return run;
}
const commands = () =>
  Object.values(usePaletteCommandsStore.getState().groups).flatMap((group) => group.commands);
const command = (label: string) => commands().find((candidate) => candidate.label === label)!;

afterEach(() => {
  cleanup();
  usePaletteCommandsStore.setState({ groups: {} });
});

describe("AutomationPaletteCommands", () => {
  it("runs the header's handlers, leaving Delete to its button", () => {
    const run = renderCommands(FACTS);
    const listed = commands().filter((candidate) => isCommandListed(candidate.availability));
    expect(listed.map((candidate) => candidate.label)).toEqual([
      "Run automation now",
      "Edit automation…",
      "Share automation…",
      "Pause automation",
    ]);
    act(() => command("Run automation now").run());
    act(() => command("Pause automation").run());
    expect(run["run-now"]).toHaveBeenCalledTimes(1);
    expect(run.pause).toHaveBeenCalledTimes(1);
    // Edit and Share open dialogs, so they wait for the palette to close.
    expect(command("Edit automation…").movesFocus).toBe(true);
    expect(command("Pause automation").movesFocus).toBe(false);
  });

  it("greys what the header greys, saying why", () => {
    renderCommands({ ...FACTS, readOnly: true, paused: true });
    const resume = command("Resume automation");
    expect(isCommandAvailable(resume.availability)).toBe(false);
    expect(resume.availability).toMatchObject({ reason: OWNER_ONLY_REASONS.resume });
  });

  it("offers nothing with the flag off", () => {
    renderCommands(FACTS, handlers(), false);
    expect(commands()).toEqual([]);
  });
});
