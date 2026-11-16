import { describe, expect, it } from "vitest";
import { gridLayoutForSessions, type SessionGrid } from "./gridLayout";
import { recordUnsavedWrite, withUnsaved } from "./unsavedGrids";

const grid = (id: string, name?: string): SessionGrid => ({
  id,
  ...(name ? { name } : {}),
  layout: gridLayoutForSessions([`${id}-1`, `${id}-2`])!,
});
// Storage hands back fresh parsed copies, as `parseSessionGrids` does.
const stored = (...grids: SessionGrid[]): SessionGrid[] => JSON.parse(JSON.stringify(grids));

describe("unsaved grid changes", () => {
  it("keeps a new grid and an edit until storage moves on", () => {
    const saved = stored(grid("g", "Saved"));
    const renamed = { ...saved[0], name: "Mine" };
    const added = grid("u", "Unsaved");
    const unsaved = recordUnsavedWrite(undefined, saved, saved, [renamed, added]);

    expect(withUnsaved(stored(grid("g", "Saved")), unsaved)).toEqual([renamed, added]);
    // Another tab renamed it later: its rename wins, and the new grid stays.
    expect(withUnsaved(stored(grid("g", "Theirs")), unsaved)).toEqual([grid("g", "Theirs"), added]);
    // Another tab deleted it later: the edit doesn't bring it back.
    expect(withUnsaved(stored(), unsaved)).toEqual([added]);
  });

  it("hides a deleted grid only while storage still holds the copy it deleted", () => {
    const saved = stored(grid("g", "Saved"), grid("h"));
    const unsaved = recordUnsavedWrite(undefined, saved, saved, [saved[1]]);

    expect(withUnsaved(stored(grid("g", "Saved"), grid("h")), unsaved)).toEqual([grid("h")]);
    expect(withUnsaved(stored(grid("g", "Edited"), grid("h")), unsaved)).toEqual([
      grid("g", "Edited"),
      grid("h"),
    ]);
  });

  it("ignores grids the write never saw, so an older list can't delete them", () => {
    // This tab built its write before another tab's grid N reached it.
    const base = stored(grid("g"));
    const unsaved = recordUnsavedWrite(undefined, stored(grid("g"), grid("n")), base, [
      ...base,
      grid("u"),
    ]);
    expect(withUnsaved(stored(grid("g"), grid("n")), unsaved)).toEqual([
      grid("g"),
      grid("n"),
      grid("u"),
    ]);
  });

  it("bases a change on the copy this tab saw, not a newer stored one", () => {
    // Another tab's edit is stored but not yet shown here when this tab renames.
    const seen = stored(grid("g", "Old"));
    const unsaved = recordUnsavedWrite(undefined, stored(grid("g", "Theirs")), seen, [
      { ...seen[0], name: "Mine" },
    ]);
    expect(withUnsaved(stored(grid("g", "Theirs")), unsaved)).toEqual([grid("g", "Theirs")]);
  });

  it("rebases a later change once another tab's edit has replaced an earlier one", () => {
    const saved = stored(grid("g", "Saved"));
    const first = recordUnsavedWrite(undefined, saved, saved, [{ ...saved[0], name: "Mine" }]);
    // Another tab's edit is stored and shown here; this tab then deletes G, and that fails too.
    const theirs = stored(grid("g", "Theirs"));
    const deleted = recordUnsavedWrite(first, theirs, theirs, []);
    expect(withUnsaved(stored(grid("g", "Theirs")), deleted)).toEqual([]);
    const edited = recordUnsavedWrite(first, theirs, theirs, [{ ...theirs[0], name: "Again" }]);
    expect(withUnsaved(stored(grid("g", "Theirs")), edited)).toEqual([
      { ...theirs[0], name: "Again" },
    ]);
  });

  it("carries earlier unsaved changes through later failed writes", () => {
    const saved = stored(grid("g", "Saved"));
    const added = grid("u", "Unsaved");
    const first = recordUnsavedWrite(undefined, saved, saved, [...saved, added]);
    const renamed = { ...added, name: "Renamed" };
    // Deleting the stored grid keeps the earlier new grid, now renamed.
    const second = recordUnsavedWrite(first, saved, [...saved, added], [renamed]);
    expect(withUnsaved(stored(grid("g", "Saved")), second)).toEqual([renamed]);
    // Deleting a grid that was never stored leaves nothing to hide.
    expect(recordUnsavedWrite(second, saved, [renamed], [])).toEqual({
      changed: new Map(),
      removed: new Map([["g", JSON.stringify(saved[0])]]),
    });
    expect(recordUnsavedWrite(undefined, saved, saved, saved)).toBeUndefined();
  });
});
