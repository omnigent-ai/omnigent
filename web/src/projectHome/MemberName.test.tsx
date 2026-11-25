import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { ProjectStatusMember } from "@/lib/projectStatusApi";
import { MemberName, OTHER_AGENTS_EXPLANATION, memberDisplayName } from "./MemberName";
import { teamCount } from "./ProjectSummary";

function member(kind: ProjectStatusMember["kind"], name: string, user_id: string | null = null) {
  return {
    key: `${kind}:${name}`,
    kind,
    name,
    user_id,
    identity_id: kind === "identity" ? name : null,
    tickets_done: 0,
    prs_merged: null,
    spend_cents: null,
    cost_per_merged_pr_cents: null,
  } satisfies ProjectStatusMember;
}

describe("teamCount", () => {
  it("counts people and roles apart and leaves out the agents pool", () => {
    expect(
      teamCount([
        member("human", "alice.smith@example.com", "alice.smith@example.com"),
        member("human", "bob@example.com", "bob@example.com"),
        member("identity", "Lead"),
        member("identity", "PM"),
        member("agents", "Other agents"),
      ]),
    ).toBe("2 people · 2 roles");
    expect(teamCount([member("human", "a@example.com", "a@example.com")])).toBe(
      "1 person · 0 roles",
    );
  });
});

describe("MemberName", () => {
  it("shows a person by name with their email on hover", () => {
    const person = member("human", "alice.smith@example.com", "alice.smith@example.com");
    render(<MemberName member={person} />);
    expect(screen.getByText("Alice Smith")).toBeTruthy();
    expect(screen.getByTitle("alice.smith@example.com")).toBeTruthy();
    expect(memberDisplayName(person)).toBe("Alice Smith");
    expect(memberDisplayName(member("identity", "PM"))).toBe("PM");
  });

  it("explains the other-agents row", async () => {
    render(
      <TooltipProvider>
        <MemberName member={member("agents", "Other agents")} />
      </TooltipProvider>,
    );
    await userEvent.hover(screen.getByRole("button", { name: 'What "Other agents" means' }));
    expect((await screen.findAllByText(OTHER_AGENTS_EXPLANATION)).length).toBeGreaterThan(0);
  });
});
