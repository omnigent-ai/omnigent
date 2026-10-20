// Tests for the admin SharingPage (server-wide sharing-settings picker).
//
// Mock identity and sharing hooks to cover admin gating and disabled controls.

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
    public_sharing_max_level: "read",
    public_sharing_max_level_editable: true,
    public_sharing_max_level_options: ["read", "edit"],
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
  it("updates the public ceiling independently of the sharing mode", async () => {
    setSharingState(state());
    renderPage();
    const trigger = await screen.findByRole("combobox", { name: "Maximum public permission" });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Edit" }));
    expect(setModeMutate).toHaveBeenCalledWith(
      { public_sharing_max_level: "edit" },
      expect.anything(),
    );
  });

  it("disables a deployment-managed public ceiling", async () => {
    setSharingState(state({ public_sharing_max_level_editable: false }));
    renderPage();
    expect(
      await screen.findByRole("combobox", { name: "Maximum public permission" }),
    ).toBeDisabled();
  });

  it("uses titled outlined groups and mobile description tooltips", async () => {
    setSharingState(state());

    renderPage();
    await waitFor(() => expect(screen.getByText("On")).toBeInTheDocument());

    expect(screen.getByText(/Control whether users on this server can share sessions/)).toHaveClass(
      "max-md:hidden",
    );
    for (const testId of ["settings-group-sharing-mode", "settings-group-public-sharing"]) {
      expect(screen.getByTestId(testId).lastElementChild).toHaveClass(
        "rounded-xl",
        "border",
        "border-border",
        "bg-card",
      );
    }

    expect(screen.getByText(/Allow sharing a session with anyone who has the link/)).toHaveClass(
      "max-md:hidden",
    );
    const help = screen.getByRole("button", { name: "About Public access" });
    expect(help).toHaveClass("md:hidden");
    fireEvent.click(help);
    expect(await screen.findByRole("tooltip")).toHaveTextContent(
      /Allow sharing a session with anyone who has the link/,
    );
  });

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

  describe("default sharing for new sessions", () => {
    const TOGGLE = "Share new sessions by default";
    const GROUP = "Automatically share";

    it("shows only the scope options when automatic sharing is enabled", async () => {
      setSharingState(state({ default_public_sessions: "sandbox" }));

      renderPage();
      expect(await screen.findByRole("switch", { name: TOGGLE })).toBeChecked();

      const radios = radiosIn(GROUP);
      expect(radios.map((r) => r.value)).toEqual(["sandbox", "all"]);
      expect(radios.find((r) => r.value === "sandbox")!.checked).toBe(true);
      expect(screen.getByRole("radio", { name: "Managed hosts only" })).toBeChecked();
      expect(screen.getByText(/Sessions on local hosts start private/)).toBeInTheDocument();
    });

    it("hides scope while off and enables managed hosts only by default", async () => {
      setSharingState(state());

      renderPage();
      const toggle = await screen.findByRole("switch", { name: TOGGLE });
      expect(toggle).not.toBeChecked();
      expect(screen.queryByRole("group", { name: GROUP })).not.toBeInTheDocument();
      fireEvent.click(toggle);

      expect(setModeMutate).toHaveBeenCalledWith(
        { default_public_sessions: "sandbox" },
        expect.anything(),
      );
    });

    it("calls the mutation with the chosen scope", async () => {
      setSharingState(state({ default_public_sessions: "sandbox" }));

      renderPage();
      await screen.findByRole("switch", { name: TOGGLE });

      fireEvent.click(radiosIn(GROUP).find((r) => r.value === "all")!);

      expect(setModeMutate).toHaveBeenCalledWith(
        { default_public_sessions: "all" },
        expect.anything(),
      );
    });

    it("turns automatic sharing off with the parent switch", async () => {
      setSharingState(state({ default_public_sessions: "all" }));

      renderPage();
      fireEvent.click(await screen.findByRole("switch", { name: TOGGLE }));

      expect(setModeMutate).toHaveBeenCalledWith(
        { default_public_sessions: "off" },
        expect.anything(),
      );
    });

    it("is disabled when deployment-managed", async () => {
      setSharingState(
        state({ default_public_sessions: "sandbox", default_public_sessions_editable: false }),
      );

      renderPage();
      const toggle = await screen.findByRole("switch", { name: TOGGLE });
      expect(toggle).toBeDisabled();

      const radios = radiosIn(GROUP);
      expect(radios.every((r) => r.disabled)).toBe(true);
      fireEvent.click(toggle);
      fireEvent.click(radios.find((r) => r.value === "all")!);
      expect(setModeMutate).not.toHaveBeenCalled();
    });

    it("disables both controls while a save is pending", async () => {
      setSharingState(state({ default_public_sessions: "sandbox" }));
      vi.mocked(sharingHook.useSetSharing).mockReturnValue({
        mutate: setModeMutate,
        isPending: true,
      } as unknown as ReturnType<typeof sharingHook.useSetSharing>);

      renderPage();
      expect(await screen.findByRole("switch", { name: TOGGLE })).toBeDisabled();
      expect(radiosIn(GROUP).every((r) => r.disabled)).toBe(true);
    });

    it("keeps the saved scope and shows an error when saving fails", async () => {
      setSharingState(state({ default_public_sessions: "sandbox" }));
      setModeMutate.mockImplementation((_update, { onError }) => onError(new Error("Save failed")));

      renderPage();
      await screen.findByRole("switch", { name: TOGGLE });
      fireEvent.click(screen.getByRole("radio", { name: "All sessions" }));

      expect(screen.getByText("Save failed")).toBeInTheDocument();
      expect(screen.getByRole("radio", { name: "Managed hosts only" })).toBeChecked();
    });

    it("is off and hides scope while public access is off", async () => {
      setSharingState(state({ public_sharing_enabled: false, default_public_sessions: "all" }));

      const view = renderPage();
      const toggle = await screen.findByRole("switch", { name: TOGGLE });

      expect(toggle).toBeDisabled();
      expect(toggle).not.toBeChecked();
      expect(screen.queryByRole("group", { name: GROUP })).not.toBeInTheDocument();
      expect(
        screen.getByText("Turn on public access to change the default visibility."),
      ).toBeInTheDocument();
      fireEvent.click(toggle);
      expect(setModeMutate).not.toHaveBeenCalled();

      setSharingState(state({ public_sharing_enabled: true, default_public_sessions: "all" }));
      view.rerender(
        <TooltipProvider>
          <SharingPage />
        </TooltipProvider>,
      );

      const radios = radiosIn(GROUP);
      const restoredToggle = screen.getByRole("switch", { name: TOGGLE });
      expect(restoredToggle).toBeEnabled();
      expect(restoredToggle).toBeChecked();
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
      const defaultToggle = screen.getByRole("switch", { name: "Share new sessions by default" });
      expect(defaultToggle).toBeDisabled();
      expect(defaultToggle).not.toBeChecked();
      expect(screen.queryByRole("group", { name: "Automatically share" })).not.toBeInTheDocument();
      fireEvent.click(toggle);
      fireEvent.click(defaultToggle);
      expect(setModeMutate).not.toHaveBeenCalled();
    });
  });

  describe("waits for identity before loading settings", () => {
    const enabledArgs = () =>
      vi.mocked(sharingHook.useSharing).mock.calls.map((c) => c[0]?.enabled);

    beforeEach(() => vi.mocked(sharingHook.useSharing).mockClear());

    it("does not load while /v1/me is pending, then loads for an admin", async () => {
      let resolveMe: (id: string) => void = () => {};
      vi.mocked(identity.resolveIdentity).mockReturnValue(
        new Promise<string>((r) => {
          resolveMe = r;
        }),
      );
      setSharingState(state());

      renderPage();
      expect(enabledArgs()).toEqual(expect.arrayContaining([false]));
      expect(enabledArgs()).not.toContain(true);

      resolveMe("admin@example.com");
      await waitFor(() => expect(enabledArgs()).toContain(true));
    });

    it("never loads for a non-admin", async () => {
      vi.mocked(identity.getCurrentIsAdmin).mockReturnValue(false);
      setSharingState(undefined);

      renderPage();
      await waitFor(() =>
        expect(
          screen.getByText("You don't have permission to manage session sharing."),
        ).toBeInTheDocument(),
      );
      expect(enabledArgs().every((enabled) => enabled === false)).toBe(true);
    });
  });
});
