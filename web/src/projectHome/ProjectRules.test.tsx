import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import type { Project } from "@/lib/projectsApi";
import type * as MandateModule from "./ProjectMandateDialog";
import { ProjectRules } from "./ProjectRules";

const mocks = vi.hoisted(() => ({
  project: undefined as Project | undefined,
  viewer: "member@example.com" as string | null,
  settings: vi.fn(),
}));

vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => mocks.viewer }));
vi.mock("./useProjectTickets", () => ({
  useTicketProject: () => ({ projectId: "project", projects: { isLoading: false } }),
  useTicketStagesEnabled: () => true,
  useTicketStages: () => ({
    supported: true,
    data: {
      initial_stage_id: "open",
      stages: [
        { id: "open", name: "Pending", instructions: "", is_complete: false },
        { id: "done", name: "Verified", instructions: "Attach a demo.", is_complete: true },
      ],
    },
  }),
}));
vi.mock("./ProjectMandateDialog", async (importOriginal) => ({
  ...(await importOriginal<typeof MandateModule>()),
  useProjectMandate: () => ({ data: mocks.project, isPending: false }),
  ProjectMandateDialog: () => <div role="dialog">Mandate dialog</div>,
}));
vi.mock("./ProjectTicketStagesDialog", () => ({
  ProjectTicketStagesDialog: () => <div role="dialog">Stages dialog</div>,
}));
vi.mock("@/shell/ProjectSharing", () => ({
  ProjectSharingSection: () => <div data-testid="sharing-section" />,
  ProjectProductApproversSection: () => <div data-testid="approvers-section" />,
}));
vi.mock("@/shell/ProjectSettingsDialog", () => ({
  ProjectSettingsDialog: (props: { open: boolean; defaultsOnly?: boolean }) => {
    mocks.settings(props);
    return props.open ? <div role="dialog">Session defaults dialog</div> : null;
  },
}));

function renderRules() {
  render(
    <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, project_mandate: true }}>
      <ProjectRules projectName="Payments" />
    </CapabilitiesProvider>,
  );
}

beforeEach(() => {
  mocks.viewer = "member@example.com";
  mocks.project = {
    id: "project",
    name: "Payments",
    shared: true,
    mandate: "Only change docs/.",
    mandate_proposals: [{ id: "p1", text: "Add a rule.", created_at: 1 }],
    config: { product_approvers: [] },
  } as Project;
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ProjectRules", () => {
  it("gathers the mandate, stages, sign-offs, people and session defaults", () => {
    renderRules();
    expect(screen.getByRole("heading", { name: "How this project works" })).toBeTruthy();
    expect(screen.getByTestId("project-rules-mandate")).toHaveTextContent("Only change docs/.");
    const stages = screen.getAllByTestId("project-rules-stage").map((row) => row.textContent);
    expect(stages[0]).toContain("1. Pending");
    expect(stages[1]).toContain("2. Verified");
    expect(stages[1]).toContain("Attach a demo.");
    const signOffs = screen.getAllByTestId("project-rules-sign-off").map((row) => row.textContent);
    expect(signOffs.map((text) => text?.match(/^[A-Z][a-z]+( approval)?/)?.[0])).toEqual([
      "Review",
      "Product approval",
      "Risk approval",
      "Owner approval",
    ]);
    // The technical label names sit behind a disclosure, still in order.
    const labels = within(screen.getByTestId("project-rules-label-details"))
      .getAllByTestId("project-rules-sign-off-label")
      .map((row) => row.textContent?.match(/approved|product-ok|risk-ok|owner-ok/)?.[0]);
    expect(labels).toEqual(["approved", "product-ok", "risk-ok", "owner-ok"]);
    const nav = within(screen.getByRole("navigation", { name: "Rules sections" }));
    expect(nav.getAllByRole("link").map((link) => link.textContent)).toEqual([
      "Mandate",
      "Stages",
      "Who approves what",
      "People",
      "Advanced",
    ]);
    expect(screen.getByTestId("approvers-section")).toBeTruthy();
    expect(screen.getByTestId("sharing-section")).toBeTruthy();

    fireEvent.click(screen.getByTestId("project-rules-session-defaults"));
    expect(screen.getByRole("dialog")).toHaveTextContent("Session defaults dialog");
    expect(mocks.settings).toHaveBeenLastCalledWith(
      expect.objectContaining({ open: true, defaultsOnly: true }),
    );
  });

  it("shows a member the mandate read-only, with proposals waiting on someone else", () => {
    renderRules();
    const mandate = screen.getByTestId("project-rules-mandate");
    expect(within(mandate).getByTestId("project-rules-mandate-open")).toHaveTextContent("View");
    expect(mandate).toHaveTextContent("Only the owner or a product approver can change it");
    expect(screen.getByTestId("project-rules-mandate-pending")).toHaveTextContent(
      "1 proposed change waiting for the owner or a product approver.",
    );
    expect(screen.queryByTestId("project-rules-mandate-review")).toBeNull();
  });

  it("lets the owner, or a member who is a product approver, edit the mandate", () => {
    mocks.project = { ...mocks.project!, shared: false };
    renderRules();
    expect(screen.getByTestId("project-rules-mandate-open")).toHaveTextContent("Edit mandate");
    expect(screen.getByTestId("project-rules-mandate-pending")).toHaveTextContent(
      "1 proposed change to the mandate",
    );
    fireEvent.click(screen.getByTestId("project-rules-mandate-review"));
    expect(screen.getByRole("dialog")).toHaveTextContent("Mandate dialog");
    cleanup();

    mocks.project = {
      ...mocks.project!,
      shared: true,
      config: { product_approvers: [mocks.viewer!] },
    };
    renderRules();
    fireEvent.click(screen.getByTestId("project-rules-mandate-open"));
    expect(screen.getByRole("dialog")).toHaveTextContent("Mandate dialog");
  });

  it("opens the existing stage editor", () => {
    renderRules();
    fireEvent.click(screen.getByTestId("project-rules-stages-edit"));
    expect(screen.getByRole("dialog")).toHaveTextContent("Stages dialog");
  });
});
