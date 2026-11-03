import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type * as AvailableAgentsModule from "@/hooks/useAvailableAgents";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import {
  HIDDEN_PICKER_AGENTS_STORAGE_KEY,
  readHiddenPickerAgents,
  writeHiddenPickerAgents,
} from "@/lib/pickerEntryVisibility";
import { testAgent } from "@/test/agentFixtures";
import { PickerVisibilitySetting, pickerVisibilitySummary } from "./PickerVisibilitySetting";

const catalog = vi.hoisted(() => ({
  data: undefined as AvailableAgent[] | undefined,
  isLoading: false,
  isError: false,
  retry: vi.fn(),
}));
vi.mock("@/hooks/useAvailableAgents", async (importOriginal) => ({
  ...(await importOriginal<typeof AvailableAgentsModule>()),
  useAgentCatalog: () => catalog,
}));

const claude = testAgent("agent-claude", "claude-native-ui", {
  display_name: "Claude Code",
  harness: "claude-native",
  builtin: true,
});
const codex = testAgent("agent-codex", "codex-native-ui", {
  display_name: "Codex",
  harness: "codex-native",
  builtin: true,
});
const polly = testAgent("agent-polly", "polly", {
  display_name: "Polly",
  harness: "claude-sdk",
  builtin: true,
});
const debby = testAgent("agent-debby", "debby", {
  display_name: "Debby",
  harness: "claude-sdk",
  builtin: true,
});

afterEach(() => {
  cleanup();
  window.localStorage.removeItem(HIDDEN_PICKER_AGENTS_STORAGE_KEY);
  catalog.data = undefined;
  catalog.isLoading = false;
  catalog.isError = false;
  catalog.retry.mockReset();
});

describe("pickerVisibilitySummary", () => {
  const entries = [claude, codex, polly, debby];
  it.each([
    ["nothing hidden", [], "Showing all 4."],
    ["two hidden", ["debby", "codex-native-ui"], "Showing 2 of 4. Hidden: Codex and Debby."],
    ["a stale name only", ["removed-agent"], "Showing all 4."],
    [
      "more than three hidden",
      ["claude-native-ui", "codex-native-ui", "polly", "debby"],
      "Showing 0 of 4. Hidden: Claude Code, Codex, Polly, and 1 more.",
    ],
  ])("%s", (_label, hidden, expected) => {
    expect(pickerVisibilitySummary(entries, new Set(hidden))).toBe(expected);
  });
});

describe("PickerVisibilitySetting", () => {
  const option = (name: string) => screen.getByRole("option", { name });
  const summary = () => screen.getByTestId("picker-visibility-summary");

  it("toggles an entry from the filterable checklist", () => {
    catalog.data = [claude, codex, polly];
    render(<PickerVisibilitySetting />);
    expect(summary()).toHaveTextContent("Showing all 3.");
    expect(screen.getByTestId("picker-visibility-reset")).toBeDisabled();

    fireEvent.click(screen.getByTestId("picker-visibility-edit"));
    expect(screen.getByText("Harnesses")).toBeInTheDocument();
    expect(screen.getByText("Agents")).toBeInTheDocument();
    expect(option("Codex")).toHaveAttribute("aria-checked", "true");

    fireEvent.click(option("Codex"));
    expect(readHiddenPickerAgents()).toEqual(new Set(["codex-native-ui"]));
    expect(option("Codex")).toHaveAttribute("aria-checked", "false");
    expect(option("Claude Code")).toHaveAttribute("aria-checked", "true");
    expect(summary()).toHaveTextContent("Showing 2 of 3. Hidden: Codex.");

    fireEvent.change(screen.getByPlaceholderText("Filter harnesses and agents…"), {
      target: { value: "poll" },
    });
    expect(option("Polly")).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Codex" })).toBeNull();
  });

  it("restores every entry with Show all", () => {
    catalog.data = [claude, debby];
    writeHiddenPickerAgents(new Set(["debby"]));
    render(<PickerVisibilitySetting />);
    expect(summary()).toHaveTextContent("Showing 1 of 2. Hidden: Debby.");

    fireEvent.click(screen.getByTestId("picker-visibility-reset"));
    expect(readHiddenPickerAgents().size).toBe(0);
    expect(summary()).toHaveTextContent("Showing all 2.");
  });

  it("follows a change made outside the control", () => {
    catalog.data = [claude, codex];
    render(<PickerVisibilitySetting />);
    act(() => writeHiddenPickerAgents(new Set(["claude-native-ui"])));
    expect(summary()).toHaveTextContent("Showing 1 of 2. Hidden: Claude Code.");
  });

  it.each<[string, { isLoading?: boolean; isError?: boolean }, string]>([
    ["while the catalog loads", { isLoading: true }, "Loading agents…"],
    ["for an empty catalog", {}, "No agents available on this server yet."],
    ["when the catalog failed", { isError: true }, "Couldn't load this server's harnesses"],
  ])("explains the empty list %s", (_label, state, expected) => {
    Object.assign(catalog, state);
    render(<PickerVisibilitySetting />);
    expect(summary()).toHaveTextContent(expected);
    expect(screen.getByTestId("picker-visibility-edit")).toBeDisabled();
    expect(screen.queryByTestId("picker-visibility-retry") !== null).toBe(state.isError ?? false);
  });

  it("retries a failed catalog load", () => {
    catalog.isError = true;
    render(<PickerVisibilitySetting />);
    fireEvent.click(screen.getByTestId("picker-visibility-retry"));
    expect(catalog.retry).toHaveBeenCalledTimes(1);
  });
});
