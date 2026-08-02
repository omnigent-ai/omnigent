import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { usePreference } from "@/hooks/usePreference";
import {
  clearAppearancePreferenceRegistryForTests,
  createLocalPreference,
} from "@/lib/preferences";

afterEach(() => {
  cleanup();
  localStorage.clear();
  clearAppearancePreferenceRegistryForTests();
});

describe("usePreference", () => {
  it("reads the initial value and updates on set", () => {
    const pref = createLocalPreference({
      key: "test:hook",
      defaultValue: "auto" as const,
      parse: (raw) => (raw === "dark" ? "dark" : "auto"),
      serialize: (value) => value,
      clearWhenDefault: true,
    });

    function Probe() {
      const [value, setValue] = usePreference(pref);
      return (
        <div>
          <span data-testid="value">{value}</span>
          <button type="button" data-testid="set-dark" onClick={() => setValue("dark")}>
            dark
          </button>
        </div>
      );
    }

    render(<Probe />);
    expect(screen.getByTestId("value").textContent).toBe("auto");

    fireEvent.click(screen.getByTestId("set-dark"));
    expect(screen.getByTestId("value").textContent).toBe("dark");
    expect(localStorage.getItem("test:hook")).toBe("dark");
  });

  it("re-renders when the preference is written externally (e.g. reset)", () => {
    const pref = createLocalPreference({
      key: "test:hook-reset",
      defaultValue: 16,
      parse: (raw) => Number(raw),
      serialize: (value) => String(value),
    });
    pref.write(18);

    function Probe() {
      const [value] = usePreference(pref);
      return <span data-testid="value">{value}</span>;
    }

    render(<Probe />);
    expect(screen.getByTestId("value").textContent).toBe("18");

    act(() => {
      pref.reset();
    });
    expect(screen.getByTestId("value").textContent).toBe("16");
    expect(localStorage.getItem("test:hook-reset")).toBeNull();
  });
});
