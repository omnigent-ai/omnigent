import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { usePaletteCommandsStore } from "@/store/paletteCommands";
import { registeredPaletteCommands } from "@/test/paletteCommands";

import { useClearSidebarFiltersCommand } from "./useClearSidebarFiltersCommand";

afterEach(() => {
  usePaletteCommandsStore.setState({ groups: {} });
});

describe("useClearSidebarFiltersCommand", () => {
  it("offers Clear sidebar filters only while a filter is on, and clears with the latest call", () => {
    const first = vi.fn();
    const latest = vi.fn();
    const { rerender } = renderHook(
      ({ filtersOn, clear }) => useClearSidebarFiltersCommand({ filtersOn, clear }),
      { initialProps: { filtersOn: false, clear: first } },
    );
    expect(registeredPaletteCommands()).toEqual([]);

    rerender({ filtersOn: true, clear: first });
    rerender({ filtersOn: true, clear: latest });
    const rows = registeredPaletteCommands();
    expect(rows.map((row) => row.label)).toEqual(["Clear sidebar filters"]);
    rows[0]!.run();
    expect(latest).toHaveBeenCalledOnce();
    expect(first).not.toHaveBeenCalled();

    rerender({ filtersOn: false, clear: latest });
    expect(registeredPaletteCommands()).toEqual([]);
  });
});
