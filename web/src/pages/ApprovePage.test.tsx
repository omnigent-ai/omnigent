// Tests for the standalone URL-mode ApprovePage
// (`/approve/:sessionId/:elicitationId`). The page is driven entirely by a
// single `authenticatedFetch` helper (mocked here) for both the initial GET
// and the resolve POST, and by route params via react-router (a real
// MemoryRouter + Route supplies them, since `@/lib/routing` falls back to
// react-router-dom). Each test pins one of the page's state-machine branches:
// loading, pending, resolved, submitted (approve/reject), and the error paths
// (bad status, network throw, missing params).

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApprovePage } from "./ApprovePage";
import * as identity from "@/lib/identity";

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
    expect(screen.getByText("Loading elicitation…")).toBeInTheDocument();
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

  it("renders a native permission message with its tool name in bold", async () => {
    // WHY: the hook's "… wants to call **Bash**" must not show its asterisks here
    // any more than on the chat card.
    vi.mocked(identity.authenticatedFetch).mockResolvedValue(
      jsonResponse({ status: "pending", message: "Claude wants to call **Bash**" }),
    );
    renderPage();
    const bold = await screen.findByText("Bash", { selector: "strong" });
    expect(bold.parentElement?.textContent).toBe("Claude wants to call Bash");
  });

  it("shows the resolved state when the elicitation is no longer pending", async () => {
    // WHY: a `status: "resolved"` payload means the prompt was already
    // resolved/timed-out/cancelled — no buttons, just an informational alert.
    vi.mocked(identity.authenticatedFetch).mockResolvedValue(jsonResponse({ status: "resolved" }));
    renderPage();
    expect(await screen.findByText("Elicitation resolved")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
  });

  it("surfaces a server error when the GET returns a non-ok status", async () => {
    // WHY: a non-ok response routes to the `error` branch with the HTTP status.
    vi.mocked(identity.authenticatedFetch).mockResolvedValue(
      jsonResponse(null, { ok: false, status: 500 }),
    );
    renderPage();
    expect(await screen.findByText("Server error: 500")).toBeInTheDocument();
  });

  it("surfaces a load error when the GET rejects", async () => {
    // WHY: a thrown fetch (network failure) routes to the `error` branch.
    vi.mocked(identity.authenticatedFetch).mockRejectedValue(new Error("offline"));
    renderPage();
    expect(await screen.findByText(/Failed to load:/)).toBeInTheDocument();
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

    await waitFor(() => expect(screen.getByText("Approved")).toBeInTheDocument());
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

    await waitFor(() => expect(screen.getByText("Rejected")).toBeInTheDocument());
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
    expect(await screen.findByText("Resolve failed: 409")).toBeInTheDocument();
  });

  it("shows a network error when the resolve POST throws", async () => {
    // WHY: a thrown resolve POST routes to the `error` branch (network error).
    vi.mocked(identity.authenticatedFetch)
      .mockResolvedValueOnce(jsonResponse({ status: "pending", message: "Run the migration?" }))
      .mockRejectedValueOnce(new Error("boom"));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Reject/ }));
    expect(await screen.findByText(/Network error:/)).toBeInTheDocument();
  });
});

it.each(["success", "http error", "network error"])(
  "observes confirmation while submission is stalled, despite stale polls, a network blip and late %s",
  async (postOutcome) => {
    vi.useFakeTimers();
    const waiting = {
      status: "pending",
      policy_name: "kiro_native_permission",
      message: "Approve pwd",
      native_delivery: { state: "unconfirmed", attempt: "retry-2", action: "accept" },
    };
    let finishPoll!: (response: Response) => void;
    let finishSubmit!: (response: Response) => void;
    let failSubmit!: (error: Error) => void;
    let reads = 0;
    const fetch = vi.mocked(identity.authenticatedFetch);
    fetch.mockImplementation(async (_url, init) => {
      if (init?.method === "POST") {
        return new Promise<Response>((resolve, reject) => {
          finishSubmit = resolve;
          failSubmit = reject;
        });
      }
      reads++;
      if (reads === 1) return jsonResponse(waiting);
      if (reads === 2)
        return new Promise<Response>((resolve) => {
          finishPoll = resolve;
        });
      if (reads === 3) throw new Error("temporary disconnect");
      if (reads === 4) return jsonResponse(waiting);
      return jsonResponse({ status: "resolved", resolution: "confirmed" });
    });
    try {
      await act(async () => {
        renderPage();
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      fireEvent.click(screen.getByRole("button", { name: "Retry sending approval" }));
      expect(screen.getByText("Waiting for Kiro to confirm")).toBeInTheDocument();
      await act(async () => {
        finishPoll(jsonResponse(waiting));
      });
      expect(
        screen.queryByRole("button", { name: "Retry sending approval" }),
      ).not.toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      expect(screen.getByText("Waiting for Kiro to confirm")).toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      expect(screen.getByText("Waiting for Kiro to confirm")).toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      expect(screen.getByText("Elicitation resolved")).toBeInTheDocument();
      await act(async () => {
        if (postOutcome === "network error") failSubmit(new Error("connection lost"));
        else finishSubmit(jsonResponse({}, { ok: postOutcome === "success", status: 500 }));
      });
      expect(screen.getByText("Elicitation resolved")).toBeInTheDocument();
      const posts = fetch.mock.calls.filter(([, init]) => init?.method === "POST");
      expect(posts).toHaveLength(1);
      expect(JSON.parse(posts[0]![1]!.body as string)).toEqual({
        action: "accept",
        _meta: { native_delivery_attempt: "retry-2" },
      });
    } finally {
      cleanup();
      vi.useRealTimers();
    }
  },
);

it.each(
  ["success", "http error", "network error"].flatMap((outcome) =>
    [false, true].map((retryFirst) => ({ outcome, retryFirst })),
  ),
)(
  "adopts a newer attempt during a stalled POST ($outcome, retry first: $retryFirst)",
  async ({ outcome, retryFirst }) => {
    vi.useFakeTimers();
    let delivery = { state: "ready", attempt: "first", action: "accept" };
    const submissions: { resolve: (value: Response) => void; reject: (error: Error) => void }[] =
      [];
    const fetch = vi.mocked(identity.authenticatedFetch);
    fetch.mockImplementation(async (_url, init) => {
      if (init?.method === "POST") {
        return new Promise<Response>((resolve, reject) => {
          submissions.push({ resolve, reject });
        });
      }
      return jsonResponse({ status: "pending", message: "Approve pwd", native_delivery: delivery });
    });
    const poll = () =>
      act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
    const retry = () =>
      fireEvent.click(screen.getByRole("button", { name: "Retry sending approval" }));
    try {
      await act(async () => {
        renderPage();
      });
      fireEvent.click(screen.getByRole("button", { name: "Approve" }));
      await poll();
      expect(screen.getByText("Waiting for Kiro to confirm")).toBeInTheDocument();
      delivery = { state: "unconfirmed", attempt: "second", action: "accept" };
      await poll();
      expect(screen.getByRole("button", { name: "Retry sending approval" })).toBeEnabled();
      if (retryFirst) retry();
      await act(async () => {
        if (outcome === "network error") submissions[0]!.reject(new Error("offline"));
        else submissions[0]!.resolve(jsonResponse({}, { ok: outcome === "success", status: 500 }));
      });
      if (!retryFirst) retry();
      await poll();
      expect(screen.getByText("Waiting for Kiro to confirm")).toBeInTheDocument();
      expect(submissions).toHaveLength(2);
      const posts = fetch.mock.calls.filter(([, init]) => init?.method === "POST");
      expect(JSON.parse(posts[1]![1]!.body as string)).toEqual({
        action: "accept",
        _meta: { native_delivery_attempt: "second" },
      });
      await act(async () => {
        submissions[1]!.reject(new Error("retry failed"));
      });
      expect(screen.getByText(/Network error:/)).toBeInTheDocument();
      await poll();
      expect(screen.getByRole("button", { name: "Retry sending approval" })).toBeEnabled();
    } finally {
      cleanup();
      vi.useRealTimers();
    }
  },
);

it.each([false, true])(
  "recovers an expired native request without reloading (cold: %s)",
  async (cold) => {
    vi.useFakeTimers();
    const waiting = {
      status: "pending",
      message: "Approve pwd",
      native_delivery: { state: "unconfirmed", attempt: "recovered", action: "decline" },
    };
    const expired = { status: "resolved", resolution: "unanswered" };
    const fetch = vi.mocked(identity.authenticatedFetch);
    fetch.mockResolvedValue(jsonResponse(cold ? expired : waiting));
    const poll = () =>
      act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
    try {
      await act(async () => {
        renderPage();
      });
      fetch.mockResolvedValue(jsonResponse(expired));
      await poll();
      expect(screen.getByText("Delivery unconfirmed")).toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Retry sending rejection" }),
      ).not.toBeInTheDocument();
      // Cache eviction or an older replica is not confirmation either.
      fetch.mockResolvedValue(jsonResponse({ status: "resolved" }));
      await poll();
      fetch.mockRejectedValue(new Error("offline"));
      await poll();
      expect(screen.getByText("Delivery unconfirmed")).toBeInTheDocument();
      fetch.mockResolvedValue(jsonResponse(waiting));
      await poll();
      fireEvent.click(screen.getByRole("button", { name: "Retry sending rejection" }));
      const posts = fetch.mock.calls.filter(([, init]) => init?.method === "POST");
      expect(posts).toHaveLength(1);
      expect(JSON.parse(posts[0]![1]!.body as string)).toEqual({
        action: "decline",
        _meta: { native_delivery_attempt: "recovered" },
      });
      fetch.mockResolvedValue(jsonResponse({ status: "resolved", resolution: "confirmed" }));
      await poll();
      expect(screen.getByText("Elicitation resolved")).toBeInTheDocument();
      const reads = fetch.mock.calls.length;
      fetch.mockResolvedValue(jsonResponse(waiting));
      await poll();
      expect(fetch.mock.calls).toHaveLength(reads);
      expect(
        screen.queryByRole("button", { name: "Retry sending rejection" }),
      ).not.toBeInTheDocument();
    } finally {
      cleanup();
      vi.useRealTimers();
    }
  },
);
