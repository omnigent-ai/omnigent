import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { ServerInfo, SharingMode } from "@/lib/capabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { PermissionsModal } from "./PermissionsModal";

vi.mock("@/lib/permissionsApi", () => ({
  listPermissions: vi.fn(),
  grantPermission: vi.fn(),
  revokePermission: vi.fn(),
}));

// Stub the QR renderer so tests can assert the encoded deep-link value via a
// data attribute instead of decoding SVG paths. Renders a bare element with no
// text content so it can't accidentally match unrelated text-based assertions.
vi.mock("qrcode.react", () => ({
  QRCodeSVG: (props: { value: string; "aria-label"?: string }) => (
    <svg data-testid="share-qr-code" data-value={props.value} aria-label={props["aria-label"]} />
  ),
}));

// Host config is read-once at render to decide plain-input vs combobox and to
// transform the share link. Mock both getters so we can drive each branch.
vi.mock("@/lib/host", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/host")>();
  return {
    ...actual,
    getOmnigentUserSearch: vi.fn(() => undefined),
    getOmnigentTransformShareLink: vi.fn(() => undefined),
  };
});

import * as api from "@/lib/permissionsApi";
import * as host from "@/lib/host";
const listMock = vi.mocked(api.listPermissions);
const grantMock = vi.mocked(api.grantPermission);
const revokeMock = vi.mocked(api.revokePermission);
const userSearchMock = vi.mocked(host.getOmnigentUserSearch);
const transformLinkMock = vi.mocked(host.getOmnigentTransformShareLink);

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

/** Full OSS ServerInfo with permissive defaults; override per test. */
function serverInfo(overrides: Partial<ServerInfo> = {}): ServerInfo {
  return {
    accounts_enabled: false,
    single_user: false,
    login_url: null,
    needs_setup: false,
    databricks_features: false,
    managed_sandboxes_enabled: false,
    sandbox_provider: null,
    sharing_mode: "on",
    public_sharing_enabled: true,
    server_version: null,
    smart_routing_enabled: false,
    harness_install_enabled: false,
    installable_harnesses: [],
    dictation_available: false,
    ...overrides,
  };
}

/** Wrapper that pins arbitrary ServerInfo overrides via CapabilitiesProvider. */
function createInfoWrapper(overrides: Partial<ServerInfo>) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return (
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <CapabilitiesProvider info={serverInfo(overrides)}>{children}</CapabilitiesProvider>
        </TooltipProvider>
      </QueryClientProvider>
    );
  };
}

/** Wrapper that pins the server's sharing policy via CapabilitiesProvider. */
function createSharingWrapper(mode: SharingMode) {
  return createInfoWrapper({ sharing_mode: mode });
}

beforeEach(() => {
  listMock.mockReset();
  grantMock.mockReset();
  revokeMock.mockReset();
  // Default: standalone (no host providers). Combobox/transform tests opt in.
  userSearchMock.mockReturnValue(undefined);
  transformLinkMock.mockReturnValue(undefined);
});

afterEach(cleanup);

describe("PermissionsModal", () => {
  it("fetches and displays grants when opened", async () => {
    listMock.mockResolvedValue([
      { user_id: "alice@example.com", conversation_id: "conv_abc", level: 3 },
      { user_id: "bob@example.com", conversation_id: "conv_abc", level: 1 },
    ]);

    render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(screen.getByText("alice@example.com")).toBeInTheDocument();
      expect(screen.getByText("bob@example.com")).toBeInTheDocument();
    });
    expect(listMock).toHaveBeenCalledWith("conv_abc");
  });

  it("calls grantPermission with the form values on submit", async () => {
    listMock.mockResolvedValue([]);
    grantMock.mockResolvedValue({ user_id: "carol", conversation_id: "conv_abc", level: 2 });

    render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(listMock).toHaveBeenCalled());

    const input = screen.getByPlaceholderText("alice@example.com");
    fireEvent.change(input, { target: { value: "carol@example.com" } });

    const grantBtn = screen.getByRole("button", { name: /grant/i });
    fireEvent.click(grantBtn);

    await waitFor(() => {
      expect(grantMock).toHaveBeenCalledWith("conv_abc", "carol@example.com", 1);
    });
  });

  it("calls revokePermission when the revoke button is clicked", async () => {
    listMock.mockResolvedValue([
      { user_id: "bob@example.com", conversation_id: "conv_abc", level: 1 },
    ]);
    revokeMock.mockResolvedValue(undefined);

    render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(screen.getByText("bob@example.com")).toBeInTheDocument());

    const revokeBtn = screen.getByRole("button", { name: /revoke/i });
    fireEvent.click(revokeBtn);

    await waitFor(() => {
      expect(revokeMock).toHaveBeenCalledWith("conv_abc", "bob@example.com");
    });
  });

  it("updates a grant's level inline via grantPermission (no revoke + re-add)", async () => {
    listMock.mockResolvedValue([
      { user_id: "bob@example.com", conversation_id: "conv_abc", level: 1 },
    ]);
    grantMock.mockResolvedValue({
      user_id: "bob@example.com",
      conversation_id: "conv_abc",
      level: 2,
    });

    render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(screen.getByText("bob@example.com")).toBeInTheDocument());

    // The row's permission dropdown shows the current level ("Read") as a
    // combobox; the grant form's level select also shows "Read", so disambiguate
    // by picking the combobox inside bob's row.
    const rowTrigger = screen.getAllByRole("combobox").find((el) => el.textContent === "Read")!;
    rowTrigger.focus();
    fireEvent.keyDown(rowTrigger, { key: "Enter" });
    fireEvent.click(await screen.findByRole("option", { name: "Edit" }));

    await waitFor(() => {
      expect(grantMock).toHaveBeenCalledWith("conv_abc", "bob@example.com", 2);
    });
    // Editing the level must never delete the existing grant.
    expect(revokeMock).not.toHaveBeenCalled();
  });

  it("renders the owner as non-editable with no revoke control", async () => {
    listMock.mockResolvedValue([
      { user_id: "owner@example.com", conversation_id: "conv_abc", level: 4 },
      { user_id: "bob@example.com", conversation_id: "conv_abc", level: 1 },
    ]);

    render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(screen.getByText("owner@example.com")).toBeInTheDocument());

    // Owner level is fixed text, not a dropdown, and exposes no revoke button.
    expect(screen.getByText("Owner")).toBeInTheDocument();
    const revokeButtons = screen.queryAllByRole("button", { name: /revoke/i });
    expect(revokeButtons).toHaveLength(1); // only bob's row is revocable
    // Exactly one editable permission dropdown (bob); owner has none.
    expect(screen.getAllByRole("combobox")).toHaveLength(2); // bob's row + grant form
  });

  it("toggles public access via grant/revoke of __public__ sentinel", async () => {
    listMock.mockResolvedValue([]);
    grantMock.mockResolvedValue({ user_id: "__public__", conversation_id: "conv_abc", level: 1 });

    render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(listMock).toHaveBeenCalled());

    const toggle = screen.getByRole("switch");
    fireEvent.click(toggle);

    await waitFor(() => {
      expect(grantMock).toHaveBeenCalledWith("conv_abc", "__public__", 1);
    });
  });

  it("displays server error messages from failed grant", async () => {
    listMock.mockResolvedValue([]);
    grantMock.mockRejectedValue(new Error("'rice' needs manage permission"));

    render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(listMock).toHaveBeenCalled());

    const input = screen.getByPlaceholderText("alice@example.com");
    fireEvent.change(input, { target: { value: "rice" } });
    fireEvent.click(screen.getByRole("button", { name: /grant/i }));

    await waitFor(() => {
      expect(screen.getByText("'rice' needs manage permission")).toBeInTheDocument();
    });
  });

  // Regression guard: granting manage (level 3) is backend-only. No level
  // dropdown in the modal may ever offer "Manage" — not the add-grant form
  // and not the per-row level select, regardless of the viewer (owners and
  // managers see the same modal).
  it("never offers Manage in any level dropdown", async () => {
    listMock.mockResolvedValue([
      { user_id: "owner@example.com", conversation_id: "conv_abc", level: 4 },
      { user_id: "mallory@example.com", conversation_id: "conv_abc", level: 3 },
      { user_id: "bob@example.com", conversation_id: "conv_abc", level: 1 },
    ]);

    render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(screen.getByText("mallory@example.com")).toBeInTheDocument());

    // Exactly two dropdowns exist: bob's row + the add-grant form. If this
    // count is 3, the pre-existing manage grant regressed from a fixed label
    // back to an editable (and thus Manage-bearing) select.
    const triggers = screen.getAllByRole("combobox");
    expect(triggers).toHaveLength(2);
    // The manage grant's level is still visible to the viewer as static text.
    expect(screen.getByText("Manage")).toBeInTheDocument();

    for (const trigger of triggers) {
      trigger.focus();
      fireEvent.keyDown(trigger, { key: "Enter" });
      const listbox = await screen.findByRole("listbox");
      // The full option list is exactly Read and Edit. A "Manage" entry here
      // means the UI re-exposed grantable manage; any other extra entry means
      // a new level was added without deciding whether it's grantable.
      const options = within(listbox).getAllByRole("option");
      expect(options.map((o) => o.textContent)).toEqual(["Read", "Edit"]);
      fireEvent.keyDown(listbox, { key: "Escape" });
      await waitFor(() => expect(screen.queryByRole("listbox")).not.toBeInTheDocument());
    }
  });

  it("does not fetch permissions when closed", () => {
    render(<PermissionsModal sessionId="conv_abc" open={false} onOpenChange={() => {}} />, {
      wrapper: createWrapper(),
    });
    expect(listMock).not.toHaveBeenCalled();
  });

  // Regression: the copy-link button used to copy window.location.href, so
  // sharing from the sidebar 3-dot menu always produced a link to whatever
  // conversation was currently open instead of the one being shared.
  it("copies a link to the shared conversation, not the currently open one", async () => {
    listMock.mockResolvedValue([]);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const originalLocation = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        ...originalLocation,
        origin: "https://app.example.com",
        href: "https://app.example.com/c/conv_currently_open",
      },
    });

    try {
      render(
        <PermissionsModal sessionId="conv_being_shared" open={true} onOpenChange={() => {}} />,
        { wrapper: createWrapper() },
      );

      await waitFor(() => expect(listMock).toHaveBeenCalled());

      fireEvent.click(screen.getByRole("button", { name: /copy link/i }));

      await waitFor(() => {
        expect(writeText).toHaveBeenCalledWith("https://app.example.com/c/conv_being_shared");
      });
    } finally {
      Object.defineProperty(window, "location", { configurable: true, value: originalLocation });
    }
  });

  it("uses the host transformShareLink when one is installed", () => {
    // WHY: in the embed the host returns the full absolute URL; the modal must
    // defer to that transform instead of prepending window.location.origin.
    listMock.mockResolvedValue([]);
    transformLinkMock.mockReturnValue((path: string) => `https://host.example.com/embed#${path}`);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });

    render(<PermissionsModal sessionId="conv_xyz" open={true} onOpenChange={() => {}} />, {
      wrapper: createWrapper(),
    });

    fireEvent.click(screen.getByRole("button", { name: /copy link/i }));

    return waitFor(() => {
      expect(writeText).toHaveBeenCalledWith("https://host.example.com/embed#/c/conv_xyz");
    });
  });

  it("surfaces a server error from a failed revoke", async () => {
    // WHY: revoke failures (e.g. insufficient permission) must render the
    // server message via the onError path, mirroring the grant error path.
    listMock.mockResolvedValue([
      { user_id: "bob@example.com", conversation_id: "conv_abc", level: 1 },
    ]);
    revokeMock.mockRejectedValue(new Error("cannot revoke last owner"));

    render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(screen.getByText("bob@example.com")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /revoke/i }));

    await waitFor(() => {
      expect(screen.getByText("cannot revoke last owner")).toBeInTheDocument();
    });
  });

  // Overflow regression: DialogContent is a CSS grid, and a long nowrap email
  // used to set the grants section's min-content, pushing every row past the
  // dialog edge. jsdom does no layout, so these tests pin the DOM contract the
  // CSS fix relies on: min-w-0 on the grants grid item, and a truncating row
  // label that keeps the full id reachable via its title tooltip.
  describe("long user id rendering", () => {
    it("caps the grants section's grid min-content (min-w-0)", async () => {
      listMock.mockResolvedValue([
        { user_id: "bob@example.com", conversation_id: "conv_abc", level: 1 },
      ]);

      render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
        wrapper: createWrapper(),
      });

      await waitFor(() => expect(screen.getByText("bob@example.com")).toBeInTheDocument());
      expect(screen.getByTestId("share-grants")).toHaveClass("min-w-0");
    });

    it("renders a long email as a truncating label with the full id in its title", async () => {
      listMock.mockResolvedValue([
        {
          user_id: "alice.a-very-long-local-part-that-overflows@example.com",
          conversation_id: "conv_abc",
          level: 1,
        },
      ]);

      render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
        wrapper: createWrapper(),
      });

      const label = await screen.findByText(
        "alice.a-very-long-local-part-that-overflows@example.com",
      );
      // Tail truncation prioritizes the local part (the distinguishing half
      // when every grantee shares one company domain); the title tooltip
      // carries the full id.
      expect(label).toHaveClass("truncate");
      expect(label).toHaveAttribute(
        "title",
        "alice.a-very-long-local-part-that-overflows@example.com",
      );
    });
  });

  describe("with a host user-search provider (combobox)", () => {
    beforeEach(() => {
      // Install a deterministic searcher so the add-user field upgrades to the
      // suggestion combobox.
      userSearchMock.mockReturnValue(
        vi.fn(async (query: string) =>
          query.startsWith("a")
            ? [
                { userId: "alice@example.com", displayName: "Alice" },
                { userId: "amir@example.com", displayName: "Amir" },
              ]
            : [],
        ),
      );
    });

    it("renders the field as a combobox and shows suggestions while typing", async () => {
      listMock.mockResolvedValue([]);
      render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
        wrapper: createWrapper(),
      });
      await waitFor(() => expect(listMock).toHaveBeenCalled());

      const input = screen.getByPlaceholderText("alice@example.com");
      // The upgraded field carries role="combobox".
      expect(input).toHaveAttribute("role", "combobox");
      fireEvent.focus(input);
      fireEvent.change(input, { target: { value: "al" } });

      // The host searcher resolves to two matches, rendered as listbox options.
      await waitFor(() => expect(screen.getByRole("listbox")).toBeInTheDocument());
      expect(screen.getByRole("option", { name: /Alice/ })).toBeInTheDocument();
      expect(screen.getByRole("option", { name: /Amir/ })).toBeInTheDocument();
    });

    it("commits a clicked suggestion into the input value", async () => {
      listMock.mockResolvedValue([]);
      render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
        wrapper: createWrapper(),
      });
      await waitFor(() => expect(listMock).toHaveBeenCalled());

      const input = screen.getByPlaceholderText("alice@example.com") as HTMLInputElement;
      fireEvent.focus(input);
      fireEvent.change(input, { target: { value: "al" } });
      await waitFor(() => expect(screen.getByRole("listbox")).toBeInTheDocument());

      // mousedown (not click) so the input isn't blurred before commit.
      fireEvent.mouseDown(screen.getByRole("option", { name: /Alice/ }));
      expect(input.value).toBe("alice@example.com");
    });

    it("shows an empty-state message when the searcher returns no matches", async () => {
      listMock.mockResolvedValue([]);
      render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
        wrapper: createWrapper(),
      });
      await waitFor(() => expect(listMock).toHaveBeenCalled());

      const input = screen.getByPlaceholderText("alice@example.com");
      fireEvent.focus(input);
      // "z..." matches nothing in the stub searcher.
      fireEvent.change(input, { target: { value: "zzz" } });

      await waitFor(() => expect(screen.getByText("No matches")).toBeInTheDocument());
    });
  });

  describe("sharing mode", () => {
    it("off: shows the disabled notice and never fetches grants", async () => {
      render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
        wrapper: createSharingWrapper("off"),
      });

      expect(
        await screen.findByText("Sharing has been disabled for this Omnigent server."),
      ).toBeInTheDocument();
      // Off short-circuits before the grant-list query and hides all controls.
      expect(listMock).not.toHaveBeenCalled();
      expect(screen.queryByRole("button", { name: /grant/i })).not.toBeInTheDocument();
      expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    });

    it("on: renders the full controls with no disabled/read-only notice", async () => {
      listMock.mockResolvedValue([]);

      render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
        wrapper: createSharingWrapper("on"),
      });

      await waitFor(() => expect(listMock).toHaveBeenCalledWith("conv_abc"));
      expect(screen.getByRole("button", { name: /grant/i })).toBeInTheDocument();
      expect(
        screen.getByText("Invite others to view or collaborate on this session."),
      ).toBeInTheDocument();
      expect(
        screen.queryByText("Sharing has been disabled for this Omnigent server."),
      ).not.toBeInTheDocument();
    });

    it("read_only: shows the read-only notice, keeps Grant, offers only Read", async () => {
      listMock.mockResolvedValue([]);

      render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
        wrapper: createSharingWrapper("read_only"),
      });

      await waitFor(() => expect(listMock).toHaveBeenCalledWith("conv_abc"));
      expect(
        screen.getByText(
          "This server allows read-only sharing — invite others to view this session.",
        ),
      ).toBeInTheDocument();
      // Read grants are still allowed, so the Grant control stays.
      expect(screen.getByRole("button", { name: /grant/i })).toBeInTheDocument();
      // The add-form level select must offer only Read (Edit is hidden). With no
      // grants there is exactly one combobox (the add-form select).
      const trigger = screen.getByRole("combobox");
      trigger.focus();
      fireEvent.keyDown(trigger, { key: "Enter" });
      const listbox = await screen.findByRole("listbox");
      const options = within(listbox).getAllByRole("option");
      expect(options.map((o) => o.textContent)).toEqual(["Read"]);
    });

    it("restricted_read_only: presents the same read-only UI as read_only", async () => {
      // The per-session home/root block is enforced server-side; the modal
      // itself shows the read-only affordance for every session.
      listMock.mockResolvedValue([]);

      render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
        wrapper: createSharingWrapper("restricted_read_only"),
      });

      await waitFor(() => expect(listMock).toHaveBeenCalledWith("conv_abc"));
      expect(
        screen.getByText(
          "This server allows read-only sharing — invite others to view this session.",
        ),
      ).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /grant/i })).toBeInTheDocument();
      const trigger = screen.getByRole("combobox");
      trigger.focus();
      fireEvent.keyDown(trigger, { key: "Enter" });
      const listbox = await screen.findByRole("listbox");
      const options = within(listbox).getAllByRole("option");
      expect(options.map((o) => o.textContent)).toEqual(["Read"]);
    });
  });

  describe("public access", () => {
    it("hides the Public access toggle when the server disables public sharing", async () => {
      listMock.mockResolvedValue([]);

      render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
        wrapper: createInfoWrapper({ public_sharing_enabled: false }),
      });

      await waitFor(() => expect(listMock).toHaveBeenCalledWith("conv_abc"));
      // The user-grant UI stays; only the public toggle is gone.
      expect(screen.getByRole("button", { name: /grant/i })).toBeInTheDocument();
      expect(screen.queryByText("Public access")).not.toBeInTheDocument();
      expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    });

    it("shows the Public access toggle when public sharing is enabled", async () => {
      listMock.mockResolvedValue([]);

      render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
        wrapper: createInfoWrapper({ public_sharing_enabled: true }),
      });

      await waitFor(() => expect(listMock).toHaveBeenCalledWith("conv_abc"));
      expect(screen.getByText("Public access")).toBeInTheDocument();
      expect(screen.getByRole("switch")).toBeInTheDocument();
    });
  });

  describe("share QR code", () => {
    // The QR encodes an `omnigent://<host>/c/<id>` deep link (the same scheme
    // the desktop shell's deep-link handler parses — see electron/src/deepLink.js).
    // Pin the host resolution against the standalone origin and the embedded
    // host transform so the encoded value stays correct in both contexts.

    // jsdom's window.location is a non-configurable property; override it on a
    // plain object so `getShareableLink`/`getDeepLink` see the test origin. Awaits
    // the body so re-renders triggered during the test (e.g. the permissions
    // query resolving) still see the overridden origin before it's restored.
    async function withLocation(origin: string, fn: () => Promise<void> | void): Promise<void> {
      const original = window.location;
      Object.defineProperty(window, "location", {
        configurable: true,
        value: { ...original, origin },
      });
      try {
        await fn();
      } finally {
        Object.defineProperty(window, "location", { configurable: true, value: original });
      }
    }

    it("encodes the omnigent:// deep link for the session from the server origin", async () => {
      listMock.mockResolvedValue([]);

      await withLocation("https://app.example.com", async () => {
        render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
          wrapper: createWrapper(),
        });

        await waitFor(() => expect(listMock).toHaveBeenCalled());
        // The QR is hidden until the user clicks "Open in mobile app".
        expect(screen.queryByTestId("share-qr-code")).not.toBeInTheDocument();
        fireEvent.click(screen.getByRole("button", { name: /open in mobile app/i }));
        const qr = await screen.findByTestId("share-qr-code");
        expect(qr).toHaveAttribute("data-value", "omnigent://app.example.com/c/conv_abc");
      });
    });

    it("includes the port in the host when non-default", async () => {
      // Mirrors the deep-link convention: `omnigent://localhost:8000/c/<id>`.
      listMock.mockResolvedValue([]);

      await withLocation("http://localhost:8000", async () => {
        render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
          wrapper: createWrapper(),
        });

        await waitFor(() => expect(listMock).toHaveBeenCalled());
        fireEvent.click(screen.getByRole("button", { name: /open in mobile app/i }));
        const qr = await screen.findByTestId("share-qr-code");
        expect(qr).toHaveAttribute("data-value", "omnigent://localhost:8000/c/conv_abc");
      });
    });

    it("derives the host from the host transformShareLink in the embed", async () => {
      // In the embed the host transform returns the full absolute URL; the QR
      // must take its host from that URL, not window.location.origin.
      listMock.mockResolvedValue([]);
      transformLinkMock.mockReturnValue((path: string) => `https://host.example.com/embed#${path}`);

      render(<PermissionsModal sessionId="conv_xyz" open={true} onOpenChange={() => {}} />, {
        wrapper: createWrapper(),
      });

      await waitFor(() => expect(listMock).toHaveBeenCalled());
      fireEvent.click(screen.getByRole("button", { name: /open in mobile app/i }));
      const qr = await screen.findByTestId("share-qr-code");
      expect(qr).toHaveAttribute("data-value", "omnigent://host.example.com/c/conv_xyz");
    });
  });
});
