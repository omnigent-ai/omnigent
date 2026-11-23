import { act, cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { QuickPickItem } from "./model";
import { QuickPick } from "./QuickPick";
import { type QuickPickPages, useQuickPickPages } from "./useQuickPickPages";

afterEach(cleanup);

describe("useQuickPickPages", () => {
  it("opens a child page with an empty query and returns to the parent as it was", () => {
    const { result } = renderHook(() => useQuickPickPages<"themes" | "appearance">("themes"));
    act(() => result.current.pickerProps.onQueryChange("dark"));
    act(() => result.current.pickerProps.onActiveIdChange("theme:dark"));
    expect(result.current.pickerProps.onBack).toBeUndefined();

    act(() => result.current.push("appearance", "appearance:system"));
    expect(result.current.page).toEqual({
      id: "appearance",
      query: "",
      activeId: "appearance:system",
    });
    expect(result.current.depth).toBe(2);

    act(() => result.current.pickerProps.onBack?.());
    expect(result.current.page).toEqual({ id: "themes", query: "dark", activeId: "theme:dark" });
  });

  it("starts a new query from the best match", () => {
    const { result } = renderHook(() => useQuickPickPages("root", "first"));
    act(() => result.current.pickerProps.onQueryChange("x"));
    expect(result.current.page.activeId).toBeNull();
  });

  it("stays on the root page when asked to go back from it, and resets to a fresh root", () => {
    const { result } = renderHook(() => useQuickPickPages<"root" | "child">("root", "start"));
    act(() => result.current.pop());
    expect(result.current.depth).toBe(1);
    act(() => result.current.push("child"));
    act(() => result.current.reset());
    expect(result.current.page).toEqual({ id: "root", query: "", activeId: "start" });
  });
});

describe("useQuickPickPages with a rendered picker", () => {
  const choices: QuickPickItem<string>[] = ["alpha", "beta"].map((id) => ({
    id,
    label: id,
    value: id,
  }));
  let pages: QuickPickPages<"root" | "child">;
  function Paged() {
    pages = useQuickPickPages<"root" | "child">("root");
    return (
      <QuickPick
        label={pages.page.id}
        segments={[{ sourceId: pages.page.id, ranking: "client", items: choices }]}
        emptyMessage="No results found"
        onAccept={() => undefined}
        {...pages.pickerProps}
      />
    );
  }
  const active = () =>
    document.getElementById(
      screen.getByRole("combobox").getAttribute("aria-activedescendant") ?? "",
    )?.textContent;

  it("doesn't carry a child page's pick back to the parent, or into a fresh root", () => {
    render(<Paged />);
    expect(active()).toBe("alpha");
    act(() => pages.push("child"));
    fireEvent.keyDown(screen.getByRole("combobox"), { key: "ArrowDown" });
    expect(active()).toBe("beta");
    act(() => pages.pop());
    expect(active()).toBe("alpha");

    act(() => pages.push("child"));
    fireEvent.keyDown(screen.getByRole("combobox"), { key: "ArrowDown" });
    act(() => pages.reset());
    expect(active()).toBe("alpha");
  });
});
