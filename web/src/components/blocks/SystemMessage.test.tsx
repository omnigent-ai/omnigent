import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { SystemMessageView } from "./SystemMessage";

afterEach(cleanup);

describe("SystemMessageView", () => {
  it("hides sub-agent wake notices instead of rendering a centered System row", () => {
    const { container } = render(
      <SystemMessageView
        message={{
          kind: "subagent_wake",
          label: "Sub-agent result ready",
          body: "",
        }}
      />,
    );

    expect(screen.queryByTestId("system-message")).toBeNull();
    expect(container.textContent).toBe("");
  });
});

describe("teammate deliveries", () => {
  it("renders a prose delivery as a teammate card with the summary and body visible", () => {
    render(
      <SystemMessageView
        message={{
          kind: "teammate_message",
          label: "Teammate buddy",
          body: "All good here - TMCHAT.",
          teammate: { id: "buddy", summary: "All good over here" },
        }}
      />,
    );

    const card = screen.getByTestId("teammate-message");
    expect(card.getAttribute("data-teammate-id")).toBe("buddy");
    expect(card.textContent).toContain("@buddy");
    expect(card.textContent).toContain("All good over here");
    expect(card.textContent).toContain("All good here - TMCHAT.");
    expect(screen.queryByTestId("system-message")).toBeNull();
  });

  it("renders a finished result without the System prefix", () => {
    render(
      <SystemMessageView
        message={{
          kind: "teammate_finished",
          label: "Teammate buddy finished",
          body: "TMREPLY done.",
          teammate: { id: "buddy", summary: null },
        }}
      />,
    );

    const card = screen.getByTestId("teammate-message");
    expect(card.textContent).toContain("@buddy finished");
    expect(card.textContent).toContain("TMREPLY done.");
    expect(card.textContent).not.toContain("System:");
  });
});
