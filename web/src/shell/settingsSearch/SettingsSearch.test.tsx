import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TERMINAL_THEME_ENTRY } from "@/pages/settings/search/catalog/appearance";
import { settingsItemPath } from "@/pages/settings/search/settingsControl";

import { SettingsSearch } from "./SettingsSearch";
import {
  clearSettingsSearchFocus,
  hasPendingSettingsSearchFocus,
  requestSettingsSearchFocus,
  SETTINGS_SEARCH_FOCUS_TIMEOUT_MS,
} from "./settingsSearchFocus";

afterEach(() => {
  cleanup();
  clearSettingsSearchFocus();
});

function Location() {
  const location = useLocation();
  return <output data-testid="location">{`${location.pathname}${location.hash}`}</output>;
}

function Harness({ onNavigate }: { onNavigate?: () => void }) {
  const [query, setQuery] = useState("");
  return <SettingsSearch query={query} onQueryChange={setQuery} onNavigate={onNavigate} />;
}

function renderSearch(props: { onNavigate?: () => void } = {}) {
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <MemoryRouter initialEntries={["/settings/general"]}>
        <Location />
        <Harness {...props} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return screen.getByRole("combobox", { name: "Search settings" });
}

describe("SettingsSearch", () => {
  it("shows only its field until there is a query", () => {
    const field = renderSearch();
    expect(field).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryAllByRole("option")).toHaveLength(0);
  });

  it("finds a setting by its name and opens its row when picked", () => {
    const onNavigate = vi.fn();
    const field = renderSearch({ onNavigate });
    fireEvent.change(field, { target: { value: "terminal theme" } });
    expect(screen.getByRole("option", { name: /Terminal theme/ })).toBeInTheDocument();
    fireEvent.keyDown(field, { key: "Enter" });
    expect(screen.getByTestId("location")).toHaveTextContent(
      settingsItemPath(TERMINAL_THEME_ENTRY),
    );
    expect(onNavigate).toHaveBeenCalledTimes(1);
  });

  it("clears the query on Escape, then leaves the field", () => {
    const field = renderSearch();
    field.focus();
    fireEvent.change(field, { target: { value: "terminal" } });
    fireEvent.keyDown(field, { key: "Escape" });
    expect(field).toHaveValue("");
    expect(field).toHaveFocus();
    fireEvent.keyDown(field, { key: "Escape" });
    expect(field).not.toHaveFocus();
  });

  it("takes focus when a request to focus it is pending", () => {
    requestSettingsSearchFocus();
    const field = renderSearch();
    expect(field).toHaveFocus();
    expect(hasPendingSettingsSearchFocus()).toBe(false);
  });

  it("lets a pending request lapse after a second", () => {
    requestSettingsSearchFocus();
    const later = performance.now() + SETTINGS_SEARCH_FOCUS_TIMEOUT_MS + 1;
    expect(hasPendingSettingsSearchFocus(later)).toBe(false);
  });
});
