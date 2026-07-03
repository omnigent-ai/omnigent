// DOM smoke tests for the terminal-command card rendered for !cmd inputs.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { TerminalCommandCard } from "./TerminalCommandCard";

afterEach(cleanup);

describe("TerminalCommandCard", () => {
  it("kind='input' renders the command text with a $ prefix", () => {
    render(<TerminalCommandCard kind="input" input="pwd" stdout={null} stderr={null} />);
    const card = screen.getByTestId("terminal-command-card");
    expect(card.getAttribute("data-terminal-kind")).toBe("input");
    expect(screen.getByText("pwd")).toBeDefined();
    expect(screen.getByText("$")).toBeDefined();
  });

  it("kind='input' with no text renders without crashing", () => {
    render(<TerminalCommandCard kind="input" input={null} stdout={null} stderr={null} />);
    const card = screen.getByTestId("terminal-command-card");
    expect(card.getAttribute("data-terminal-kind")).toBe("input");
  });

  it("kind='output' with no stdout/stderr renders '(no output)' and no collapsible", () => {
    const { container } = render(
      <TerminalCommandCard kind="output" input={null} stdout={null} stderr={null} />,
    );
    expect(screen.getByTestId("terminal-command-card").getAttribute("data-terminal-kind")).toBe(
      "output",
    );
    expect(screen.getByText("(no output)")).toBeDefined();
    expect(container.querySelector('[data-slot="collapsible-trigger"]')).toBeNull();
  });

  it("kind='output' with stdout renders collapsible; click reveals stdout panel", () => {
    const { container } = render(
      <TerminalCommandCard kind="output" input={null} stdout="/home/user" stderr={null} />,
    );
    const trigger = container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]');
    expect(trigger).not.toBeNull();
    expect(trigger!.getAttribute("data-state")).toBe("closed");

    fireEvent.click(trigger!);

    expect(trigger!.getAttribute("data-state")).toBe("open");
    expect(screen.getAllByText("stdout").length).toBeGreaterThan(0);
    expect(
      screen.getAllByText((_, node) => Boolean(node?.textContent?.includes("/home/user"))).length,
    ).toBeGreaterThan(0);
  });

  it("kind='output' with stderr renders stderr panel", () => {
    const { container } = render(
      <TerminalCommandCard kind="output" input={null} stdout={null} stderr="command not found" />,
    );
    const trigger = container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]');
    expect(trigger).not.toBeNull();
    fireEvent.click(trigger!);
    expect(screen.getAllByText("stderr").length).toBeGreaterThan(0);
    expect(
      screen.getAllByText((_, node) => Boolean(node?.textContent?.includes("command not found")))
        .length,
    ).toBeGreaterThan(0);
  });
});
