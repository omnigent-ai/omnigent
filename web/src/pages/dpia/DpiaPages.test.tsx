import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as availableAgents from "@/hooks/useAvailableAgents";
import * as caseSession from "@/lib/dpia/caseSession";
import * as liveInvestigation from "@/lib/dpia/liveInvestigation";
import type * as DpiaApiModule from "@/lib/dpia/dpiaApi";
import { loadDpiaCase, saveDpiaCase } from "@/lib/dpia/dpiaApi";
import { DpiaCasePage } from "./DpiaCasePage";
import { DpiaNewAssessmentPage } from "./DpiaNewAssessmentPage";
import { DpiaPortfolioPage } from "./DpiaPortfolioPage";

const { loadDurableMock, saveDurableMock } = vi.hoisted(() => ({
  loadDurableMock: vi.fn(),
  saveDurableMock: vi.fn(),
}));

vi.mock("@/lib/dpia/liveInvestigation", () => ({
  runLiveInvestigation: vi.fn(),
}));
vi.mock("@/lib/dpia/caseSession", () => ({
  findOrCreateDpiaCaseSession: vi.fn(),
}));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: vi.fn(),
}));
vi.mock("@/lib/dpia/dpiaApi", async (importOriginal) => ({
  ...(await importOriginal<typeof DpiaApiModule>()),
  loadDurableDpiaCase: loadDurableMock,
  saveDurableDpiaCase: saveDurableMock,
}));
vi.mock("@/hooks/useDpiaRequests", () => ({
  useDpiaRequests: () => ({ data: [], isLoading: false, isError: false }),
  useDpiaContributorResponses: () => ({ data: [], refetch: vi.fn() }),
}));
vi.mock("./DpiaCaseChat", () => ({
  DpiaCaseChat: () => null,
}));

function renderCase() {
  return render(
    <MemoryRouter initialEntries={["/dpia/cases/student-success-alert"]}>
      <Routes>
        <Route path="/dpia/cases/:caseId" element={<DpiaCasePage />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  localStorage.clear();
  let revision = 1;
  loadDurableMock.mockReset();
  saveDurableMock.mockReset();
  loadDurableMock.mockImplementation(async (caseId: string) => {
    const loaded = loadDpiaCase(caseId);
    return {
      ...loaded,
      revision,
      createdBy: "officer@example.com",
      updatedBy: "officer@example.com",
      createdAt: 1,
      updatedAt: revision,
    };
  });
  saveDurableMock.mockImplementation(async (snapshot, expectedRevision) => {
    if (expectedRevision !== revision) throw new Error("Unexpected test revision.");
    const caseData = saveDpiaCase(snapshot);
    revision += 1;
    return {
      caseData,
      source: "persisted",
      recoveredInvalidState: false,
      revision,
      createdBy: "officer@example.com",
      updatedBy: "officer@example.com",
      createdAt: 1,
      updatedAt: revision,
    };
  });
  vi.mocked(liveInvestigation.runLiveInvestigation).mockReset();
  vi.mocked(caseSession.findOrCreateDpiaCaseSession).mockReset();
  vi.mocked(availableAgents.useAvailableAgents).mockReturnValue({
    data: [
      {
        id: "agent_dpia",
        name: "dpia-investigation",
        display_name: "DPIA Investigation",
        description: null,
        harness: null,
        skills: [],
      },
    ],
    isLoading: false,
  } as never);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("DPIA product routes", () => {
  it("renders the portfolio with the seeded case and inspectable five-of-eight readiness", () => {
    render(
      <MemoryRouter>
        <DpiaPortfolioPage />
      </MemoryRouter>,
    );

    expect(screen.getByRole("heading", { name: "DPIA Investigation Desk" })).toBeInTheDocument();
    expect(
      screen.getByText("Student Success Alert — AI Early-Warning and Intervention"),
    ).toBeInTheDocument();
    expect(screen.getByText("5/8")).toBeInTheDocument();
    expect(screen.getByText("Potentially high")).toBeInTheDocument();
  });

  it("renders the synthetic new-assessment entry with a real-data warning", () => {
    render(
      <MemoryRouter>
        <DpiaNewAssessmentPage />
      </MemoryRouter>,
    );

    expect(screen.getByRole("heading", { name: "New DPIA assessment" })).toBeInTheDocument();
    expect(screen.getByText("Synthetic data only")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create synthetic assessment" })).toBeInTheDocument();
  });

  it("renders all six cockpit sections and the validated snapshot state", () => {
    renderCase();

    expect(screen.getByRole("heading", { name: /Student Success Alert/ })).toBeInTheDocument();
    expect(screen.getByText("Validated demo snapshot")).toBeInTheDocument();
    expect(screen.getByText("5/8 determination areas answerable")).toBeInTheDocument();
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "Overview",
      "Processing map",
      "Evidence & questions",
      "Screening",
      "Full DPIA",
      "Audit",
    ]);
  });
});

describe("DPIA case workflow", () => {
  it("connects and persists a labelled case session", async () => {
    vi.mocked(caseSession.findOrCreateDpiaCaseSession).mockResolvedValue({
      sessionId: "conv_dpia",
      status: "idle",
      created: true,
    });
    renderCase();

    fireEvent.click(screen.getByRole("button", { name: "Connect case agent" }));

    const fullSessionLink = await screen.findByRole("link", { name: "Open full session" });
    expect(fullSessionLink).toHaveAttribute("href", "/c/conv_dpia");
    expect(screen.getByText("Case agent bound")).toBeInTheDocument();
    expect(loadDpiaCase("student-success-alert").caseData.sessionId).toBe("conv_dpia");
  });

  it("persists a material intake edit across reload and marks dependent conclusions stale", async () => {
    const firstRender = renderCase();
    fireEvent.click(screen.getByRole("button", { name: "Edit intake" }));
    const effectField = await screen.findByRole("textbox", { name: "Effect on students" });
    fireEvent.change(effectField, {
      target: {
        value:
          "Persistent high-risk scores can trigger attendance escalation or fitness-to-study referral.",
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save new version" }));

    expect(await screen.findByText("Processing model v4")).toBeInTheDocument();
    expect(screen.getAllByText("Stale after change").length).toBeGreaterThan(0);
    firstRender.unmount();

    renderCase();
    expect(screen.getByText("Processing model v4")).toBeInTheDocument();
    expect(loadDpiaCase("student-success-alert")).toMatchObject({
      source: "persisted",
      caseData: { processingModel: { version: 4 } },
    });
  });

  it("opens an evidence provenance drawer with source, owner, excerpt, and artifact id", async () => {
    renderCase();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Evidence & questions" }));
    const trigger = screen.getByRole("button", { name: /Project intake form/ });
    trigger.focus();
    fireEvent.click(trigger);

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Student Services");
    expect(dialog).toHaveTextContent("Programme Director");
    expect(dialog).toHaveTextContent("The service is support only");
    expect(dialog).toHaveTextContent("ev-intake");
    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("records an officer acceptance and carries the same processing model into Full DPIA", async () => {
    renderCase();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Screening" }));
    fireEvent.click(screen.getByRole("button", { name: "Accept recommendation" }));
    expect(
      await screen.findByRole("heading", { name: "Accept screening recommendation" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Record decision" }));

    expect(await screen.findByText("Screening accepted and carried forward")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Continue to Full DPIA/ }));
    expect(screen.getByText("Screening facts carried forward")).toBeInTheDocument();
    expect(screen.getByText("No repeated intake fields")).toBeInTheDocument();
    expect(screen.getAllByText("False-positive intervention or escalation")).toHaveLength(2);
    expect(loadDpiaCase("student-success-alert").caseData.officerDecision).toMatchObject({
      action: "accepted",
      outcome: "full-dpia-likely",
      officer: "Alex Morgan",
    });
  });

  it("records an attributed request for more information", async () => {
    renderCase();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Screening" }));
    fireEvent.click(screen.getByRole("button", { name: "Ask for information" }));
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByRole("heading", { name: "Ask for more information" }),
    ).toBeInTheDocument();
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Officer rationale" }), {
      target: { value: "Obtain the hosting, transfer, and deletion evidence before deciding." },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Record decision" }));

    await waitFor(() =>
      expect(loadDpiaCase("student-success-alert").caseData.officerDecision).toMatchObject({
        action: "more-information",
        outcome: "more-information-required",
        officer: "Alex Morgan",
      }),
    );
  });

  it("requires and persists a substantive rejection rationale", async () => {
    renderCase();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Screening" }));
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    const dialog = await screen.findByRole("dialog");
    const recordButton = within(dialog).getByRole("button", { name: "Record decision" });
    expect(recordButton).toBeDisabled();
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Officer rationale" }), {
      target: {
        value:
          "The evidence does not establish likely high risk once the support-only boundary is binding.",
      },
    });
    expect(recordButton).toBeEnabled();
    fireEvent.click(recordButton);

    await waitFor(() =>
      expect(loadDpiaCase("student-success-alert").caseData.officerDecision).toMatchObject({
        action: "rejected",
        outcome: "no-full-dpia-indicated",
        officer: "Alex Morgan",
      }),
    );
  });

  it("invalidates an accepted decision and refuses export after a material edit", async () => {
    renderCase();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Screening" }));
    fireEvent.click(screen.getByRole("button", { name: "Accept recommendation" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Record decision" }),
    );
    await screen.findByText("Screening accepted and carried forward");

    fireEvent.mouseDown(screen.getByRole("tab", { name: "Overview" }));
    fireEvent.click(screen.getByRole("button", { name: "Edit intake" }));
    fireEvent.change(await screen.findByRole("textbox", { name: "Purpose" }), {
      target: { value: "Predict disengagement and support attendance escalation decisions." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save new version" }));
    await screen.findByText("Processing model v4");
    fireEvent.click(screen.getByRole("button", { name: "Print decision pack" }));

    expect(
      await screen.findByRole("heading", { name: "Decision pack is not ready" }),
    ).toBeInTheDocument();
    const persisted = loadDpiaCase("student-success-alert").caseData;
    expect(persisted.officerDecision).toBeUndefined();
    expect(persisted.determinations.some(({ status }) => status === "stale-after-change")).toBe(
      true,
    );
  });

  it("restores focus through a finding-to-evidence provenance handoff", async () => {
    renderCase();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Screening" }));
    const findingTrigger = screen.getByRole("button", {
      name: /Is the purpose, scope, and affected population sufficiently defined/,
    });
    findingTrigger.focus();
    fireEvent.click(findingTrigger);
    const findingDialog = await screen.findByRole("dialog");
    fireEvent.click(within(findingDialog).getByRole("button", { name: /Project intake form/ }));
    const evidenceDialog = await screen.findByRole("dialog");
    expect(evidenceDialog).toHaveTextContent("Evidence ID");
    fireEvent.click(within(evidenceDialog).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(findingTrigger).toHaveFocus());
  });

  it("refuses export before an officer decision", async () => {
    renderCase();
    fireEvent.click(screen.getByRole("button", { name: "Print decision pack" }));

    expect(
      await screen.findByRole("heading", { name: "Decision pack is not ready" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Privacy Officer decision is required/)).toBeInTheDocument();
  });

  it("shows a live-run failure without replacing the validated snapshot", async () => {
    vi.mocked(liveInvestigation.runLiveInvestigation).mockRejectedValue(
      new Error("No labelled DPIA root session is configured."),
    );
    renderCase();
    fireEvent.click(
      within(screen.getByLabelText("Case actions")).getByRole("button", {
        name: "Agent activity",
      }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Re-run investigation" }));

    expect(
      await screen.findByText("No labelled DPIA root session is configured."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry live run" })).toBeInTheDocument();
    expect(screen.getAllByText("Validated demo snapshot").length).toBeGreaterThan(0);
    expect(loadDpiaCase("student-success-alert")).toMatchObject({
      source: "persisted",
      caseData: {
        processingModel: { version: 3 },
        liveRun: { status: "failed", message: "No labelled DPIA root session is configured." },
      },
    });
    await waitFor(() => expect(liveInvestigation.runLiveInvestigation).toHaveBeenCalledTimes(1));
  });

  it("shows successful live completion as a separate Omnigent session", async () => {
    vi.mocked(liveInvestigation.runLiveInvestigation).mockImplementation(
      async (_caseData, onProgress) => {
        onProgress("Professional-role sessions are working");
        return { sessionId: "conv_dpia_live", completedAt: "2026-08-21T13:00:00Z" };
      },
    );
    renderCase();
    fireEvent.click(
      within(screen.getByLabelText("Case actions")).getByRole("button", {
        name: "Agent activity",
      }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Re-run investigation" }));

    expect(
      await screen.findByText(/live root session completed.*remains separate/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open live session/ })).toHaveAttribute(
      "href",
      "/c/conv_dpia_live",
    );
    expect(loadDpiaCase("student-success-alert")).toMatchObject({
      source: "persisted",
      caseData: {
        processingModel: { version: 3 },
        liveRun: { status: "completed", sessionId: "conv_dpia_live" },
      },
    });
  });
});
