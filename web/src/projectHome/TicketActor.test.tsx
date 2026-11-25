import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
  ActingFor,
  PersonName,
  RequesterName,
  personFirstName,
  personLabel,
  personTitle,
} from "./TicketActor";

afterEach(cleanup);

describe("who did it", () => {
  it("names people by first name, with the full name and sign-in ID on hover", () => {
    expect(personFirstName("alex.rivera@example.com")).toBe("Alex");
    expect(personLabel("alex.rivera@example.com", "alex.rivera@example.com")).toBe("you");
    expect(personTitle("alex.rivera@example.com")).toBe("Alex Rivera · alex.rivera@example.com");
    render(<PersonName userId="alex.rivera@example.com" viewer="kasey@example.com" avatar />);
    const name = screen.getByTestId("ticket-person-name");
    expect(name).toHaveTextContent("Alex");
    expect(name).not.toHaveTextContent("@");
    expect(name).toHaveAttribute("title", "Alex Rivera · alex.rivera@example.com");
  });

  it("shows whom an agent acts for", () => {
    render(<ActingFor userId="alex.rivera@example.com" viewer="kasey@example.com" />);
    expect(screen.getByTestId("acting-for")).toHaveTextContent(", for Alex");
  });

  it("keeps outside requesters' labels", () => {
    render(<RequesterName requestedBy="github:octo" viewer={null} />);
    expect(screen.getByText("@octo (GitHub PR)")).toBeInTheDocument();
  });
});
