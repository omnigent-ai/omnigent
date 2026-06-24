// Tests for the admin PoliciesPage (global default policies: list, toggle,
// add, delete).
//
// Browser e2e is impractical (admin/accounts-gated), so the surface is pinned
// here by mocking getMe (admin gate), useNavigate (unauth bounce), and the
// react-query policy hooks (useDefaultPolicies / usePolicyRegistry +
// add/update/delete mutations) so no QueryClient or network is needed.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PoliciesPage } from "./PoliciesPage";
import * as accountsApi from "@/lib/accountsApi";
import * as defaultPolicies from "@/hooks/useDefaultPolicies";
import * as policies from "@/hooks/usePolicies";
import i18n from "@/i18n";

const t = i18n.getFixedT(null, "common");

const navigateMock = vi.fn();
const addMutate = vi.fn();
const updateMutate = vi.fn();
const deleteMutate = vi.fn();
const refetchMock = vi.fn();

vi.mock("@/lib/routing", async (importActual) => ({
  ...(await importActual<typeof import("@/lib/routing")>()),
  useNavigate: () => navigateMock,
}));
vi.mock("@/lib/accountsApi", () => ({ getMe: vi.fn() }));
vi.mock("@/hooks/useDefaultPolicies", () => ({
  useDefaultPolicies: vi.fn(),
  useAddDefaultPolicy: vi.fn(),
  useUpdateDefaultPolicy: vi.fn(),
  useDeleteDefaultPolicy: vi.fn(),
}));
vi.mock("@/hooks/usePolicies", () => ({ usePolicyRegistry: vi.fn() }));

type Policy = ReturnType<typeof policy>;
function policy(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    id: "p1",
    object: "default_policy",
    name: "block_canada",
    type: "python",
    handler: "omnigent.policies.block_canada",
    factory_params: null,
    enabled: true,
    created_at: 1,
    updated_at: null,
    created_by: null,
    ...overrides,
  };
}

/** A useMutation-shaped stub whose mutate invokes onSuccess synchronously. */
function mutationStub(mutate: ReturnType<typeof vi.fn>) {
  mutate.mockImplementation((_arg: unknown, opts?: { onSuccess?: () => void }) =>
    opts?.onSuccess?.(),
  );
  return { mutate, isPending: false, isError: false, error: null };
}

function setPolicies(list: Policy[]) {
  vi.mocked(defaultPolicies.useDefaultPolicies).mockReturnValue({
    data: list,
    refetch: refetchMock,
  } as never);
}

function renderPage() {
  return render(
    <MemoryRouter>
      <PoliciesPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.mocked(accountsApi.getMe).mockResolvedValue({
    id: "admin",
    is_admin: true,
    created_at: null,
    last_login_at: null,
  });
  setPolicies([]);
  vi.mocked(policies.usePolicyRegistry).mockReturnValue({ data: [] } as never);
  vi.mocked(defaultPolicies.useAddDefaultPolicy).mockReturnValue(mutationStub(addMutate) as never);
  vi.mocked(defaultPolicies.useUpdateDefaultPolicy).mockReturnValue(
    mutationStub(updateMutate) as never,
  );
  vi.mocked(defaultPolicies.useDeleteDefaultPolicy).mockReturnValue(
    mutationStub(deleteMutate) as never,
  );
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("PoliciesPage gating", () => {
  it("shows a loading state until the identity probe resolves", () => {
    vi.mocked(accountsApi.getMe).mockReturnValue(new Promise(() => {}));
    renderPage();
    expect(screen.getByText(t("loading"))).toBeInTheDocument();
  });

  it("blocks non-admins with a permission message", async () => {
    vi.mocked(accountsApi.getMe).mockResolvedValue({
      id: "alice",
      is_admin: false,
      created_at: null,
      last_login_at: null,
    });
    renderPage();
    expect(
      await screen.findByText(t("noPermissionGlobalPolicies")),
    ).toBeInTheDocument();
  });

  it("bounces an unauthenticated visitor to /login", async () => {
    vi.mocked(accountsApi.getMe).mockResolvedValue(null);
    renderPage();
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/login", { replace: true }));
  });
});

describe("PoliciesPage list", () => {
  it("shows the empty state when no global policies are configured", async () => {
    renderPage();
    expect(await screen.findByText(t("noGlobalPoliciesConfigured"))).toBeInTheDocument();
  });

  it("renders each policy with its handler, a Disabled badge, and parameters", async () => {
    setPolicies([
      policy({ id: "p1", name: "block_canada", enabled: false }),
      policy({
        id: "p2",
        name: "rate_limit",
        handler: "omnigent.policies.rate_limit",
        factory_params: { max_per_min: 5 },
      }),
    ]);
    renderPage();

    expect(await screen.findByText("block_canada")).toBeInTheDocument();
    expect(screen.getByText("omnigent.policies.block_canada")).toBeInTheDocument();
    expect(screen.getByText(t("disabled"))).toBeInTheDocument(); // only the disabled one
    // factory_params render as a "Parameters" block.
    expect(screen.getByText(t("parameters"))).toBeInTheDocument();
    expect(screen.getByText("max_per_min:")).toBeInTheDocument();
  });
});

describe("PoliciesPage actions", () => {
  it("toggling a policy's switch fires the update mutation with the new state", async () => {
    setPolicies([policy({ id: "p1", name: "block_canada", enabled: true })]);
    renderPage();

    const toggle = await screen.findByRole("switch", {
      name: t("togglePolicy", { name: "block_canada" }),
    });
    fireEvent.click(toggle);
    expect(updateMutate).toHaveBeenCalledWith({ policyId: "p1", enabled: false });
  });

  it("deletes a policy through the confirmation dialog", async () => {
    setPolicies([policy({ id: "p1", name: "block_canada" })]);
    renderPage();

    await screen.findByText("block_canada");
    fireEvent.click(screen.getByRole("button", { name: t("removePolicy") }));

    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText(t("removePolicyConfirmTitle", { name: "block_canada" })),
    ).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: t("remove") }));

    expect(deleteMutate).toHaveBeenCalledWith("p1", expect.anything());
  });

  it("adds a global policy from the registry via the Add dialog", async () => {
    vi.mocked(policies.usePolicyRegistry).mockReturnValue({
      data: [
        {
          handler: "omnigent.policies.block_canada",
          kind: "callable",
          name: "Block Canada",
          description: "Deny anything mentioning Canada.",
          params_schema: null,
        },
      ],
    } as never);
    renderPage();
    await screen.findByText(t("noGlobalPoliciesConfigured"));

    fireEvent.click(screen.getByRole("button", { name: t("addPolicyButton") }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByText("Block Canada")); // select the registry entry
    fireEvent.click(within(dialog).getByRole("button", { name: t("add") }));

    expect(addMutate).toHaveBeenCalledWith(
      { name: "block_canada", type: "python", handler: "omnigent.policies.block_canada" },
      expect.anything(),
    );
  });
});
