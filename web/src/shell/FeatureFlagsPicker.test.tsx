import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { SERVER_INFO_OFFLINE_FALLBACK } from "@/lib/bootCapabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { resetUiFeatureOverridesForTests } from "@/lib/uiFeatureOverrides";
import { openFeatureFlagsPicker, useFeatureFlagsPickerStore } from "@/store/featureFlagsPicker";
import { FeatureFlagsPicker } from "./FeatureFlagsPicker";

const key = `omnigent:ui-feature-overrides:v1:${JSON.stringify(window.location.origin)}`;

function renderPicker(quickPick = true) {
  render(
    <CapabilitiesProvider
      info={{
        ...SERVER_INFO_OFFLINE_FALLBACK,
        features: { quick_pick_palette: quickPick, canvas: false, session_overview: true },
      }}
    >
      <TooltipProvider>
        <FeatureFlagsPicker />
      </TooltipProvider>
    </CapabilitiesProvider>,
  );
  act(() => openFeatureFlagsPicker());
  return screen.getByRole("combobox", { name: "UI feature flags" });
}

function openCanvas() {
  const input = renderPicker();
  fireEvent.change(input, { target: { value: "canvas" } });
  fireEvent.keyDown(input, { key: "Enter" });
  return screen.getByRole("combobox", { name: "Choose a value" });
}

function mockReload() {
  const reload = vi.fn();
  const realWindow = window;
  vi.stubGlobal(
    "window",
    new Proxy(realWindow, {
      get(target, property) {
        return property === "location"
          ? { origin: target.location.origin, reload }
          : Reflect.get(target, property, target);
      },
    }),
  );
  return reload;
}

beforeEach(() => {
  localStorage.clear();
  resetUiFeatureOverridesForTests();
});
afterEach(() => {
  cleanup();
  useFeatureFlagsPickerStore.setState({ open: false, replaced: false });
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
  resetUiFeatureOverridesForTests();
});

describe("FeatureFlagsPicker", () => {
  it("lists only reported features with their current choice", () => {
    renderPicker();
    expect(screen.getByRole("option", { name: /Canvas/ })).toHaveTextContent("Automatic (Off)");
    expect(screen.getByRole("option", { name: /Session cost sidebar/ })).toHaveTextContent(
      "Automatic (On)",
    );
    expect(screen.queryByRole("option", { name: /Sidebar grouping/ })).toBeNull();
  });

  it("opens when QuickPick is enabled by an override and lists its own setting", () => {
    localStorage.setItem(key, '{"quick_pick_palette":true}');
    renderPicker(false);
    expect(screen.getByRole("option", { name: /QuickPick command palette/ })).toHaveTextContent(
      "On",
    );
  });

  it.each([
    { label: /^Off/, stored: '{"quick_pick_palette":false}' },
    { label: /^Automatic \(Off\)/, stored: null },
  ])("returns QuickPick to $label and reloads", ({ label, stored }) => {
    localStorage.setItem(key, '{"quick_pick_palette":true}');
    renderPicker(false);
    fireEvent.click(screen.getByRole("option", { name: /QuickPick command palette/ }));
    const reload = mockReload();
    fireEvent.click(screen.getByRole("option", { name: label }));
    expect(localStorage.getItem(key)).toBe(stored);
    expect(reload).toHaveBeenCalledOnce();
  });

  it("writes a choice and reloads the page", () => {
    const input = openCanvas();
    expect(screen.getByRole("option", { name: /Automatic \(Off\)/ })).toHaveTextContent(
      "A Canvas page",
    );
    const reload = mockReload();
    fireEvent.change(input, { target: { value: "on" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(localStorage.getItem(key)).toBe('{"canvas":true}');
    expect(reload).toHaveBeenCalledOnce();
  });

  it.each([
    { label: /^Off/, stored: '{"canvas":false}' },
    { label: /^Automatic \(Off\)/, stored: null },
  ])("saves $label and reloads from an existing override", ({ label, stored }) => {
    localStorage.setItem(key, '{"canvas":true}');
    openCanvas();
    expect(screen.getByRole("option", { name: /^On/ })).toHaveTextContent("Current");
    const reload = mockReload();
    fireEvent.click(screen.getByRole("option", { name: label }));
    expect(localStorage.getItem(key)).toBe(stored);
    expect(reload).toHaveBeenCalledOnce();
  });

  it("shows a failure and does not reload if storage refuses the write", () => {
    const input = openCanvas();
    const reload = mockReload();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("denied");
    });
    fireEvent.change(input, { target: { value: "on" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByText("Couldn't change Canvas.")).toHaveAttribute("role", "status");
    expect(reload).not.toHaveBeenCalled();
  });
});
