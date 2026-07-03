// Share-safety contract for the Share modal.
//
// The backend surfaces the factual isolation boundary of a session's
// primary environment on the resources projection
// (`GET /v1/sessions/{id}/resources/environments/default`), e.g.
// `metadata.sandbox_active: false` / `metadata.sandbox_type: "none"` for an
// unconfined (local, no-sandbox) workspace where a collaborator's shell would
// reach the host filesystem.
//
// A planned feature asks the Web UI to surface a warning in the Share flow when
// a session runs without sandbox isolation, so an owner understands what
// granting access exposes (a shared-session shell needs workspace/sensitive-file
// isolation). That UI does not exist yet — the Share modal renders identical
// markup regardless of environment safety — so the warning assertion below is
// a strict xfail (`it.fails`): it documents the desired contract and flips to a
// hard failure the moment the warning lands, prompting removal of the marker.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { PermissionsModal } from "./PermissionsModal";

vi.mock("@/lib/permissionsApi", () => ({
  listPermissions: vi.fn(),
  grantPermission: vi.fn(),
  revokePermission: vi.fn(),
}));

import * as api from "@/lib/permissionsApi";
const listMock = vi.mocked(api.listPermissions);

// The unsafe (no-sandbox) primary-environment resource, exactly as the backend
// serializes it on the wire: `sandbox_active` is the load-bearing field and is
// false because `sandbox_type` is "none", i.e. a shared collaborator's shell
// would run unconfined against the host.
const UNSAFE_ENV_RESOURCE = {
  id: "default",
  object: "session.resource",
  type: "environment",
  session_id: "conv_unsafe",
  name: "Primary environment",
  metadata: {
    environment_type: "caller_process",
    role: "primary",
    sandbox_type: "none",
    sandbox_active: false,
  },
};

// Text the Share modal would plausibly use to warn about an unsandboxed
// environment. Deliberately broad across phrasings, but disjoint from the
// modal's current copy ("Share this session", "Public access", "People with
// access", grant levels, "Copy link") so it cannot match today's markup.
const SAFETY_WARNING_RE =
  /not sandboxed|no sandbox|unsandboxed|isolation|not isolated|unsafe|sensitive file|local (?:machine|workspace|environment)|shell access|host filesystem|caution|warning/i;

function createWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return (
      <QueryClientProvider client={qc}>
        <TooltipProvider>{children}</TooltipProvider>
      </QueryClientProvider>
    );
  };
}

beforeEach(() => {
  listMock.mockReset();
  // The Share modal does not fetch the environment resource today; a future
  // implementation is expected to read it from the canonical endpoint.
  // Serve the unsafe metadata for any environments-resource request so that,
  // once the warning is wired up, this test exercises the no-sandbox branch.
  // If the implementation sources the signal elsewhere, update this stub
  // alongside the fix.
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      const body = url.includes("/resources/environments/") ? UNSAFE_ENV_RESOURCE : {};
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  cleanup();
});

describe("PermissionsModal share-safety", () => {
  // STRICT XFAIL: no share-safety warning is rendered today. When the
  // Share flow learns to warn for a no-sandbox environment, `it.fails` turns
  // red — delete the marker and keep the assertion.
  it.fails("warns when sharing a session whose primary environment is not sandboxed", async () => {
    // The modal mounts via the same permissions path the other tests cover,
    // so the only operation that can fail here is the warning lookup — it
    // fails today because no warning element exists, not because the modal
    // failed to render.
    listMock.mockResolvedValue([
      { user_id: "owner@example.com", conversation_id: "conv_unsafe", level: 4 },
    ]);

    render(<PermissionsModal sessionId="conv_unsafe" open={true} onOpenChange={() => {}} />, {
      wrapper: createWrapper(),
    });

    // waitFor (not a synchronous query) so a future implementation that
    // renders the warning only after its async environment fetch resolves
    // still satisfies the contract; today it exhausts the timeout because no
    // matching element ever appears.
    await waitFor(() => {
      expect(screen.getByText(SAFETY_WARNING_RE)).toBeInTheDocument();
    });
  });
});
