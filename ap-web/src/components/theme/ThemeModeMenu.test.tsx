// Tests for ThemeModeMenu — the compact sidebar button that cycles the theme
// system → dark → light on each click.
//
// The icon shows the *current* mode, while the aria-label/title announce the
// *next* mode the click will apply (see nextThemeMode). It hides entirely when
// embedded (the host owns the theme). `next-themes` and `@/lib/embedded` are
// mocked so each test pins the current theme, system theme, and embed state;
// the real themeMode helpers (pure) run unmocked.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";

const setTheme = vi.fn();
let currentTheme: string | undefined;
let systemTheme: string | undefined;
let embedded: boolean;

vi.mock("next-themes", () => ({
  useTheme: () => ({ theme: currentTheme, systemTheme, setTheme }),
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
  systemTheme = undefined;
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

  it("skips dark when the system theme is dark", () => {
    // WHY: at "system" on a dark OS, pinning dark would render identically, so
    // the cycle jumps straight to light.
    currentTheme = "system";
    systemTheme = "dark";
    renderMenu();
    fireEvent.click(screen.getByRole("button", { name: "Switch to Light" }));
    expect(setTheme).toHaveBeenCalledWith("light");
  });

  it("does not offer light first when the system theme is light", () => {
    // WHY: from "system" the cycle's first stop is dark regardless of OS, so a
    // light OS still advances to dark before anything else.
    currentTheme = "system";
    systemTheme = "light";
    renderMenu();
    fireEvent.click(screen.getByRole("button", { name: "Switch to Dark" }));
    expect(setTheme).toHaveBeenCalledWith("dark");
  });

  it("skips light when an explicit dark theme sits on a light system", () => {
    // WHY: dark's next stop is light, but a light OS already renders light, so
    // skip the redundant hop and go straight to system. This is the asymmetry
    // the system-theme check fixes — `resolvedTheme` would have offered light.
    currentTheme = "dark";
    systemTheme = "light";
    renderMenu();
    fireEvent.click(screen.getByRole("button", { name: "Switch to System" }));
    expect(setTheme).toHaveBeenCalledWith("system");
  });
});
