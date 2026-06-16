// Tests for ThemeModeMenu — the compact sidebar button that cycles the theme
// system → dark → light on each click.
//
// The button previews the *next* mode: its aria-label/title and icon describe
// the mode the next click applies (see nextThemeMode). It hides entirely when
// embedded (the host owns the theme). `next-themes` and `@/lib/embedded` are
// mocked so each test pins the current theme and embed state; the real
// themeMode helpers (pure) run unmocked.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";

const setTheme = vi.fn();
let currentTheme: string | undefined;
let embedded: boolean;

vi.mock("next-themes", () => ({
  useTheme: () => ({ theme: currentTheme, setTheme }),
}));

vi.mock("@/lib/embedded", () => ({
  useIsEmbedded: () => embedded,
}));

import { ThemeModeMenu } from "./ThemeModeMenu";

function renderMenu() {
  return render(
    <TooltipProvider>
      <ThemeModeMenu />
    </TooltipProvider>,
  );
}

beforeEach(() => {
  currentTheme = "system";
  embedded = false;
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ThemeModeMenu", () => {
  it("renders nothing when embedded", () => {
    // WHY: the host owns the theme in embed mode, so the switcher must be a
    // no-op and render no button at all.
    embedded = true;
    const { container } = renderMenu();
    expect(container).toBeEmptyDOMElement();
  });

  it("labels the button with the next mode in the cycle (system → dark)", () => {
    // WHY: at "system" the next click applies "dark", so the action label must
    // announce "Switch to Dark".
    currentTheme = "system";
    renderMenu();
    expect(screen.getByRole("button", { name: "Switch to Dark" })).toBeInTheDocument();
  });

  it("clicking from system selects dark", () => {
    // WHY: a click must advance one step in the cycle, calling setTheme with
    // the previewed next mode rather than the current one.
    currentTheme = "system";
    renderMenu();
    fireEvent.click(screen.getByRole("button", { name: "Switch to Dark" }));
    expect(setTheme).toHaveBeenCalledWith("dark");
  });

  it("clicking from dark selects light", () => {
    // WHY: dark's next mode is light — pins the middle hop of the cycle.
    currentTheme = "dark";
    renderMenu();
    fireEvent.click(screen.getByRole("button", { name: "Switch to Light" }));
    expect(setTheme).toHaveBeenCalledWith("light");
  });

  it("clicking from light wraps back to system", () => {
    // WHY: light's next mode is system — pins the wrap-around so the cycle
    // visits every mode rather than trapping in two states.
    currentTheme = "light";
    renderMenu();
    fireEvent.click(screen.getByRole("button", { name: "Switch to System" }));
    expect(setTheme).toHaveBeenCalledWith("system");
  });

  it("treats an unknown stored theme as system", () => {
    // WHY: a garbage/legacy stored value must normalize to "system", whose
    // next mode is dark — so the button still offers "Switch to Dark".
    currentTheme = "sepia";
    renderMenu();
    expect(screen.getByRole("button", { name: "Switch to Dark" })).toBeInTheDocument();
  });
});
