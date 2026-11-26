import { describe, expect, it, vi } from "vitest";

vi.mock("./GoToFileDialog", () => ({ GoToFileDialog: () => null }));
const { nextOpenRequest, openRequestKey } = await import("./GoToFileHost");

const files = { id: 1, kind: "files" as const };
const text = { id: 2, kind: "text" as const };

describe("nextOpenRequest", () => {
  it("opens on the mode asked for, with the request's text", () => {
    expect(nextOpenRequest(null, { ...text, query: "needle" })).toEqual({
      key: "files:2",
      query: "needle",
      mode: "text",
    });
  });

  it("switches to the other mode in place, keeping the opening", () => {
    const open = nextOpenRequest(null, files)!;
    expect(nextOpenRequest(open, { ...text, toggle: true })).toEqual({ ...open, mode: "text" });
  });

  it.each([
    ["the other mode", text],
    ["the mode showing", files],
  ])("opens afresh on a request's text from %s, even empty text", (_case, kind) => {
    const open = nextOpenRequest(null, { ...files, query: "guide" })!;
    for (const query of ["needle", ""]) {
      expect(nextOpenRequest(open, { ...kind, id: 7, query })).toEqual({
        key: "files:7",
        query,
        mode: kind.kind,
      });
    }
  });

  it("closes from the showing mode's chord, and stays open for its ⌘K row", () => {
    const open = nextOpenRequest(null, text)!;
    expect(nextOpenRequest(open, { ...text, id: 3, toggle: true })).toBeNull();
    expect(nextOpenRequest(open, { ...text, id: 3 })).toBe(open);
  });

  it("keys file and session requests apart, since each store counts from 1", () => {
    expect(openRequestKey(files)).toBe("files:1");
    expect(openRequestKey(text)).toBe("files:2");
    expect(openRequestKey({ id: 1, kind: "sessions" })).toBe("sessions:1");
  });
});
