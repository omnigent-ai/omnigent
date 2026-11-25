import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TicketLabels } from "./TicketControls";
import { blockedCount, blockingLabel } from "./TicketShipBlocker";

const APPROVED = "6f3cdf49105bdf8aae5adc2723b3362162665fcb";
const OLDER = "a2a63b2e4d255cdb33a25724201272a66700762c";

describe("gate labels", () => {
  it("shows approved and verified as short SHAs, side by side, with the full SHA on hover", () => {
    render(
      <TicketLabels
        labels={[
          { key: "verified", value: APPROVED },
          { key: "kind", value: "ux" },
          { key: "approved", value: APPROVED },
        ]}
      />,
    );
    const approved = screen.getByTestId("ticket-gate-approved");
    const verified = screen.getByTestId("ticket-gate-verified");
    expect(approved).toHaveTextContent("approved6f3cdf4");
    expect(approved).toHaveAttribute("title", `approved ${APPROVED}`);
    expect(verified).toHaveTextContent("verified6f3cdf4");
    expect(approved.nextElementSibling).toBe(verified);
    expect(verified).not.toHaveTextContent("≠ approved");
  });

  it("flags a verification that no longer matches the approved commit", () => {
    render(
      <TicketLabels
        labels={[
          { key: "approved", value: APPROVED },
          { key: "verified", value: OLDER },
        ]}
      />,
    );
    const verified = screen.getByTestId("ticket-gate-verified");
    expect(verified).toHaveTextContent("≠ approved");
    expect(verified).toHaveAttribute("title", `verified ${OLDER}, not the approved ${APPROVED}`);
  });

  it("keeps a non-SHA value as a plain label", () => {
    render(<TicketLabels labels={[{ key: "approved", value: "yes" }]} />);
    expect(screen.queryByTestId("ticket-gate-approved")).toBeNull();
    expect(screen.getByText("approved: yes")).toBeInTheDocument();
  });
});

describe("ship blocker", () => {
  const prefix = {
    pushable_sha: null,
    stop: { sha: APPROVED, ticket_id: "t1", reason: "Not verified yet" },
    blocked_verified_ticket_ids: ["t2", "t3"],
  };

  it("counts only for the blocking ticket", () => {
    expect(blockedCount(prefix, "t1")).toBe(2);
    expect(blockedCount(prefix, "t2")).toBe(0);
    expect(blockedCount(null, "t1")).toBe(0);
    expect(blockedCount({ ...prefix, stop: null }, "t1")).toBe(0);
    expect(blockedCount({ ...prefix, stop: { ...prefix.stop, ticket_id: null } }, "t1")).toBe(0);
  });

  it("reads in plain words", () => {
    expect(blockingLabel(1)).toBe("Blocking 1 Verified ticket from shipping");
    expect(blockingLabel(3)).toBe("Blocking 3 Verified tickets from shipping");
  });
});

describe("gate labels on a card", () => {
  it("shows one verified chip in place of approved and verified", () => {
    render(
      <TicketLabels
        compact
        labels={[
          { key: "kind", value: "fix" },
          { key: "approved", value: APPROVED },
          { key: "verified", value: APPROVED },
        ]}
      />,
    );
    expect(screen.queryByTestId("ticket-gate-approved")).toBeNull();
    expect(screen.getByTestId("ticket-gate-verified")).toHaveTextContent("6f3cdf4");
    expect(screen.queryByLabelText(/more label/)).toBeNull();
  });
});
