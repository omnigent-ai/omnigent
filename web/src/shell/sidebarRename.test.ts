import { describe, expect, it, vi } from "vitest";
import { isInClosedSidebar, registerSidebarRename, startSidebarRename } from "./sidebarRename";

describe("sidebar rename targets", () => {
  it("returns false when no row is registered for the session", () => {
    expect(startSidebarRename("missing")).toBe(false);
  });

  it("uses the first row that can take the edit and stops after it", () => {
    const hidden = vi.fn(() => false);
    const shown = vi.fn(() => true);
    const later = vi.fn(() => true);
    const cleanups = [hidden, shown, later].map((start) => registerSidebarRename("conv", start));

    expect(startSidebarRename("conv")).toBe(true);
    expect(hidden).toHaveBeenCalledOnce();
    expect(shown).toHaveBeenCalledOnce();
    expect(later).not.toHaveBeenCalled();
    expect(startSidebarRename("other")).toBe(false);
    cleanups.forEach((cleanup) => cleanup());
  });

  it("stops offering a row once it unregisters", () => {
    const start = vi.fn(() => true);
    const cleanup = registerSidebarRename("conv", start);
    cleanup();
    cleanup();

    expect(startSidebarRename("conv")).toBe(false);
    expect(start).not.toHaveBeenCalled();
  });

  it("treats rows inside a collapsed container as closed", () => {
    const aside = document.createElement("aside");
    const row = document.createElement("li");
    aside.append(row);
    expect(isInClosedSidebar(row)).toBe(false);
    aside.setAttribute("data-collapsed", "true");
    expect(isInClosedSidebar(row)).toBe(true);
  });
});
