import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type * as identity from "@/lib/identity";
import { needsWhom } from "./ticketWaitingOn";
import { TicketLabels } from "./TicketControls";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof identity>()),
  getCurrentUserId: () => "kasey@example.com",
}));

const ask = (responders: string[], reason = "owner", raised_by_user_id: string | null = null) => ({
  reason,
  responders,
  raised_by_user_id,
});

describe("needsWhom", () => {
  it("says you, a named person, a group, or nothing once answered", () => {
    expect(needsWhom(ask(["kasey@example.com"]), "kasey@example.com")).toEqual({ kind: "you" });
    expect(needsWhom(ask(["corey.zumar@example.com"]), "kasey@example.com")).toMatchObject({
      kind: "other",
      label: "Needs Corey",
      title: "Waiting on Corey Zumar",
    });
    expect(
      needsWhom(ask(["aaron@example.com", "corey@example.com"], "product-ok"), "kasey@example.com"),
    ).toMatchObject({ label: "Needs product approval" });
    expect(needsWhom(null, "kasey@example.com")).toBeNull();
    expect(needsWhom(undefined, "kasey@example.com")).toBeUndefined();
  });

  it("isn't waiting on the person who raised the ask", () => {
    const raised = ask(
      ["kasey@example.com", "corey@example.com"],
      "product-ok",
      "kasey@example.com",
    );
    expect(needsWhom(raised, "kasey@example.com")).toMatchObject({ label: "Needs Corey" });
  });
});

describe("the needs-human tag", () => {
  const labels = [{ key: "needs-human", value: null }];

  it("reads Needs you in the strong accent, or the person it waits on in grey", () => {
    const { unmount } = render(
      <TicketLabels labels={labels} compact waitingOn={ask(["kasey@example.com"])} />,
    );
    expect(screen.getByTestId("needs-you")).toHaveTextContent("Needs you");
    expect(screen.getByTestId("needs-you")).toHaveClass("text-white");
    unmount();
    render(<TicketLabels labels={labels} compact waitingOn={ask(["corey@example.com"])} />);
    expect(screen.getByTestId("needs-other")).toHaveTextContent("Needs Corey");
    expect(screen.getByTestId("needs-other")).toHaveClass("text-muted-foreground");
  });

  it("disappears once the ask is answered, and stays generic when routing is unknown", () => {
    const { unmount } = render(<TicketLabels labels={labels} compact waitingOn={null} />);
    expect(screen.queryByText(/Needs/)).toBeNull();
    unmount();
    render(<TicketLabels labels={labels} compact />);
    expect(screen.getByText("Needs response")).toBeInTheDocument();
  });
});
