// A shared dialog's controls take their names from the overlay owner the Settings site renders
// it under, narrowed to the members that can show each section; elsewhere, with no owner, every
// section still renders, with the dialog's own names.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { settingsEntryFamily, type SettingsEntry } from "./catalog/types";
import { SettingsOverlayOwner, useSettingsOverlayParts } from "./settingsOwnership";

const build =
  (opener: "setUp" | "credentialAction") =>
  (member: "alpha" | "beta"): SettingsEntry => ({
    id: `fixture.fix-${opener}-${member}`,
    section: "general",
    label: `Fix ${member}`,
    description: "A fix dialog.",
    parts: {
      [opener]: "Fix it",
      retry: "Try again",
      copyCommand: "Copy omni setup",
      ...(member === "beta" ? { copyCode: "Copy one-time code" } : {}),
    },
    nameAlternatives: { copyCommand: { tmux: "Copy brew install tmux" } },
    presence: {
      retry: { overlay: opener },
      copyCommand: { overlay: opener },
      ...(member === "beta" ? { copyCode: { overlay: opener } } : {}),
    },
    overlays: { [opener]: {} },
  });
const CARD = settingsEntryFamily({
  key: "card",
  members: ["alpha", "beta"],
  build: build("setUp"),
});
const CREDENTIAL = settingsEntryFamily({
  key: "credential",
  members: ["alpha", "beta"],
  build: build("credentialAction"),
});
const DEVICE = ["beta"] as const;

/** A dialog body: a retry everywhere, a copy with a finite alternative, and a one-time code. */
function Dialog() {
  const parts = useSettingsOverlayParts();
  const device = parts.within(DEVICE);
  const confirm = parts.within(["alpha", "beta"], "credentialAction");
  return (
    <div role="dialog" {...parts.overlay()}>
      <button type="button" {...parts.control("retry", { outside: {} })}>
        Try again
      </button>
      <button
        type="button"
        {...parts.control("copyCommand", {
          name: { alternative: "tmux" },
          outside: { "aria-label": "Copy brew install tmux" },
        })}
      />
      {device && (
        <device.Provider>
          <Code />
        </device.Provider>
      )}
      {confirm && <span>Confirm offered</span>}
    </div>
  );
}

function Code() {
  const parts = useSettingsOverlayParts();
  return (
    <button
      type="button"
      {...parts.control("copyCode", { outside: { "aria-label": "Copy one-time code" } })}
    />
  );
}

afterEach(cleanup);

describe("a shared dialog's overlay parts", () => {
  it("names each control as its owner's overlay part, with the content linked to the opener", () => {
    render(
      <SettingsOverlayOwner entry={CARD.entry("beta")} opener="setUp">
        <Dialog />
      </SettingsOverlayOwner>,
    );
    expect(screen.getByRole("dialog")).toHaveAttribute(
      "data-settings-overlay-owner",
      "fixture.fix-setUp-beta:setUp",
    );
    expect(screen.getByRole("button", { name: "Try again" })).toHaveAttribute(
      "data-settings-control",
      "fixture.fix-setUp-beta:retry",
    );
    // A declared alternative, through the same formatter as every part.
    expect(screen.getByRole("button", { name: "Copy brew install tmux" })).toHaveAttribute(
      "data-settings-control",
      "fixture.fix-setUp-beta:copyCommand",
    );
    // A member the narrowing keeps: the Provider lends the owner to the shared child.
    expect(screen.getByRole("button", { name: "Copy one-time code" })).toHaveAttribute(
      "data-settings-control",
      "fixture.fix-setUp-beta:copyCode",
    );
    // A narrowing to another opener leaves the card's owner out.
    expect(screen.queryByText("Confirm offered")).toBeNull();
  });

  it("leaves a section out for a Settings owner its narrowing excludes", () => {
    render(
      <SettingsOverlayOwner entry={CREDENTIAL.entry("alpha")} opener="credentialAction">
        <Dialog />
      </SettingsOverlayOwner>,
    );
    expect(screen.queryByRole("button", { name: "Copy one-time code" })).toBeNull();
    expect(screen.getByText("Confirm offered")).toBeInTheDocument();
  });

  it("renders every section unowned, with the dialog's own names, outside Settings", () => {
    render(<Dialog />);
    const dialog = screen.getByRole("dialog");
    expect(dialog).not.toHaveAttribute("data-settings-overlay-owner");
    for (const name of ["Try again", "Copy brew install tmux", "Copy one-time code"]) {
      const control = screen.getByRole("button", { name });
      expect(control).not.toHaveAttribute("data-settings-control");
      expect(control).not.toHaveAttribute("title");
    }
    // Narrowed sections still render with no owner at all.
    expect(screen.getByText("Confirm offered")).toBeInTheDocument();
  });

  // A field a narrowed Provider wraps, and an unrelated tick.
  function Field() {
    const [value, setValue] = useState("");
    return (
      <input aria-label="Field" value={value} onChange={(event) => setValue(event.target.value)} />
    );
  }
  function Narrowed({ tick }: { tick: number }) {
    const parts = useSettingsOverlayParts();
    const device = parts.within(DEVICE);
    const again = device?.within(DEVICE);
    return (
      again && (
        <again.Provider>
          <span>{tick}</span>
          <Field />
        </again.Provider>
      )
    );
  }

  it.each([true, false])(
    "keeps what a narrowed Provider wraps across its parent's renders, owned: %s",
    (owned) => {
      const tree = (tick: number) =>
        owned ? (
          <SettingsOverlayOwner entry={CARD.entry("beta")} opener="setUp">
            <Narrowed tick={tick} />
          </SettingsOverlayOwner>
        ) : (
          <Narrowed tick={tick} />
        );
      const view = render(tree(0));
      const field = screen.getByRole("textbox", { name: "Field" });
      fireEvent.change(field, { target: { value: "typed" } });
      field.focus();
      view.rerender(tree(1));
      expect(screen.getByRole("textbox", { name: "Field" })).toBe(field);
      expect(field).toHaveValue("typed");
      expect(field).toHaveFocus();
    },
  );

  it("names a templated part around the runtime value it's given", () => {
    const entry: SettingsEntry = {
      id: "fixture.copy-command",
      section: "general",
      label: "Copy",
      description: "A command to copy.",
      parts: { open: "Open", copy: "Copy" },
      nameTemplates: { copy: { before: "Copy " } },
      presence: { copy: { overlay: "open" } },
      overlays: { open: {} },
    };
    function Copy() {
      const parts = useSettingsOverlayParts();
      return (
        <button
          type="button"
          {...parts.control("copy", { name: { runtime: "command" }, outside: {} })}
        />
      );
    }
    render(
      <SettingsOverlayOwner entry={entry} opener="open">
        <Copy />
      </SettingsOverlayOwner>,
    );
    expect(screen.getByRole("button", { name: "Copy command" })).toHaveAttribute(
      "data-settings-control",
      "fixture.copy-command:copy",
    );
  });
});
