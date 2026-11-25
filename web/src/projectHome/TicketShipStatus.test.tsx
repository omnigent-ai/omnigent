import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TicketShipStatusBadge } from "./TicketControls";

const LANDED = "6f3cdf49105bdf8aae5adc2723b3362162665fcb";
const RUNNING = "a2a63b2e4d255cdb33a25724201272a66700762c";

describe("ship status chip", () => {
  it("says a landed change is live when the running code has it", () => {
    render(
      <TicketShipStatusBadge status={{ state: "live", sha: LANDED, running_commit: RUNNING }} />,
    );
    const chip = screen.getByTestId("ticket-ship-status");
    expect(chip).toHaveTextContent("Live in this app");
    expect(chip).toHaveAttribute("data-state", "live");
    expect(chip.getAttribute("title")).toContain(RUNNING);
    expect(chip).not.toHaveAttribute("aria-label");
  });

  it("says a landed change waits for a restart when the running code predates it", () => {
    render(
      <TicketShipStatusBadge
        status={{ state: "waiting_restart", sha: LANDED, running_commit: RUNNING }}
      />,
    );
    const chip = screen.getByTestId("ticket-ship-status");
    expect(chip).toHaveTextContent("Landed, waiting for a restart");
    expect(chip.getAttribute("title")).toContain("after a restart");
  });
});
