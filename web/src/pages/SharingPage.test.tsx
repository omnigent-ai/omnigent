// Tests for the admin SharingPage (server-wide sharing-settings picker).
//
// Browser e2e is impractical (admin-gated), so the surface is pinned here by
// mocking the mode-agnostic identity probe (resolveIdentity / getCurrentIsAdmin
// gate admin) and the react-query sharing hooks, so no QueryClient or
// network is needed.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SharingPage } from "./SharingPage";
import * as identity from "@/lib/identity";
import * as sharingHook from "@/hooks/useSharing";
import type { SharingState } from "@/hooks/useSharing";

const serverInfoMocks = vi.hoisted(() => ({
  accountsEnabled: true,
  loginUrl: null as string | null,
  serverVersion: "0.3.0.dev0" as string | null,
}));

vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({
    accounts_enabled: serverInfoMocks.accountsEnabled,
    login_url: serverInfoMocks.loginUrl,
    server_version: serverInfoMocks.serverVersion,
  }),
}));

vi.mock("@/lib/identity", () => ({
  resolveIdentity: vi.fn(),
  getCurrentIsAdmin: vi.fn(),
}));
vi.mock("@/hooks/useSharing", () => ({
  useSharing: vi.fn(),
  useSetSharing: vi.fn(),
}));

const setModeMutate = vi.fn();

function state(overrides: Partial<SharingState> = {}): SharingState {
  return {
    object: "sharing",
    sharing_mode: "on",
    editable: true,
    options: ["on", "read_only", "restricted_read_only", "off"],
    public_sharing_enabled: true,
    public_sharing_editable: true,
    ...overrides,
  };
}

function setSharingState(s: SharingState | undefined, isLoading = false) {
  vi.mocked(sharingHook.useSharing).mockReturnValue({
    data: s,
    isLoading,
  } as unknown as ReturnType<typeof sharingHook.useSharing>);
}

beforeEach(() => {
  vi.mocked(identity.resolveIdentity).mockResolvedValue("admin@example.com");
  vi.mocked(identity.getCurrentIsAdmin).mockReturnValue(true);
  setModeMutate.mockReset();
  vi.mocked(sharingHook.useSetSharing).mockReturnValue({
    mutate: setModeMutate,
    isPending: false,
  } as unknown as ReturnType<typeof sharingHook.useSetSharing>);
  serverInfoMocks.accountsEnabled = true;
  serverInfoMocks.loginUrl = null;
  serverInfoMocks.serverVersion = "0.3.0.dev0";
});

afterEach(cleanup);

describe("SharingPage", () => {
  it("shows all four tiers with the current one selected (admin)", async () => {
    setSharingState(state({ sharing_mode: "read_only" }));

    render(<SharingPage />);

    await waitFor(() => expect(screen.getByText("On")).toBeInTheDocument());
    expect(screen.getByText("Read only")).toBeInTheDocument();
    expect(screen.getByText("Read only (restricted)")).toBeInTheDocument();
    expect(screen.getByText("Off")).toBeInTheDocument();

    // The current tier's radio is checked; a different one is not.
    const radios = screen.getAllByRole("radio") as HTMLInputElement[];
    expect(radios).toHaveLength(4);
    const readOnly = radios.find((r) => r.value === "read_only")!;
    const off = radios.find((r) => r.value === "off")!;
    expect(readOnly.checked).toBe(true);
    expect(off.checked).toBe(false);
  });

  it("calls the mutation with the chosen tier", async () => {
    setSharingState(state({ sharing_mode: "on" }));

    render(<SharingPage />);
    await waitFor(() => expect(screen.getByText("On")).toBeInTheDocument());

    const restricted = (screen.getAllByRole("radio") as HTMLInputElement[]).find(
      (r) => r.value === "restricted_read_only",
    )!;
    fireEvent.click(restricted);

    expect(setModeMutate).toHaveBeenCalledWith(
      { sharing_mode: "restricted_read_only" },
      expect.anything(),
    );
  });

  it("is read-only with a notice when the deployment manages the mode", async () => {
    setSharingState(state({ editable: false }));

    render(<SharingPage />);
    await waitFor(() =>
      expect(
        screen.getByText(/managed by this deployment and can't be changed here/i),
      ).toBeInTheDocument(),
    );

    // Radios are disabled; clicking does nothing.
    const radios = screen.getAllByRole("radio") as HTMLInputElement[];
    expect(radios.every((r) => r.disabled)).toBe(true);
    fireEvent.click(radios.find((r) => r.value === "off")!);
    expect(setModeMutate).not.toHaveBeenCalled();
  });

  it("shows a no-permission message to a non-admin", async () => {
    vi.mocked(identity.getCurrentIsAdmin).mockReturnValue(false);
    setSharingState(state());

    render(<SharingPage />);

    await waitFor(() =>
      expect(
        screen.getByText("You don't have permission to manage session sharing."),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
  });

  describe("public access toggle", () => {
    it("renders an enabled, checked switch when public sharing is on and editable", async () => {
      setSharingState(state({ public_sharing_enabled: true, public_sharing_editable: true }));

      render(<SharingPage />);
      await waitFor(() => expect(screen.getByText("On")).toBeInTheDocument());

      const toggle = screen.getByRole("switch", { name: /public access/i });
      expect(toggle).toBeEnabled();
      expect(toggle).toBeChecked();
    });

    it("toggling the switch calls the mutation with public_sharing", async () => {
      setSharingState(state({ public_sharing_enabled: true, public_sharing_editable: true }));

      render(<SharingPage />);
      await waitFor(() => expect(screen.getByText("On")).toBeInTheDocument());

      fireEvent.click(screen.getByRole("switch", { name: /public access/i }));

      expect(setModeMutate).toHaveBeenCalledWith({ public_sharing: false }, expect.anything());
    });

    it("disables the switch (no mutation) when public access is deployment-managed", async () => {
      setSharingState(state({ public_sharing_enabled: true, public_sharing_editable: false }));

      render(<SharingPage />);
      await waitFor(() => expect(screen.getByText("On")).toBeInTheDocument());

      const toggle = screen.getByRole("switch", { name: /public access/i });
      expect(toggle).toBeDisabled();
      fireEvent.click(toggle);
      expect(setModeMutate).not.toHaveBeenCalled();
    });
  });
});
