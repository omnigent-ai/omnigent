// Policy controls and identity recovery. Most collaborators are mocked;
// recovery exercises the real query hook and cache.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PoliciesPage } from "./PoliciesPage";
import * as identity from "@/lib/identity";
import * as defaultPolicies from "@/hooks/useDefaultPolicies";
import * as policies from "@/hooks/usePolicies";

const serverInfoMocks = vi.hoisted(() => ({
  accountsEnabled: true,
  loginUrl: null as string | null,
  serverVersion: "0.3.0.dev0" as string | null,
  singleUser: false,
}));

vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({
    accounts_enabled: serverInfoMocks.accountsEnabled,
    login_url: serverInfoMocks.loginUrl,
    server_version: serverInfoMocks.serverVersion,
    single_user: serverInfoMocks.singleUser,
  }),
}));

const addMutate = vi.fn();
const updateMutate = vi.fn();
const deleteMutate = vi.fn();
const refetchMock = vi.fn();

const identityUpdates = vi.hoisted(() => new Set<() => void>());

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof identity>()),
  resolveIdentity: vi.fn(),
  getCurrentIsAdmin: vi.fn(),
  getCurrentUserId: vi.fn(),
  subscribeIdentity: (listener: () => void) => {
    identityUpdates.add(listener);
    return () => identityUpdates.delete(listener);
  },
}));

beforeEach(() => identityUpdates.clear());
vi.mock("@/hooks/useDefaultPolicies", async (importOriginal) => ({
  ...(await importOriginal<typeof defaultPolicies>()),
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

function setPoliciesForbidden() {
  vi.mocked(defaultPolicies.useDefaultPolicies).mockReturnValue({
    data: undefined,
    refetch: refetchMock,
    error: new defaultPolicies.PoliciesForbiddenError("nope"),
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
  serverInfoMocks.accountsEnabled = true;
  serverInfoMocks.loginUrl = null;
  serverInfoMocks.serverVersion = "0.3.0.dev0";
  serverInfoMocks.singleUser = false;
  vi.mocked(identity.resolveIdentity).mockResolvedValue("admin");
  vi.mocked(identity.getCurrentIsAdmin).mockReturnValue(true);
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
  vi.unstubAllGlobals();
});

describe("PoliciesPage gating", () => {
  it("shows a loading state until the identity probe resolves", () => {
    vi.mocked(identity.resolveIdentity).mockReturnValue(new Promise(() => {}));
    renderPage();
    expect(screen.getByText("Loading...")).toBeInTheDocument();
  });

  it("blocks non-admins with a permission message", async () => {
    vi.mocked(identity.resolveIdentity).mockResolvedValue("alice");
    vi.mocked(identity.getCurrentIsAdmin).mockReturnValue(false);
    renderPage();
    expect(
      await screen.findByText("You don't have permission to manage global policies."),
    ).toBeInTheDocument();
    expect(defaultPolicies.useDefaultPolicies).toHaveBeenLastCalledWith(false);
  });

  it.each([true, false])(
    "recovers an exhausted policies query after identity becomes available (allowed: %s)",
    async (allowed) => {
      const actual = await vi.importActual<typeof defaultPolicies>("@/hooks/useDefaultPolicies");
      vi.mocked(defaultPolicies.useDefaultPolicies).mockImplementation(actual.useDefaultPolicies);
      vi.mocked(identity.resolveIdentity).mockResolvedValue(null);
      vi.mocked(identity.getCurrentUserId).mockReturnValue(null);
      vi.mocked(identity.getCurrentIsAdmin).mockReturnValue(false);
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      client.setQueryData(actual.DEFAULT_POLICIES_QUERY_KEY, [policy({ name: "previous_policy" })]);
      const denied = new actual.PoliciesForbiddenError("Not permitted to list default policies");
      await expect(
        client.fetchQuery({
          queryKey: actual.DEFAULT_POLICIES_QUERY_KEY,
          queryFn: () => Promise.reject(denied),
        }),
      ).rejects.toBe(denied);
      let respond!: (response: Response) => void;
      const fetch = vi.fn().mockReturnValue(
        new Promise<Response>((resolve) => {
          respond = resolve;
        }),
      );
      vi.stubGlobal("fetch", fetch);
      const view = render(
        <QueryClientProvider client={client}>
          <MemoryRouter>
            <PoliciesPage />
          </MemoryRouter>
        </QueryClientProvider>,
      );
      await waitFor(() => expect(identity.resolveIdentity).toHaveBeenCalled());
      expect(screen.getByText("Loading...")).toBeInTheDocument();
      expect(fetch).not.toHaveBeenCalled();

      vi.mocked(identity.getCurrentUserId).mockReturnValue("admin");
      vi.mocked(identity.getCurrentIsAdmin).mockReturnValue(true);
      act(() => {
        for (const listener of identityUpdates) listener();
      });

      await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
      expect(
        screen.queryByText("You don't have permission to manage global policies."),
      ).not.toBeInTheDocument();
      respond(
        new Response(
          JSON.stringify({ object: "list", data: [policy({ name: "recovered_policy" })] }),
          { status: allowed ? 200 : 403 },
        ),
      );
      if (allowed) {
        expect(await screen.findByText("recovered_policy")).toBeInTheDocument();
        expect(screen.getByRole("button", { name: "Add policy" })).toBeInTheDocument();
      } else {
        expect(
          await screen.findByText("You don't have permission to manage global policies."),
        ).toBeInTheDocument();
        expect(screen.queryByRole("button", { name: "Add policy" })).not.toBeInTheDocument();
      }
      expect(fetch.mock.calls[0][0]).toBe("/v1/policies");
      view.unmount();
      expect(identityUpdates.size).toBe(0);
      client.clear();
    },
  );

  it("hides the section with a permission message when the list call is forbidden", async () => {
    // An admin whose workspace restricts global policies: the list call 403s,
    // so the section is hidden behind the same message rather than rendering
    // an empty page + Add button or a raw error.
    setPoliciesForbidden();
    const { unmount } = renderPage();
    expect(
      await screen.findByText("You don't have permission to manage global policies."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add policy" })).not.toBeInTheDocument();
    unmount();

    // Single-user mode skips the admin probe, so the 403 alone must hide the section.
    serverInfoMocks.singleUser = true;
    renderPage();
    expect(
      await screen.findByText("You don't have permission to manage global policies."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add policy" })).not.toBeInTheDocument();
  });

  it("stays loading for an unauthenticated visitor (resolveIdentity redirects)", async () => {
    // resolveIdentity returns null AND owns the login redirect, so the page
    // never resolves its admin flag — it stays in the loading state.
    vi.mocked(identity.resolveIdentity).mockResolvedValue(null);
    renderPage();
    await waitFor(() => expect(identity.getCurrentIsAdmin).not.toHaveBeenCalled());
    expect(screen.getByText("Loading...")).toBeInTheDocument();
  });
});

describe("PoliciesPage list", () => {
  it("shows the empty state when no global policies are configured", async () => {
    renderPage();
    expect(await screen.findByText(/No global policies configured/)).toBeInTheDocument();
    expect(screen.getByText("Policies applied to all sessions.")).toHaveClass("max-md:hidden");
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
    expect(screen.getByText("Disabled")).toBeInTheDocument(); // only the disabled one
    // factory_params render as a "Parameters" block.
    expect(screen.getByText("Parameters")).toBeInTheDocument();
    expect(screen.getByText("max_per_min:")).toBeInTheDocument();
  });
});

describe("PoliciesPage Settings rows", () => {
  it("renders the add and list rows, and each policy's actions, from their Settings catalog entries", async () => {
    setPolicies([policy({ id: "p1", name: "block_canada", enabled: true })]);
    renderPage();
    await screen.findByText("block_canada");

    expect(document.querySelector('[data-settings-item="policies-add"]')).not.toBeNull();
    expect(document.querySelector('[data-settings-item="policies-list"]')).not.toBeNull();
    expect(
      document.querySelector('[data-settings-region="policies-list:policies"]'),
    ).not.toBeNull();
    const card = screen.getByText("block_canada").closest("[data-settings-region-row]");
    expect(card).toBeInTheDocument();
    expect(card?.querySelector('[data-settings-row-action="toggle"]')).not.toBeNull();
    expect(card?.querySelector('[data-settings-row-action="remove"]')).not.toBeNull();
  });

  it("names the Add dialog's filter, policy name and submit from their entries", async () => {
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
    await screen.findByText(/No global policies configured/);
    fireEvent.click(screen.getByRole("button", { name: /Add policy/ }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog.getAttribute("data-settings-overlay-owner")).toBe("policies-add:add");
    expect(within(dialog).getByRole("textbox", { name: "Filter policies" })).toBeInTheDocument();
    expect(
      dialog.querySelector('[data-settings-row-action="choose"][data-settings-region-row]'),
    ).not.toBeNull();

    fireEvent.click(within(dialog).getByText("Block Canada"));
    expect(within(dialog).getByRole("textbox", { name: "Policy name" })).toHaveValue(
      "block_canada",
    );
    expect(
      within(dialog).getByRole("button", { name: "Add" }).getAttribute("data-settings-control"),
    ).toBe("policies-add:submit");
  });
});

describe("PoliciesPage actions", () => {
  it("toggling a policy's switch fires the update mutation with the new state", async () => {
    setPolicies([policy({ id: "p1", name: "block_canada", enabled: true })]);
    renderPage();

    const toggle = await screen.findByRole("switch", { name: "Toggle block_canada" });
    fireEvent.click(toggle);
    expect(updateMutate).toHaveBeenCalledWith({ policyId: "p1", enabled: false });
  });

  it("deletes a policy through the confirmation dialog", async () => {
    setPolicies([policy({ id: "p1", name: "block_canada" })]);
    renderPage();

    await screen.findByText("block_canada");
    fireEvent.click(screen.getByRole("button", { name: "Remove policy" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Remove block_canada?")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: /^Remove$/ }));

    expect(deleteMutate).toHaveBeenCalledWith("p1", expect.anything());
  });

  it("adds array (multi-select) values via the model combobox as a coerced list", async () => {
    // WHY: the expensive_models-style array param renders the single-input
    // combobox; picking options and typing a free-form value must survive the
    // comma-joined form state and coerce to a list[str] on submit — guarding
    // the checkbox→combobox refactor against a regression.
    vi.mocked(policies.usePolicyRegistry).mockReturnValue({
      data: [
        {
          handler: "omnigent.policies.budget",
          kind: "factory",
          name: "Budget Guard",
          description: "blocks expensive models",
          params_schema: {
            properties: {
              expensive_models: {
                type: "array",
                items: { type: "string", enum: ["opus", "sonnet", "haiku"] },
              },
            },
            required: [],
          },
        },
      ],
    } as never);
    renderPage();
    await screen.findByText(/No global policies configured/);

    fireEvent.click(screen.getByRole("button", { name: /Add policy/ }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByText("Budget Guard"));

    const combo = within(dialog).getByPlaceholderText("Select or type a value…");
    expect(combo).toHaveAccessibleName("Parameter values: expensive_models");
    fireEvent.focus(combo);
    fireEvent.mouseDown(within(dialog).getByRole("button", { name: "opus" }));
    fireEvent.mouseDown(within(dialog).getByRole("button", { name: "haiku" }));
    // Free-form typed value still works (Enter commits).
    fireEvent.change(combo, { target: { value: "custom-tier" } });
    fireEvent.keyDown(combo, { key: "Enter" });

    fireEvent.click(within(dialog).getByRole("button", { name: /^Add$/ }));

    expect(addMutate).toHaveBeenCalledTimes(1);
    const payload = addMutate.mock.calls[0][0];
    expect(payload.handler).toBe("omnigent.policies.budget");
    expect(payload.factory_params).toEqual({
      expensive_models: ["opus", "haiku", "custom-tier"],
    });
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
    await screen.findByText(/No global policies configured/);

    fireEvent.click(screen.getByRole("button", { name: /Add policy/ }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByText("Block Canada")); // select the registry entry
    fireEvent.click(within(dialog).getByRole("button", { name: /^Add$/ }));

    expect(addMutate).toHaveBeenCalledWith(
      { name: "block_canada", type: "python", handler: "omnigent.policies.block_canada" },
      expect.anything(),
    );
  });

  it("Cancel steps back to the policy list after a policy is selected", async () => {
    // WHY: once a policy is selected the dialog shows its config; Cancel must
    // return to the list so the user can pick a different policy (not close).
    vi.mocked(policies.usePolicyRegistry).mockReturnValue({
      data: [
        {
          handler: "omnigent.policies.block_canada",
          kind: "callable",
          name: "Block Canada",
          description: "Deny anything mentioning Canada.",
          params_schema: null,
        },
        {
          handler: "omnigent.policies.rate_limit",
          kind: "callable",
          name: "Rate Limit",
          description: "Cap request rate.",
          params_schema: null,
        },
      ],
    } as never);
    renderPage();
    await screen.findByText(/No global policies configured/);

    fireEvent.click(screen.getByRole("button", { name: /Add policy/ }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByText("Block Canada"));
    expect(within(dialog).queryByPlaceholderText("Filter policies...")).toBeNull();

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(within(dialog).getByPlaceholderText("Filter policies...")).toBeInTheDocument();
    expect(within(dialog).getByText("Block Canada")).toBeInTheDocument();
    expect(within(dialog).getByText("Rate Limit")).toBeInTheDocument();
    expect(addMutate).not.toHaveBeenCalled();
  });

  it("Cancel from the policy list closes the dialog", async () => {
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
    await screen.findByText(/No global policies configured/);

    fireEvent.click(screen.getByRole("button", { name: /Add policy/ }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});

describe("PoliciesPage single-user mode", () => {
  beforeEach(() => {
    // Explicit single-user local runtime (single_user marker): no auth
    // endpoints, so the admin probe is skipped and the page renders directly.
    serverInfoMocks.accountsEnabled = false;
    serverInfoMocks.loginUrl = null;
    serverInfoMocks.serverVersion = "0.3.0.dev0";
    serverInfoMocks.singleUser = true;
  });

  it("shows the full page without the admin gate (empty state)", async () => {
    renderPage();
    expect(await screen.findByText(/No global policies configured/)).toBeInTheDocument();
    expect(screen.queryByText("Loading...")).not.toBeInTheDocument();
    expect(
      screen.queryByText("You don't have permission to manage global policies."),
    ).not.toBeInTheDocument();
    expect(defaultPolicies.useDefaultPolicies).toHaveBeenLastCalledWith(true);
  });

  it("shows policies list directly without probing identity", async () => {
    setPolicies([policy({ id: "p1", name: "block_canada", enabled: true })]);
    renderPage();
    expect(await screen.findByText("block_canada")).toBeInTheDocument();
    expect(identity.resolveIdentity).not.toHaveBeenCalled();
    expect(defaultPolicies.useDefaultPolicies).toHaveBeenLastCalledWith(true);
  });
});

describe("PoliciesPage on a multi-user header-auth deploy", () => {
  beforeEach(() => {
    // Header-auth multi-user (SSO proxy): accounts off AND no login_url, same
    // shape as single-user, but single_user is false — so the admin gate must
    // still run (probe identity, 403 non-admins). This is the regression the
    // single_user signal fixes: before, this deploy skipped the gate entirely.
    serverInfoMocks.accountsEnabled = false;
    serverInfoMocks.loginUrl = null;
    serverInfoMocks.serverVersion = "0.3.0.dev0";
    serverInfoMocks.singleUser = false;
  });

  it("probes identity and gates non-admins", async () => {
    vi.mocked(identity.getCurrentIsAdmin).mockReturnValue(false);
    renderPage();
    expect(
      await screen.findByText("You don't have permission to manage global policies."),
    ).toBeInTheDocument();
    expect(identity.resolveIdentity).toHaveBeenCalled();
    expect(defaultPolicies.useDefaultPolicies).toHaveBeenLastCalledWith(false);
  });
});
