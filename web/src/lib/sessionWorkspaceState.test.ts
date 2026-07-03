import { afterEach, describe, expect, it } from "vitest";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "./sessionWorkspaceState";

const STORAGE_KEY = "omnigent:session-workspace-state";
// Mirrors MAX_SESSIONS in the source; the pruning tests below seed exactly this
// many sessions to sit right at the cap.
const MAX_SESSIONS = 100;

afterEach(() => {
  localStorage.clear();
});

describe("sessionWorkspaceState", () => {
  it("returns an empty object for an unknown session", () => {
    // No stored entry must read as a clean "fresh session" state, not an error.
    expect(readSessionWorkspaceState("conv_unknown")).toEqual({});
  });

  it("merges partial patches into one session rather than replacing", () => {
    writeSessionWorkspaceState("conv_a", { open: true, widthPx: 480 });
    writeSessionWorkspaceState("conv_a", { rightRailTab: "subagents" });

    // The second write patches only rightRailTab; open/widthPx from the first
    // write must survive. A failure here means writes clobber the whole entry
    // instead of merging.
    expect(readSessionWorkspaceState("conv_a")).toEqual({
      open: true,
      widthPx: 480,
      rightRailTab: "subagents",
    });
  });

  it("keeps sessions isolated by id", () => {
    writeSessionWorkspaceState("conv_a", { open: true });
    writeSessionWorkspaceState("conv_b", { open: false, widthPx: 600 });

    // Writing conv_b must not bleed into conv_a — a failure means the store is
    // keying entries together or sharing state across ids.
    expect(readSessionWorkspaceState("conv_a")).toEqual({ open: true });
    expect(readSessionWorkspaceState("conv_b")).toEqual({ open: false, widthPx: 600 });
  });

  it("caps the persisted open-file tabs at 20, keeping the most recent", () => {
    // Persist 25 file tabs in open order ("f0".."f24").
    const files = Array.from({ length: 25 }, (_, i) => `f${i}`);
    writeSessionWorkspaceState("conv_files", { openFiles: files });

    // Only the most-recent 20 (f5..f24) survive; the 5 oldest are dropped.
    // Failure means the cap didn't apply (unbounded growth) or trimmed the
    // wrong end (kept the stale tabs instead of the recent ones).
    const stored = readSessionWorkspaceState("conv_files").openFiles;
    expect(stored).toEqual(Array.from({ length: 20 }, (_, i) => `f${i + 5}`));
  });

  it("prunes the least-recently-touched session past the cap (numeric ids)", () => {
    // Seed exactly MAX_SESSIONS sessions with purely numeric ids ("1".."100").
    // Numeric-string keys are the case a plain object store gets wrong: V8
    // reorders integer-like keys into ascending numeric order, so the "touch =
    // move to most-recent" refresh and oldest-first pruning would both operate
    // on numeric order instead of recency.
    for (let i = 1; i <= MAX_SESSIONS; i++) {
      writeSessionWorkspaceState(String(i), { open: true });
    }

    // Re-touch the oldest session ("1"), making it the most-recently-touched.
    // In recency order the new oldest is now "2".
    writeSessionWorkspaceState("1", { rightRailTab: "files" });

    // One more session ("101") pushes the count to MAX_SESSIONS + 1, forcing a
    // single eviction of the least-recently-touched entry.
    writeSessionWorkspaceState("101", { open: true });

    // "1" must survive because it was just refreshed. With the old object-keyed
    // store, numeric ordering would put "1" at the front and evict it here —
    // this assertion is the regression guard for that bug.
    expect(readSessionWorkspaceState("1")).toEqual({ open: true, rightRailTab: "files" });
    // "2" is now the least-recently-touched, so it is the one evicted.
    expect(readSessionWorkspaceState("2")).toEqual({});
    // The newest write is retained.
    expect(readSessionWorkspaceState("101")).toEqual({ open: true });
  });

  it("drops invalid fields and corrupt storage without throwing", () => {
    // A non-array payload (the pre-array on-disk shape, or any corruption) must
    // read as empty rather than crashing the panel on boot.
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ conv_a: { open: true } }));
    expect(readSessionWorkspaceState("conv_a")).toEqual({});

    // Malformed per-field values are dropped while valid siblings are kept,
    // proving one bad field can't poison the whole entry.
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify([{ id: "conv_b", state: { open: true, widthPx: -5, rightRailTab: "bogus" } }]),
    );
    expect(readSessionWorkspaceState("conv_b")).toEqual({ open: true });
  });
});
