// Tests for the admin SharingPage (server-wide sharing-settings picker).
//
// Browser e2e is impractical (admin-gated), so the surface is pinned here by
// mocking the mode-agnostic identity probe (resolveIdentity / getCurrentIsAdmin
// gate admin) and the react-query sharing hooks, so no QueryClient or
// network is needed.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SharingPage } from "./SharingPage";
import { TooltipProvider } from "@/components/ui/tooltip";
import * as identity from "@/lib/identity";
import * as sharingHook from "@/hooks/useSharing";
import type { SharingState } from "@/hooks/useSharing";

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
    default_public_sessions: "off",
    default_public_sessions_editable: true,
    default_public_sessions_options: ["off", "sandbox", "all"],
    ...overrides,
  };
}

/** Radios of one fieldset, so the tier and default-public groups stay separate. */
function radiosIn(group: string): HTMLInputElement[] {
  return within(screen.getByRole("group", { name: group })).getAllByRole(
    "radio",
  ) as HTMLInputElement[];
}

function renderPage() {
  return render(
    <TooltipProvider>
      <SharingPage />
    </TooltipProvider>,
  );
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
  serverInfoMocks.singleUser = false;
});

afterEach(cleanup);

describe("SharingPage", () => {
  it("shows all four tiers with the current one selected (admin)", async () => {
    setSharingState(state({ sharing_mode: "read_only" }));

    renderPage();

    await waitFor(() => expect(screen.getByText("On")).toBeInTheDocument());
    expect(screen.getByText("Read only")).toBeInTheDocument();
    expect(screen.getByText("Read only (restricted)")).toBeInTheDocument();
    expect(screen.getByText("Off")).toBeInTheDocument();

    // The current tier's radio is checked; a different one is not.
    const radios = radiosIn("Session sharing mode");
    expect(radios).toHaveLength(4);
    const readOnly = radios.find((r) => r.value === "read_only")!;
    const off = radios.find((r) => r.value === "off")!;
    expect(readOnly.checked).toBe(true);
    expect(off.checked).toBe(false);
  });

  it("calls the mutation with the chosen tier", async () => {
    setSharingState(state({ sharing_mode: "on" }));

    renderPage();
    await waitFor(() => expect(screen.getByText("On")).toBeInTheDocument());

    const restricted = radiosIn("Session sharing mode").find(
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

    renderPage();
    await waitFor(() =>
      expect(
        screen.getByText(/managed by this deployment and can't be changed here/i),
      ).toBeInTheDocument(),
    );

    // Radios are disabled; clicking does nothing.
    const radios = radiosIn("Session sharing mode");
    expect(radios.every((r) => r.disabled)).toBe(true);
    fireEvent.click(radios.find((r) => r.value === "off")!);
    expect(setModeMutate).not.toHaveBeenCalled();
  });

  it("shows a no-permission message to a non-admin", async () => {
    vi.mocked(identity.getCurrentIsAdmin).mockReturnValue(false);
    setSharingState(state());

    renderPage();

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

      renderPage();
      await waitFor(() => expect(screen.getByText("On")).toBeInTheDocument());

      const toggle = screen.getByRole("switch", { name: /public access/i });
      expect(toggle).toBeEnabled();
      expect(toggle).toBeChecked();
    });

    it("toggling the switch calls the mutation with public_sharing", async () => {
      setSharingState(state({ public_sharing_enabled: true, public_sharing_editable: true }));

      renderPage();
      await waitFor(() => expect(screen.getByText("On")).toBeInTheDocument());

      fireEvent.click(screen.getByRole("switch", { name: /public access/i }));

      expect(setModeMutate).toHaveBeenCalledWith({ public_sharing: false }, expect.anything());
    });

    it("disables the switch (no mutation) when public access is deployment-managed", async () => {
      setSharingState(state({ public_sharing_enabled: true, public_sharing_editable: false }));

      renderPage();
      await waitFor(() => expect(screen.getByText("On")).toBeInTheDocument());

      const toggle = screen.getByRole("switch", { name: /public access/i });
      expect(toggle).toBeDisabled();
      fireEvent.click(toggle);
      expect(setModeMutate).not.toHaveBeenCalled();
    });
  });

  describe("default visibility for new sessions", () => {
    const GROUP = "Default visibility for new sessions";

    it("shows the three options with the current one selected", async () => {
      setSharingState(state({ default_public_sessions: "sandbox" }));

      renderPage();
      await waitFor(() => expect(screen.getByText("On")).toBeInTheDocument());

      const radios = radiosIn(GROUP);
      expect(radios.map((r) => r.value)).toEqual(["off", "sandbox", "all"]);
      expect(radios.find((r) => r.value === "sandbox")!.checked).toBe(true);
      expect(screen.queryByText(/has no effect/i)).not.toBeInTheDocument();
    });

    it("calls the mutation with the chosen default", async () => {
      setSharingState(state());

      renderPage();
      await waitFor(() => expect(screen.getByText("On")).toBeInTheDocument());

      fireEvent.click(radiosIn(GROUP).find((r) => r.value === "all")!);

      expect(setModeMutate).toHaveBeenCalledWith(
        { default_public_sessions: "all" },
        expect.anything(),
      );
    });

    it("is disabled when deployment-managed", async () => {
      setSharingState(state({ default_public_sessions_editable: false }));

      renderPage();
      await waitFor(() => expect(screen.getByText("On")).toBeInTheDocument());

      const radios = radiosIn(GROUP);
      expect(radios.every((r) => r.disabled)).toBe(true);
      fireEvent.click(radios.find((r) => r.value === "all")!);
      expect(setModeMutate).not.toHaveBeenCalled();
    });

    it("is greyed out and shows Private while public access is off", async () => {
      setSharingState(state({ public_sharing_enabled: false, default_public_sessions: "all" }));

      renderPage();
      await waitFor(() => expect(screen.getByText("On")).toBeInTheDocument());

      const radios = radiosIn(GROUP);
      expect(radios.every((r) => r.disabled)).toBe(true);
      // Effective value, not the saved "all", which returns once public access is on.
      expect(radios.find((r) => r.value === "off")!.checked).toBe(true);
      expect(
        screen.getByText("Turn on public access to change the default visibility."),
      ).toBeInTheDocument();
      fireEvent.click(radios.find((r) => r.value === "sandbox")!);
      expect(setModeMutate).not.toHaveBeenCalled();
    });

    it("shows the saved choice again once public access is on", async () => {
      setSharingState(state({ public_sharing_enabled: true, default_public_sessions: "all" }));

      renderPage();
      await waitFor(() => expect(screen.getByText("On")).toBeInTheDocument());

      const radios = radiosIn(GROUP);
      expect(radios.every((r) => !r.disabled)).toBe(true);
      expect(radios.find((r) => r.value === "all")!.checked).toBe(true);
      expect(screen.queryByText(/to change the default visibility/)).not.toBeInTheDocument();
    });
  });

  describe("sharing off", () => {
    it("greys out public access and default visibility", async () => {
      setSharingState(
        state({
          sharing_mode: "off",
          public_sharing_enabled: true,
          default_public_sessions: "all",
        }),
      );

      renderPage();
      await waitFor(() => expect(screen.getByText("Off")).toBeInTheDocument());

      const toggle = screen.getByRole("switch", { name: /public access/i });
      expect(toggle).toBeDisabled();
      expect(toggle).not.toBeChecked();
      expect(screen.getByText("Turn sharing on to use public access.")).toBeInTheDocument();
      const radios = radiosIn("Default visibility for new sessions");
      expect(radios.every((r) => r.disabled)).toBe(true);
      expect(radios.find((r) => r.value === "off")!.checked).toBe(true);
      fireEvent.click(toggle);
      expect(setModeMutate).not.toHaveBeenCalled();
    });
  });
});
