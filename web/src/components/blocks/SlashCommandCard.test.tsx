// DOM smoke for the slash-command indicator. Pure jsdom — no
// canvas, no clipboard, no animation timing.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { SlashCommandCard } from "./SlashCommandCard";

afterEach(cleanup);

describe("SlashCommandCard", () => {
  it("renders the 'Skill' framing + name with no payload", () => {
    render(
      <SlashCommandCard kind="skill" name="dev-productivity:simplify" arguments="" output={null} />,
    );
    expect(screen.getByText("Skill")).toBeDefined();
    expect(screen.getByText("dev-productivity:simplify")).toBeDefined();
  });

  it("kind='command' switches the prefix to 'Command'", () => {
    // Bucket-C CLI built-ins (``/effort``, ``/clear``, ``/compact``,
    // ``/model``, ``/ultrareview``) render with the Command label —
    // distinct from user-authored Skills.
    render(<SlashCommandCard kind="command" name="effort" arguments="high" output={null} />);
    expect(screen.getByText("Command")).toBeDefined();
    expect(screen.getByText("effort")).toBeDefined();
    expect(screen.getByText("high")).toBeDefined();
    // The data attribute lets the snapshot/styling tests later
    // differentiate cards by kind without inspecting class lists.
    const card = screen.getByTestId("slash-command-card");
    expect(card.getAttribute("data-slash-kind")).toBe("command");
  });

  it("shows args inline in the trigger row when present", () => {
    render(<SlashCommandCard kind="skill" name="oncall" arguments="file-bug" output={null} />);
    expect(screen.getByText("oncall")).toBeDefined();
    expect(screen.getByText("file-bug")).toBeDefined();
  });

  it("collapsed by default; click reveals labelled Arguments and Output panels", () => {
    const { container } = render(
      <SlashCommandCard
        kind="skill"
        name="oncall"
        arguments="file-bug"
        output="oncall: file-bug subcommand started"
      />,
    );
    const trigger = container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]');
    expect(trigger).not.toBeNull();
    expect(trigger!.getAttribute("data-state")).toBe("closed");

    fireEvent.click(trigger!);

    expect(trigger!.getAttribute("data-state")).toBe("open");
    // CodeBlock may split text into multiple syntax-highlight spans;
    // ``getAllByText`` tolerates that as long as at least one match
    // exists.
    expect(screen.getAllByText("Arguments").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Output").length).toBeGreaterThan(0);
    expect(screen.getAllByText("file-bug").length).toBeGreaterThan(0);
    expect(
      screen.getAllByText((_, node) =>
        Boolean(node?.textContent?.includes("oncall: file-bug subcommand started")),
      ).length,
    ).toBeGreaterThan(0);
  });

  it("no payload renders without a Collapsible wrapper", () => {
    const { container } = render(
      <SlashCommandCard kind="skill" name="dev-productivity:simplify" arguments="" output={null} />,
    );
    expect(container.querySelector('[data-slot="collapsible-trigger"]')).toBeNull();
  });
});
