// Tests for the standalone URL-mode ApprovePage
// (`/approve/:sessionId/:elicitationId`). The page is driven entirely by a
// single `authenticatedFetch` helper (mocked here) for both the initial GET
// and the resolve POST, and by route params via react-router (a real
// MemoryRouter + Route supplies them, since `@/lib/routing` falls back to
// react-router-dom). Each test pins one of the page's state-machine branches:
// loading, pending, resolved, submitted (approve/reject), and the error paths
// (bad status, network throw, missing params).

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApprovePage } from "./ApprovePage";
import * as identity from "@/lib/identity";
import i18n from "@/i18n";

const t = i18n.getFixedT(null, "common");

vi.mock("@/lib/identity", () => ({
  authenticatedFetch: vi.fn(),
}));

/** A Response-like stub: only `ok`, `status`, and `json()` are read. */
function jsonResponse(body: unknown, { ok = true, status = 200 } = {}): Response {
  return {
    ok,
    status,
    json: async () => body,
  } as unknown as Response;
}

/** Render the page at a concrete `/approve/:sessionId/:elicitationId` route. */
function renderPage(sessionId = "sess_1", elicitationId = "eli_1") {
  return render(
    <MemoryRouter initialEntries={[`/approve/${sessionId}/${elicitationId}`]}>
      <Routes>
        <Route path="/approve/:sessionId/:elicitationId" element={<ApprovePage />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ApprovePage states", () => {
  beforeEach(() => {
    // Default: a never-resolving fetch so the initial render is observable
    // before any test overrides the resolution.
    vi.mocked(identity.authenticatedFetch).mockReturnValue(new Promise(() => {}));
  });

  it("shows a loading state while the elicitation fetch is in flight", () => {
    // WHY: the `loading` branch renders until the GET settles.
    renderPage();
    expect(screen.getByText(t("loadingElicitation"))).toBeInTheDocument();
  });

  it("renders approve/reject controls plus message and preview when pending", async () => {
    // WHY: the `pending` branch shows the prompt body, policy/phase chips,
    // formatted preview, and both action buttons.
    vi.mocked(identity.authenticatedFetch).mockResolvedValue(
      jsonResponse({
        status: "pending",
        message: "Delete the production database?",
        phase: "pre",
        policy_name: "danger-policy",
        content_preview: "rm -rf /data",
      }),
    );
    renderPage();
    expect(await screen.findByText("Delete the production database?")).toBeInTheDocument();
    expect(screen.getByText("· danger-policy")).toBeInTheDocument();
    expect(screen.getByText("(pre)")).toBeInTheDocument();
    expect(screen.getByText(/rm -rf \/data/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Approve/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Reject/ })).toBeInTheDocument();
  });

  it("shows the resolved state when the elicitation is no longer pending", async () => {
    // WHY: a `status: "resolved"` payload means the prompt was already
    // resolved/timed-out/cancelled — no buttons, just an informational alert.
    vi.mocked(identity.authenticatedFetch).mockResolvedValue(jsonResponse({ status: "resolved" }));
    renderPage();
    expect(await screen.findByText(t("elicitationResolved"))).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
  });

  it("surfaces a server error when the GET returns a non-ok status", async () => {
    // WHY: a non-ok response routes to the `error` branch with the HTTP status.
    vi.mocked(identity.authenticatedFetch).mockResolvedValue(
      jsonResponse(null, { ok: false, status: 500 }),
    );
    renderPage();
    expect(await screen.findByText(t("serverError", { status: 500 }))).toBeInTheDocument();
  });

  it("surfaces a load error when the GET rejects", async () => {
    // WHY: a thrown fetch (network failure) routes to the `error` branch.
    vi.mocked(identity.authenticatedFetch).mockRejectedValue(new Error("offline"));
    renderPage();
    expect(
      await screen.findByText(`${t("failedToLoad")} Error: offline`),
    ).toBeInTheDocument();
  });
});

describe("ApprovePage submission", () => {
  beforeEach(() => {
    // First call (GET) returns a pending prompt; later calls (POST) are set
    // per-test below.
    vi.mocked(identity.authenticatedFetch).mockResolvedValue(
      jsonResponse({ status: "pending", message: "Run the migration?" }),
    );
  });

  it("approves: posts accept and shows the Approved confirmation", async () => {
    // WHY: clicking Approve POSTs `{action: "accept"}` to the resolve endpoint
    // and lands on the `submitted` (Approved) state.
    renderPage("sess_a", "eli_a");
    fireEvent.click(await screen.findByRole("button", { name: /Approve/ }));

    await waitFor(() => expect(screen.getByText(t("approved"))).toBeInTheDocument());
    const resolveCall = vi
      .mocked(identity.authenticatedFetch)
      .mock.calls.find(([url]) => String(url).includes("/resolve"));
    expect(resolveCall).toBeDefined();
    expect(String(resolveCall![0])).toContain("/v1/sessions/sess_a/elicitations/eli_a/resolve");
    expect(JSON.parse(String(resolveCall![1]!.body))).toEqual({ action: "accept" });
  });

  it("rejects: posts decline and shows the Rejected confirmation", async () => {
    // WHY: clicking Reject POSTs `{action: "decline"}` and lands on the
    // `submitted` (Rejected) state.
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Reject/ }));

    await waitFor(() => expect(screen.getByText(t("rejected"))).toBeInTheDocument());
    const resolveCall = vi
      .mocked(identity.authenticatedFetch)
      .mock.calls.find(([url]) => String(url).includes("/resolve"));
    expect(JSON.parse(String(resolveCall![1]!.body))).toEqual({ action: "decline" });
  });

  it("shows a resolve error when the POST returns a non-ok status", async () => {
    // WHY: a failed resolve POST routes to the `error` branch with the status.
    vi.mocked(identity.authenticatedFetch)
      .mockResolvedValueOnce(jsonResponse({ status: "pending", message: "Run the migration?" }))
      .mockResolvedValueOnce(jsonResponse(null, { ok: false, status: 409 }));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Approve/ }));
    expect(await screen.findByText(t("resolveFailed", { status: 409 }))).toBeInTheDocument();
  });

  it("shows a network error when the resolve POST throws", async () => {
    // WHY: a thrown resolve POST routes to the `error` branch (network error).
    vi.mocked(identity.authenticatedFetch)
      .mockResolvedValueOnce(jsonResponse({ status: "pending", message: "Run the migration?" }))
      .mockRejectedValueOnce(new Error("boom"));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Reject/ }));
    expect(
      await screen.findByText(t("networkError", { error: "Error: boom" })),
    ).toBeInTheDocument();
  });
});
