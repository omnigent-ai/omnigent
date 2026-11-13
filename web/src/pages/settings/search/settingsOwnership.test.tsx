// A shared control takes its name and id from the Settings row that renders it, a widget's
// portaled content is placed under that widget, and outside any owner the control keeps its own.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { settingsEntry } from "./catalog/types";
import {
  SettingsControl,
  SettingsOwner,
  useSettingsOwnership,
  useSettingsWidgetPortal,
} from "./settingsOwnership";

const ROW = settingsEntry({
  id: "fixture-row",
  section: "general",
  label: "Fixture row",
  description: "A row for the ownership tests.",
  parts: { copy: "Copy" },
  nameTemplates: { copy: { before: "Copy " } },
});

function SharedControl({ runtime }: { runtime?: string }) {
  const ownership = useSettingsOwnership();
  return (
    <button
      type="button"
      {...ownership.control({
        name: runtime === undefined ? undefined : { runtime },
        outside: { "aria-label": "Copy the value" },
      })}
    >
      Copy
    </button>
  );
}

function SharedLabels() {
  const ownership = useSettingsOwnership();
  const labels = ownership.labelIds({ labelId: "outside-label", descriptionId: "outside-text" });
  return <p id={labels.labelId} aria-describedby={labels.descriptionId} data-testid="labels" />;
}

afterEach(cleanup);

describe("a shared control's ownership", () => {
  it("is named and found as its owner's part, with a templated part's runtime value", () => {
    render(
      <SettingsOwner entry={ROW} part="copy">
        <SharedControl runtime="command" />
      </SettingsOwner>,
    );
    const control = screen.getByRole("button", { name: "Copy command" });
    expect(control).toHaveAttribute("data-settings-control", "fixture-row:copy");
    expect(control).toHaveAttribute("title", "Copy command");
  });

  it("is the owner's main control without a part", () => {
    render(
      <SettingsOwner entry={ROW}>
        <SharedControl />
      </SettingsOwner>,
    );
    const control = screen.getByRole("button");
    expect(control).toHaveAttribute("data-settings-control", "fixture-row:");
    expect(control).toHaveAttribute("aria-labelledby", "settings-fixture-row-label");
    expect(control).toHaveAttribute("aria-describedby", "settings-fixture-row-description");
    expect(control).not.toHaveAttribute("aria-label");
    expect(control).not.toHaveAttribute("title");
  });

  it("names its label and description after the owner's row, inside an owner", () => {
    render(
      <SettingsOwner entry={ROW}>
        <SharedLabels />
      </SettingsOwner>,
    );
    expect(screen.getByTestId("labels")).toHaveAttribute("id", "settings-fixture-row-label");
    expect(screen.getByTestId("labels")).toHaveAttribute(
      "aria-describedby",
      "settings-fixture-row-description",
    );
  });

  it("keeps its own names outside any owner", () => {
    render(<SharedControl />);
    const control = screen.getByRole("button", { name: "Copy the value" });
    expect(control).not.toHaveAttribute("data-settings-control");
    expect(control).not.toHaveAttribute("title");
  });

  it("keeps its own label ids outside any owner", () => {
    render(<SharedLabels />);
    expect(screen.getByTestId("labels")).toHaveAttribute("id", "outside-label");
    expect(screen.getByTestId("labels")).toHaveAttribute("aria-describedby", "outside-text");
  });
});

describe("a widget's portaled content", () => {
  function Portaled() {
    return <span data-testid="portal" {...useSettingsWidgetPortal()} />;
  }

  it("is placed under the widget that opens it", () => {
    render(
      <SettingsControl entry={ROW} part="copy">
        <Portaled />
      </SettingsControl>,
    );
    expect(screen.getByTestId("portal")).toHaveAttribute(
      "data-settings-overlay-owner",
      "fixture-row:copy",
    );
    expect(screen.getByTestId("portal")).toHaveAttribute(
      "data-settings-widget",
      "fixture-row:copy",
    );
  });

  it("has no widget outside a control", () => {
    render(<Portaled />);
    expect(screen.getByTestId("portal")).not.toHaveAttribute("data-settings-widget");
  });
});
