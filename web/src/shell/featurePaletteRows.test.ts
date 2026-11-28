import { FileIcon } from "lucide-react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  AVAILABLE,
  HIDDEN,
  type PaletteCommand,
  unavailableBecause,
  usePaletteCommandsStore,
} from "@/store/paletteCommands";

import { featurePaletteSource } from "./featurePaletteRows";

const command = (id: string, overrides: Partial<PaletteCommand> = {}): PaletteCommand => ({
  id,
  label: `Run ${id}`,
  icon: FileIcon,
  keywords: [id],
  run: vi.fn(),
  ...overrides,
});

function offer(source: string, heading: string, commands: PaletteCommand[]) {
  usePaletteCommandsStore.getState().register(source, { heading, commands });
}

afterEach(() => usePaletteCommandsStore.setState({ groups: {} }));

describe("featurePaletteSource", () => {
  it("lists each feature's runnable and explained commands under its heading", () => {
    offer("files", "Files", [
      command("open", { availability: AVAILABLE, nativeShortcut: { key: "o", mod: true } }),
      command("save", { availability: unavailableBecause("Nothing to save") }),
      command("hidden", { availability: HIDDEN }),
    ]);
    offer("review", "Review", [command("next")]);

    const rows = featurePaletteSource.rows();
    expect(rows.map((row) => [row.heading, row.label, row.disabledReason])).toEqual([
      ["Files", "Run open", undefined],
      ["Files", "Run save", "Nothing to save"],
      ["Review", "Run next", undefined],
    ]);
    expect(rows[0]!.shortcut?.at(-1)).toBe("O");
    // The same rows until a feature changes what it offers.
    expect(featurePaletteSource.rows()).toBe(rows);
    offer("review", "Review", [command("previous")]);
    expect(featurePaletteSource.rows()).not.toBe(rows);
  });

  it("leaves out settings and commands that ask which first, which need a page", () => {
    offer("view", "View", [
      command("plain"),
      command("choose", { options: () => [] }),
      command("wrap", {
        setting: {
          label: "Wrap lines",
          choices: [],
          current: () => undefined,
          choose: () => undefined,
        } as unknown as PaletteCommand["setting"],
      }),
    ]);
    expect(featurePaletteSource.rows().map((row) => row.label)).toEqual(["Run plain"]);
  });

  it("runs only the command offered now, and nothing once it's withdrawn or unavailable", () => {
    const first = command("go");
    offer("history", "Workspace", [first]);
    const [row] = featurePaletteSource.rows();

    const replaced = command("go");
    offer("history", "Workspace", [replaced]);
    expect(featurePaletteSource.command(row!.id)).toBe(replaced);

    offer("history", "Workspace", [command("go", { availability: unavailableBecause("Wait") })]);
    expect(featurePaletteSource.command(row!.id)).toBeNull();

    usePaletteCommandsStore.getState().unregister("history");
    expect(featurePaletteSource.command(row!.id)).toBeNull();
  });
});
