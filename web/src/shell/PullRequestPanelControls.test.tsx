import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PanelStateMessage } from "./PullRequestPanelControls";

describe("PanelStateMessage", () => {
  it("says what's loading in one line, as a status and never an alert", () => {
    render(<PanelStateMessage variant="loading">Loading changes…</PanelStateMessage>);
    expect(screen.getByRole("status")).toHaveTextContent("Loading changes…");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("heading")).toBeNull();
  });

  it("titles an empty state with its line and action, as a status", () => {
    render(
      <PanelStateMessage
        variant="empty"
        title="No Changes"
        actions={<button type="button">Show all</button>}
      >
        There are no changes in this pull request.
      </PanelStateMessage>,
    );
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("No Changes");
    expect(status).toHaveTextContent("There are no changes in this pull request.");
    expect(screen.getByRole("button", { name: "Show all" })).toBeVisible();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("reads a failure out as an alert, with the reason and how to recover", () => {
    render(
      <PanelStateMessage
        variant="failure"
        title="Changes Unavailable"
        actions={<button type="button">Try Again</button>}
      >
        GitHub is unavailable.
      </PanelStateMessage>,
    );
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Changes Unavailable");
    expect(alert).toHaveTextContent("GitHub is unavailable.");
    // The action sits outside the announced text, so the alert reads only the message.
    expect(alert).not.toHaveTextContent("Try Again");
    expect(screen.getByRole("button", { name: "Try Again" })).toBeVisible();
  });
});
