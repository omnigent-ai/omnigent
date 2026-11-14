import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { ServerInfo, SharingMode } from "@/lib/capabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { basenamedRouting, RoutingProvider } from "@/lib/routing";
import { PermissionsModal } from "./PermissionsModal";
import type * as SessionsApiModule from "@/lib/sessionsApi";

vi.mock("@/hooks/useSharing", () => ({
  usePublicSharingMaxLevel: () => ({ data: undefined }),
}));

vi.mock("@/lib/permissionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof api>()),
  listPermissions: vi.fn(),
  grantPermission: vi.fn(),
  revokePermission: vi.fn(),
}));

// The workspace-files toggle reads the session snapshot for its current state
// and PATCHes it. Mock both so the toggle's presence and its write can be
// driven without a live session fetch.
vi.mock("@/hooks/useSession", () => ({
  useSession: vi.fn(() => ({ session: null, isLoading: false, error: null })),
}));
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  updateSession: vi.fn(),
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
  const actual = await importOriginal<typeof host>();
  return {
    ...actual,
    getOmnigentUserSearch: vi.fn(() => undefined),
    getOmnigentTransformShareLink: vi.fn(() => undefined),
  };
});

import * as api from "@/lib/permissionsApi";
import * as host from "@/lib/host";
import { useSession } from "@/hooks/useSession";
import { updateSession } from "@/lib/sessionsApi";
import type { Session } from "@/lib/types";

const listMock = vi.mocked(api.listPermissions);
const grantMock = vi.mocked(api.grantPermission);
const revokeMock = vi.mocked(api.revokePermission);
const useSessionMock = vi.mocked(useSession);
const updateSessionMock = vi.mocked(updateSession);

/** Minimal session snapshot for the workspace-toggle tests. */
function sessionWith(overrides: Partial<Session>): Session {
  return {
    id: "conv_abc",
    agentId: "ag_owner",
    agentName: "dev",
    runnerId: null,
    status: "idle",
    createdAt: 1_700_000_000,
    title: "s",
    labels: {},
    items: [],
    pendingElicitations: [],
    permissionLevel: 3,
    parentSessionId: null,
    subAgentName: null,
    kind: "default",
    ...overrides,
  } as Session;
}
const userSearchMock = vi.mocked(host.getOmnigentUserSearch);
const transformLinkMock = vi.mocked(host.getOmnigentTransformShareLink);

function createWrapper(
  qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  }),
) {
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
    enabled_connections: [],
    sharing_mode: "on",
    public_sharing_enabled: true,
    server_version: null,
    smart_routing_enabled: false,
    smart_routing_sources: { external: false, oss: false },
    features: {},
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

async function chooseGeneralAccess(name: "No access" | "Read" | "Edit") {
  const trigger = await screen.findByRole("combobox", { name: "General access" });
  await waitFor(() => expect(trigger).toBeEnabled());
  trigger.focus();
  fireEvent.keyDown(trigger, { key: "Enter" });
  fireEvent.click(await screen.findByRole("option", { name }));
}

beforeEach(() => {
  listMock.mockReset();
  grantMock.mockReset();
  revokeMock.mockReset();
  // Default: standalone (no host providers). Combobox/transform tests opt in.
  userSearchMock.mockReturnValue(undefined);
  transformLinkMock.mockReturnValue(undefined);
  // Default: no session snapshot, so the workspace-files toggle stays hidden
  // for the existing grant/link tests. The toggle tests opt in below.
  useSessionMock.mockReset();
  useSessionMock.mockReturnValue({ session: null, isLoading: false, error: null });
  updateSessionMock.mockReset();
  updateSessionMock.mockResolvedValue(sessionWith({}));
});

afterEach(cleanup);

describe("PermissionsModal", () => {
  it.each(["General access", "New user permission", "Permission level for bob@example.com"])(
    "anchors the %s menu below the start of its trigger",
    async (label) => {
      listMock.mockResolvedValue([
        { user_id: "bob@example.com", conversation_id: "conv_abc", level: 2 },
      ]);

      render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
        wrapper: createSharingWrapper("on"),
      });

      const trigger = await screen.findByRole("combobox", { name: label });
      await waitFor(() => expect(trigger).toBeEnabled());
      trigger.focus();
      fireEvent.keyDown(trigger, { key: "Enter" });

      const menu = await screen.findByRole("listbox");
      await waitFor(() => {
        expect(menu).toHaveAttribute("data-side", "bottom");
        expect(menu).toHaveAttribute("data-align", "start");
      });
      expect(within(menu).getByRole("option", { name: "Read" })).toBeInTheDocument();

      fireEvent.keyDown(menu, { key: "Escape" });
      await waitFor(() => expect(screen.queryByRole("listbox")).not.toBeInTheDocument());
      await waitFor(() => expect(trigger).toHaveFocus());
    },
  );

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

  it.each([undefined, "report.html"])(
    "updates a grant's level inline without revocation and prepares file sharing (file: %s)",
    async (filePath) => {
      useSessionMock.mockReturnValue({
        session: sessionWith({ workspace: "/repo", shareWorkspaceFiles: false }),
        isLoading: false,
        error: null,
      });
      listMock.mockResolvedValue([
        { user_id: "bob@example.com", conversation_id: "conv_abc", level: 1 },
      ]);
      grantMock.mockResolvedValue({
        user_id: "bob@example.com",
        conversation_id: "conv_abc",
        level: 2,
      });

      render(
        <PermissionsModal
          sessionId="conv_abc"
          filePath={filePath}
          open={true}
          onOpenChange={() => {}}
        />,
        {
          wrapper: createWrapper(),
        },
      );

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
      if (filePath) {
        await waitFor(() =>
          expect(updateSessionMock).toHaveBeenCalledWith("conv_abc", { shareWorkspaceFiles: true }),
        );
        expect(grantMock.mock.invocationCallOrder[0]).toBeLessThan(
          updateSessionMock.mock.invocationCallOrder[0],
        );
      } else {
        expect(updateSessionMock).not.toHaveBeenCalled();
      }
    },
  );

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
    expect(screen.getAllByRole("combobox")).toHaveLength(3); // general + bob + grant form
  });

  it("toggles public access via grant/revoke of __public__ sentinel", async () => {
    listMock.mockResolvedValue([]);
    grantMock.mockResolvedValue({ user_id: "__public__", conversation_id: "conv_abc", level: 1 });

    render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(listMock).toHaveBeenCalled());

    await chooseGeneralAccess("Read");

    await waitFor(() => {
      expect(grantMock).toHaveBeenCalledWith("conv_abc", "__public__", 1);
    });
  });

  it("displays server error messages from failed grant", async () => {
    listMock.mockResolvedValue([]);
    let failGrant!: () => void;
    grantMock.mockReturnValue(
      new Promise((_resolve, reject) => {
        failGrant = () => reject(new Error("'rice' needs manage permission"));
      }),
    );

    render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(listMock).toHaveBeenCalled());

    const input = screen.getByPlaceholderText("alice@example.com");
    fireEvent.change(input, { target: { value: "rice" } });
    fireEvent.click(screen.getByRole("button", { name: /grant/i }));

    await waitFor(() => expect(grantMock).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: "Copy link" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Open in mobile app" })).toBeEnabled();
    failGrant();
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
    const triggers = screen
      .getAllByRole("combobox")
      .filter((trigger) => trigger.getAttribute("aria-label") !== "General access");
    expect(triggers).toHaveLength(2);
    // The manage grant's level is still visible to the viewer as static text.
    expect(screen.getByText("Manage")).toBeInTheDocument();

    // Only one listbox can be open, so each interaction must finish first.
    /* oxlint-disable no-await-in-loop */
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
    /* oxlint-enable no-await-in-loop */
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

  it.each([
    [
      "denies clipboard write",
      {
        writeText: vi
          .fn()
          .mockRejectedValue(new DOMException("Write permission denied.", "NotAllowedError")),
      },
    ],
    ["is unavailable on an insecure origin", undefined],
  ])("copies the link and confirms when the async clipboard %s", async (_case, clipboard) => {
    listMock.mockResolvedValue([]);
    const copiedText = vi.fn();
    Object.assign(navigator, { clipboard });
    Object.defineProperty(document, "execCommand", {
      configurable: true,
      value: vi.fn((command: string) => {
        if (command !== "copy") return false;
        const event = new Event("copy", { bubbles: true, cancelable: true }) as ClipboardEvent;
        Object.defineProperty(event, "clipboardData", {
          value: { setData: (_type: string, text: string) => copiedText(text) },
        });
        document.dispatchEvent(event);
        if (!event.defaultPrevented && document.activeElement instanceof HTMLTextAreaElement) {
          copiedText(document.activeElement.value);
        }
        return true;
      }),
    });

    try {
      render(<PermissionsModal sessionId="conv_xyz" open={true} onOpenChange={() => {}} />, {
        wrapper: createWrapper(),
      });

      fireEvent.click(screen.getByRole("button", { name: /copy link/i }));

      expect(await screen.findByRole("button", { name: /copied!/i })).toBeInTheDocument();
      expect(copiedText).toHaveBeenCalledWith(`${window.location.origin}/c/conv_xyz`);
    } finally {
      delete (document as { execCommand?: unknown }).execCommand;
    }
  });

  it.each([undefined, "report.html"])(
    "shows an error when the link cannot be copied (file: %s)",
    async (filePath) => {
      listMock.mockResolvedValue([]);
      useSessionMock.mockReturnValue({
        session: sessionWith({ workspace: "/repo", shareWorkspaceFiles: true }),
        isLoading: false,
        error: null,
      });
      Object.assign(navigator, { clipboard: undefined });
      Object.defineProperty(document, "execCommand", {
        configurable: true,
        value: vi.fn(() => false),
      });
      const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
      try {
        render(
          <PermissionsModal
            sessionId="conv_abc"
            filePath={filePath}
            open
            onOpenChange={() => {}}
          />,
          { wrapper: createWrapper() },
        );
        fireEvent.click(
          screen.getByRole("button", { name: filePath ? "Copy file link" : "Copy link" }),
        );

        expect(await screen.findByText(/Could not copy the link/)).toBeInTheDocument();
        expect(screen.queryByRole("button", { name: "Copied!" })).toBeNull();
      } finally {
        warn.mockRestore();
        delete (document as { execCommand?: unknown }).execCommand;
      }
    },
  );

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

  // Overflow regression: a `flex-row` footer override forced Copy link,
  // "Open in mobile app", and Done onto one row at every width. On phones the
  // row's min-content exceeds the dialog, and as a min-width:auto grid item it
  // widens the whole dialog grid track, clipping every stretched sibling (the
  // add-grant row's Grant button included) at the dialog's right edge. jsdom
  // does no layout, so pin the class contract: stacked by default, a row only
  // from sm: up.
  describe("footer on a phone-width dialog", () => {
    it("keeps the footer stacked below sm instead of forcing one row", async () => {
      listMock.mockResolvedValue([]);

      render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
        wrapper: createWrapper(),
      });

      await waitFor(() => expect(screen.getByText("No grants yet.")).toBeInTheDocument());
      const footer = document.querySelector('[data-slot="dialog-footer"]');
      expect(footer).toHaveClass("flex-col-reverse", "sm:flex-row", "sm:justify-between");
      expect(footer).not.toHaveClass("flex-row", "justify-between");
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

    it("disables the combobox for a workspace that cannot be shared", async () => {
      listMock.mockResolvedValue([]);
      render(
        <PermissionsModal
          sessionId="conv_abc"
          workspace="/home/alice"
          open
          onOpenChange={() => {}}
        />,
        { wrapper: createSharingWrapper("restricted_read_only") },
      );
      await waitFor(() => expect(listMock).toHaveBeenCalled());

      const input = screen.getByPlaceholderText("alice@example.com");
      expect(input).toHaveAttribute("role", "combobox");
      expect(input).toBeDisabled();
    });

    it("closes open suggestions when the workspace becomes unshareable", async () => {
      listMock.mockResolvedValue([]);
      const { rerender } = render(
        <PermissionsModal
          sessionId="conv_abc"
          workspace="/home/alice/project"
          open
          onOpenChange={() => {}}
        />,
        { wrapper: createSharingWrapper("restricted_read_only") },
      );
      const input = screen.getByPlaceholderText("alice@example.com");
      fireEvent.focus(input);
      fireEvent.change(input, { target: { value: "al" } });
      await waitFor(() => expect(screen.getByRole("listbox")).toBeInTheDocument());

      rerender(
        <PermissionsModal
          sessionId="conv_abc"
          workspace="/home/alice"
          open
          onOpenChange={() => {}}
        />,
      );
      expect(input).toBeDisabled();
      expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
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
      expect(screen.queryByText(/Please be careful when sharing/)).not.toBeInTheDocument();
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
      expect(
        screen.getByText(
          "Please be careful when sharing. Read access will allow for reading of all session outputs.",
        ),
      ).toBeInTheDocument();
      // Read grants are still allowed, so the Grant control stays.
      expect(screen.getByRole("button", { name: /grant/i })).toBeInTheDocument();
      // The add-form level select must offer only Read (Edit is hidden). With no
      // grants there is exactly one combobox (the add-form select).
      const trigger = screen.getByRole("combobox", { name: "New user permission" });
      trigger.focus();
      fireEvent.keyDown(trigger, { key: "Enter" });
      const listbox = await screen.findByRole("listbox");
      const options = within(listbox).getAllByRole("option");
      expect(options.map((o) => o.textContent)).toEqual(["Read"]);
    });

    it("restricted_read_only: presents the same read-only UI when the workspace is unknown", async () => {
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
      expect(
        screen.getByText(
          "Please be careful when sharing. Read access will allow for reading of all session outputs.",
        ),
      ).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /grant/i })).toBeInTheDocument();
      const trigger = screen.getByRole("combobox", { name: "New user permission" });
      trigger.focus();
      fireEvent.keyDown(trigger, { key: "Enter" });
      const listbox = await screen.findByRole("listbox");
      const options = within(listbox).getAllByRole("option");
      expect(options.map((o) => o.textContent)).toEqual(["Read"]);
    });
  });

  describe("workspace restrictions", () => {
    it("allows read-only grants in project directories", async () => {
      listMock.mockResolvedValue([]);
      grantMock.mockResolvedValue({ conversation_id: "conv_abc", user_id: "bob", level: 1 });
      render(
        <PermissionsModal
          sessionId="conv_abc"
          workspace="/home/alice/project"
          open
          onOpenChange={() => {}}
        />,
        { wrapper: createSharingWrapper("restricted_read_only") },
      );
      expect(screen.queryByText(/This session's working directory/)).not.toBeInTheDocument();
      fireEvent.change(screen.getByLabelText("User ID"), { target: { value: "bob" } });
      fireEvent.click(screen.getByRole("button", { name: /grant/i }));
      await waitFor(() => expect(grantMock).toHaveBeenCalledWith("conv_abc", "bob", 1));
    });

    it.each(["/home/alice", "/Users/bob", "/var/home/carol", "/root", "/", undefined])(
      "disables new grants and explains the restriction for workspace prop %s",
      async (workspace) => {
        listMock.mockResolvedValue([]);
        const filePath = workspace === undefined ? "report.html" : undefined;
        useSessionMock.mockReturnValue({
          session: sessionWith({
            workspace: workspace ?? "/home/alice",
            shareWorkspaceFiles: false,
          }),
          isLoading: false,
          error: null,
        });
        render(
          <PermissionsModal
            sessionId="conv_abc"
            workspace={workspace}
            filePath={filePath}
            open
            onOpenChange={() => {}}
          />,
          {
            wrapper: createSharingWrapper("restricted_read_only"),
          },
        );

        const warning = screen.getByText(/This session's working directory/);
        expect(warning).toHaveClass("text-destructive");
        const grantButton = screen.getByRole("button", { name: /grant/i });
        expect(screen.getByLabelText("User ID")).toBeDisabled();
        expect(screen.getByRole("combobox", { name: "New user permission" })).toBeDisabled();
        expect(grantButton).toHaveAccessibleDescription(warning.textContent ?? "");
        // A user id in the field must not re-enable Grant: submitting would be
        // a silent no-op because handleGrant bails out for blocked workspaces.
        fireEvent.change(screen.getByLabelText("User ID"), { target: { value: "bob" } });
        expect(grantButton).toBeDisabled();
        fireEvent.click(grantButton);
        expect(screen.getByRole("combobox", { name: "General access" })).toBeDisabled();
        await waitFor(() => expect(listMock).toHaveBeenCalledWith("conv_abc"));
        expect(grantMock).not.toHaveBeenCalled();
        if (filePath) {
          expect(screen.getByRole("button", { name: "Copy file link" })).toBeDisabled();
          expect(screen.getByRole("button", { name: "Open in mobile app" })).toBeDisabled();
          expect(updateSessionMock).not.toHaveBeenCalled();
        }
      },
    );

    it("rejects a form submission once the workspace becomes unshareable", async () => {
      listMock.mockResolvedValue([]);
      const { rerender } = render(
        <PermissionsModal
          sessionId="conv_abc"
          workspace="/home/alice/project"
          open
          onOpenChange={() => {}}
        />,
        { wrapper: createSharingWrapper("restricted_read_only") },
      );
      fireEvent.change(screen.getByLabelText("User ID"), { target: { value: "bob" } });
      expect(screen.getByRole("button", { name: /grant/i })).toBeEnabled();

      rerender(
        <PermissionsModal
          sessionId="conv_abc"
          workspace="/home/alice"
          open
          onOpenChange={() => {}}
        />,
      );
      const grantButton = screen.getByRole("button", { name: /grant/i });
      expect(grantButton).toBeDisabled();
      // Submit the form directly: a disabled button never reaches handleGrant,
      // so this is what exercises its own guard.
      fireEvent.submit(grantButton.closest("form")!);
      await waitFor(() => expect(listMock).toHaveBeenCalledWith("conv_abc"));
      expect(grantMock).not.toHaveBeenCalled();
    });

    it.each(["on", "read_only"] as const)(
      "does not block home directories in %s mode",
      async (mode) => {
        listMock.mockResolvedValue([]);
        grantMock.mockResolvedValue({ conversation_id: "conv_abc", user_id: "bob", level: 1 });
        render(
          <PermissionsModal
            sessionId="conv_abc"
            workspace="/home/alice"
            open
            onOpenChange={() => {}}
          />,
          {
            wrapper: createSharingWrapper(mode),
          },
        );
        fireEvent.change(screen.getByLabelText("User ID"), { target: { value: "bob" } });
        fireEvent.click(screen.getByRole("button", { name: /grant/i }));
        await waitFor(() => expect(grantMock).toHaveBeenCalledWith("conv_abc", "bob", 1));
        expect(screen.queryByText(/This session's working directory/)).not.toBeInTheDocument();
      },
    );

    it.each([undefined, "report.html"])(
      "keeps existing user and public grants revocable for blocked workspaces (file: %s)",
      async (filePath) => {
        listMock.mockResolvedValue([
          { conversation_id: "conv_abc", user_id: "alice", level: 4 },
          { conversation_id: "conv_abc", user_id: "bob", level: 2 },
          { conversation_id: "conv_abc", user_id: "__public__", level: 1 },
        ]);
        revokeMock.mockResolvedValue(undefined);
        render(
          <PermissionsModal
            sessionId="conv_abc"
            workspace="/home/alice"
            filePath={filePath}
            open
            onOpenChange={() => {}}
          />,
          {
            wrapper: createSharingWrapper("restricted_read_only"),
          },
        );
        fireEvent.click(await screen.findByRole("button", { name: "Revoke" }));
        await waitFor(() => expect(revokeMock).toHaveBeenCalledWith("conv_abc", "bob"));
        await chooseGeneralAccess("No access");
        await waitFor(() => expect(revokeMock).toHaveBeenCalledWith("conv_abc", "__public__"));
        expect(grantMock).not.toHaveBeenCalled();
      },
    );
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
      expect(screen.queryByText("General access")).not.toBeInTheDocument();
      expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    });

    it("shows the Public access toggle when public sharing is enabled", async () => {
      listMock.mockResolvedValue([]);

      render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
        wrapper: createInfoWrapper({ public_sharing_enabled: true }),
      });

      await waitFor(() => expect(listMock).toHaveBeenCalledWith("conv_abc"));
      expect(screen.getByText("General access")).toBeInTheDocument();
      expect(screen.getByRole("combobox", { name: "General access" })).toBeInTheDocument();
    });

    it("grants public Edit only when the server explicitly permits it", async () => {
      listMock.mockResolvedValue([]);
      grantMock.mockResolvedValue({ user_id: "__public__", conversation_id: "conv_abc", level: 2 });
      render(<PermissionsModal sessionId="conv_abc" open onOpenChange={() => {}} />, {
        wrapper: createInfoWrapper({ public_sharing_max_level: "edit" }),
      });
      await chooseGeneralAccess("Edit");
      await waitFor(() => expect(grantMock).toHaveBeenCalledWith("conv_abc", "__public__", 2));
    });

    it.each([undefined, "read"] as const)(
      "offers only No access and Read for a %s ceiling",
      async (level) => {
        listMock.mockResolvedValue([]);
        render(<PermissionsModal sessionId="conv_abc" open onOpenChange={() => {}} />, {
          wrapper: createInfoWrapper({ public_sharing_max_level: level }),
        });
        const trigger = screen.getByRole("combobox", { name: "General access" });
        await waitFor(() => expect(trigger).toBeEnabled());
        trigger.focus();
        fireEvent.keyDown(trigger, { key: "Enter" });
        const listbox = await screen.findByRole("listbox");
        expect(
          within(listbox)
            .getAllByRole("option")
            .map((option) => option.textContent),
        ).toEqual(["No access", "Read"]);
      },
    );

    it("keeps an existing public grant revocable when public sharing is disabled", async () => {
      listMock.mockResolvedValue([
        { user_id: "__public__", conversation_id: "conv_abc", level: 2 },
      ]);
      revokeMock.mockResolvedValue(undefined);
      render(<PermissionsModal sessionId="conv_abc" open onOpenChange={() => {}} />, {
        wrapper: createInfoWrapper({ public_sharing_enabled: false }),
      });
      await chooseGeneralAccess("No access");
      await waitFor(() => expect(revokeMock).toHaveBeenCalledWith("conv_abc", "__public__"));
    });
  });

  describe("file sharing", () => {
    beforeEach(() => {
      useSessionMock.mockReturnValue({
        session: sessionWith({ workspace: "/repo", shareWorkspaceFiles: true }),
        isLoading: false,
        error: null,
      });
    });

    it("leaves workspace files private when the file sharing dialog is only opened", async () => {
      listMock.mockResolvedValue([]);
      useSessionMock.mockReturnValue({
        session: sessionWith({ workspace: "/repo", shareWorkspaceFiles: false }),
        isLoading: false,
        error: null,
      });
      render(
        <PermissionsModal
          sessionId="conv_abc"
          filePath="report.html"
          open
          onOpenChange={() => {}}
        />,
        {
          wrapper: createWrapper(),
        },
      );

      expect(
        screen.getByText(/Sharing enables files for every session reader/),
      ).toBeInTheDocument();
      await screen.findByText("No grants yet.");
      expect(updateSessionMock).not.toHaveBeenCalled();
      expect(grantMock).not.toHaveBeenCalled();
    });

    it.each([
      [true, false],
      [false, false],
      [true, true],
    ])(
      "enables workspace files only after a recipient grant succeeds (success: %s, already shared: %s)",
      async (succeeds, workspaceShared) => {
        listMock.mockResolvedValue([]);
        useSessionMock.mockReturnValue({
          session: sessionWith({ workspace: "/repo", shareWorkspaceFiles: workspaceShared }),
          isLoading: false,
          error: null,
        });
        let finishGrant!: () => void;
        grantMock.mockReturnValue(
          new Promise((resolve, reject) => {
            finishGrant = () => {
              if (succeeds) resolve({ user_id: "carol", conversation_id: "conv_abc", level: 1 });
              else reject(new Error("Grant failed"));
            };
          }),
        );
        render(
          <PermissionsModal
            sessionId="conv_abc"
            filePath="report.html"
            open
            onOpenChange={() => {}}
          />,
          { wrapper: createWrapper() },
        );
        await screen.findByText("No grants yet.");
        fireEvent.change(screen.getByPlaceholderText("alice@example.com"), {
          target: { value: "carol" },
        });
        fireEvent.click(screen.getByRole("button", { name: "Grant" }));

        await waitFor(() => expect(grantMock).toHaveBeenCalledWith("conv_abc", "carol", 1, true));
        expect(updateSessionMock).not.toHaveBeenCalled();
        expect(screen.getByRole("button", { name: "Copy file link" })).toBeDisabled();
        expect(screen.getByRole("button", { name: "Open in mobile app" })).toBeDisabled();
        const workspaceToggle = within(screen.getByTestId("share-workspace-files")).getByRole(
          "switch",
        );
        expect(workspaceToggle).toBeDisabled();
        fireEvent.click(workspaceToggle);
        expect(workspaceToggle).toHaveAttribute("aria-checked", String(workspaceShared));
        expect(updateSessionMock).not.toHaveBeenCalled();
        finishGrant();
        if (succeeds && workspaceShared) {
          await waitFor(() =>
            expect(screen.getByPlaceholderText("alice@example.com")).toHaveValue(""),
          );
          expect(updateSessionMock).not.toHaveBeenCalled();
        } else if (succeeds) {
          await waitFor(() =>
            expect(updateSessionMock).toHaveBeenCalledWith("conv_abc", {
              shareWorkspaceFiles: true,
            }),
          );
          expect(grantMock.mock.invocationCallOrder[0]).toBeLessThan(
            updateSessionMock.mock.invocationCallOrder[0],
          );
        } else {
          expect(await screen.findByText("Grant failed")).toBeInTheDocument();
          expect(updateSessionMock).not.toHaveBeenCalled();
        }
      },
    );

    it("enables file access for an existing viewer without changing the viewer's grant", async () => {
      listMock.mockResolvedValue([{ user_id: "alice", conversation_id: "conv_abc", level: 1 }]);
      useSessionMock.mockReturnValue({
        session: sessionWith({ workspace: "/repo", shareWorkspaceFiles: false }),
        isLoading: false,
        error: null,
      });
      render(
        <PermissionsModal
          sessionId="conv_abc"
          filePath="report.html"
          open
          onOpenChange={() => {}}
        />,
        {
          wrapper: createWrapper(),
        },
      );
      await screen.findByText("alice");
      fireEvent.change(screen.getByPlaceholderText("alice@example.com"), {
        target: { value: "alice" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Grant" }));

      await waitFor(() =>
        expect(updateSessionMock).toHaveBeenCalledWith("conv_abc", { shareWorkspaceFiles: true }),
      );
      await waitFor(() => expect(screen.getByPlaceholderText("alice@example.com")).toHaveValue(""));
      expect(grantMock).not.toHaveBeenCalled();
    });

    it("reports that session access remains granted when workspace file sharing fails", async () => {
      listMock.mockResolvedValue([]);
      useSessionMock.mockReturnValue({
        session: sessionWith({ workspace: "/repo", shareWorkspaceFiles: false }),
        isLoading: false,
        error: null,
      });
      updateSessionMock.mockRejectedValue(new Error("Workspace sharing failed"));
      grantMock.mockResolvedValue({ user_id: "carol", conversation_id: "conv_abc", level: 1 });
      render(
        <PermissionsModal
          sessionId="conv_abc"
          filePath="report.html"
          open
          onOpenChange={() => {}}
        />,
        {
          wrapper: createWrapper(),
        },
      );
      await screen.findByText("No grants yet.");
      fireEvent.change(screen.getByPlaceholderText("alice@example.com"), {
        target: { value: "carol" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Grant" }));

      expect(
        await screen.findByText(
          "Session access is granted, but file sharing failed: Workspace sharing failed",
        ),
      ).toBeInTheDocument();
      expect(grantMock).toHaveBeenCalledWith("conv_abc", "carol", 1, true);
      expect(revokeMock).not.toHaveBeenCalled();
    });

    it.each(["/repo", null])(
      "copies immediately and confirms after sharing workspace files (snapshot workspace: %s)",
      async (workspace) => {
        listMock.mockResolvedValue([]);
        const queryClient = new QueryClient();
        const cached = sessionWith({
          workspace,
          title: "Cached title",
          shareWorkspaceFiles: false,
        });
        if (workspace) queryClient.setQueryData(["session", "conv_abc"], cached);
        useSessionMock.mockReturnValue({
          session: sessionWith({ workspace, shareWorkspaceFiles: false }),
          isLoading: false,
          error: null,
        });
        const writeText = vi.fn().mockResolvedValue(undefined);
        Object.assign(navigator, { clipboard: { writeText } });
        let enableFiles!: (session: Session) => void;
        updateSessionMock.mockReturnValue(
          new Promise((resolve) => {
            enableFiles = resolve;
          }),
        );
        render(
          <PermissionsModal
            sessionId="conv_abc"
            filePath="report.html"
            open
            onOpenChange={() => {}}
          />,
          {
            wrapper: createWrapper(queryClient),
          },
        );
        fireEvent.click(screen.getByRole("button", { name: "Copy file link" }));

        expect(writeText).toHaveBeenCalled();
        await waitFor(() =>
          expect(updateSessionMock).toHaveBeenCalledWith("conv_abc", { shareWorkspaceFiles: true }),
        );
        expect(screen.queryByRole("button", { name: "Copied!" })).toBeNull();
        enableFiles(
          sessionWith({ workspace, title: "Stale response title", shareWorkspaceFiles: true }),
        );
        expect(await screen.findByRole("button", { name: "Copied!" })).toBeInTheDocument();
        if (workspace) {
          expect(queryClient.getQueryData(["session", "conv_abc"])).toEqual({
            ...cached,
            shareWorkspaceFiles: true,
          });
        } else {
          expect(queryClient.getQueryData(["session", "conv_abc"])).toBeUndefined();
        }
      },
    );

    it.each([
      [true, false],
      [false, false],
      [false, true],
    ])(
      "reports copy and preparation outcomes (copy succeeds: %s, preparation succeeds: %s)",
      async (copySucceeds, preparationSucceeds) => {
        listMock.mockResolvedValue([]);
        useSessionMock.mockReturnValue({
          session: sessionWith({ workspace: "/repo", shareWorkspaceFiles: false }),
          isLoading: false,
          error: null,
        });
        let finishPreparation!: () => void;
        updateSessionMock.mockReturnValue(
          new Promise<Session>((resolve, reject) => {
            finishPreparation = () => {
              if (preparationSucceeds)
                resolve(sessionWith({ workspace: "/repo", shareWorkspaceFiles: true }));
              else reject(new Error("Workspace sharing failed"));
            };
          }),
        );
        const writeText = vi.fn();
        if (copySucceeds) writeText.mockResolvedValue(undefined);
        else writeText.mockRejectedValue(new Error("Clipboard unavailable"));
        Object.assign(navigator, { clipboard: { writeText } });
        Object.defineProperty(document, "execCommand", {
          configurable: true,
          value: vi.fn(() => false),
        });
        const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
        try {
          render(
            <PermissionsModal
              sessionId="conv_abc"
              filePath="report.html"
              open
              onOpenChange={() => {}}
            />,
            { wrapper: createWrapper() },
          );
          fireEvent.click(screen.getByRole("button", { name: "Copy file link" }));

          expect(writeText).toHaveBeenCalled();
          await waitFor(() => expect(updateSessionMock).toHaveBeenCalled());
          expect(screen.queryByText(/Could not copy|Link copied/)).toBeNull();
          finishPreparation();
          if (copySucceeds) {
            expect(
              await screen.findByText(/Link copied, but file sharing failed/),
            ).toHaveTextContent("Workspace sharing failed");
          } else if (preparationSucceeds) {
            expect(
              await screen.findByText(/Could not copy the link.*Workspace files are shared/),
            ).toBeInTheDocument();
          } else {
            expect(
              await screen.findByText(/Could not copy the link, and file sharing failed/),
            ).toHaveTextContent("Workspace sharing failed");
          }
          expect(screen.queryByRole("button", { name: "Copied!" })).toBeNull();
          if (!preparationSucceeds) {
            expect(warn).toHaveBeenCalledWith("Failed to prepare file share", expect.any(Error));
          }
          if (!copySucceeds) {
            expect(warn).toHaveBeenCalledWith(
              "Failed to copy link to clipboard",
              expect.any(Error),
            );
          }
        } finally {
          warn.mockRestore();
          delete (document as { execCommand?: unknown }).execCommand;
        }
      },
    );

    it.each([true, false])(
      "shows the mobile QR code only after workspace preparation succeeds (success: %s)",
      async (succeeds) => {
        listMock.mockResolvedValue([]);
        useSessionMock.mockReturnValue({
          session: sessionWith({ workspace: "/repo", shareWorkspaceFiles: false }),
          isLoading: false,
          error: null,
        });
        let finishPreparation!: () => void;
        updateSessionMock.mockReturnValue(
          new Promise<Session>((resolve, reject) => {
            finishPreparation = () => {
              if (succeeds) resolve(sessionWith({ workspace: "/repo", shareWorkspaceFiles: true }));
              else reject(new Error("Workspace sharing failed"));
            };
          }),
        );
        render(
          <PermissionsModal
            sessionId="conv_abc"
            filePath="report.html"
            open
            onOpenChange={() => {}}
          />,
          {
            wrapper: createWrapper(),
          },
        );
        fireEvent.click(screen.getByRole("button", { name: "Open in mobile app" }));

        await waitFor(() =>
          expect(updateSessionMock).toHaveBeenCalledWith("conv_abc", { shareWorkspaceFiles: true }),
        );
        expect(screen.queryByTestId("share-qr-code")).toBeNull();
        finishPreparation();
        if (succeeds) {
          expect(await screen.findByTestId("share-qr-code")).toBeInTheDocument();
        } else {
          expect(await screen.findByText("Workspace sharing failed")).toBeInTheDocument();
          expect(screen.queryByTestId("share-qr-code")).toBeNull();
        }
      },
    );

    it.each([true, false])(
      "enables workspace files only after a general Read grant succeeds (success: %s)",
      async (succeeds) => {
        listMock.mockResolvedValue([]);
        if (succeeds) {
          grantMock.mockResolvedValue({
            user_id: "__public__",
            conversation_id: "conv_abc",
            level: 1,
          });
        } else {
          grantMock.mockRejectedValue(new Error("Grant failed"));
        }
        useSessionMock.mockReturnValue({
          session: sessionWith({ workspace: "/repo", shareWorkspaceFiles: false }),
          isLoading: false,
          error: null,
        });
        render(
          <PermissionsModal
            sessionId="conv_abc"
            filePath="report.html"
            open
            onOpenChange={() => {}}
          />,
          { wrapper: createWrapper() },
        );
        await chooseGeneralAccess("Read");

        if (succeeds) {
          await waitFor(() =>
            expect(updateSessionMock).toHaveBeenCalledWith("conv_abc", {
              shareWorkspaceFiles: true,
            }),
          );
          expect(grantMock.mock.invocationCallOrder[0]).toBeLessThan(
            updateSessionMock.mock.invocationCallOrder[0],
          );
        } else {
          expect(await screen.findByText("Grant failed")).toBeInTheDocument();
          expect(updateSessionMock).not.toHaveBeenCalled();
        }
        expect(grantMock).toHaveBeenCalledWith("conv_abc", "__public__", 1);
      },
    );

    it.each([null, new Error("Could not load the session")])(
      "keeps grants revocable while file sharing waits for the session (error: %s)",
      async (sessionError) => {
        listMock.mockResolvedValue([
          { user_id: "bob", conversation_id: "conv_abc", level: 1 },
          { user_id: "__public__", conversation_id: "conv_abc", level: 1 },
        ]);
        revokeMock.mockResolvedValue(undefined);
        useSessionMock.mockReturnValue({
          session: null,
          isLoading: !sessionError,
          error: sessionError,
        });
        render(
          <PermissionsModal
            sessionId="conv_abc"
            filePath="report.html"
            open
            onOpenChange={() => {}}
          />,
          { wrapper: createWrapper() },
        );
        await screen.findByText("bob");
        fireEvent.change(screen.getByPlaceholderText("alice@example.com"), {
          target: { value: "carol" },
        });

        expect(screen.getByRole("button", { name: "Grant" })).toBeDisabled();
        expect(screen.getByRole("button", { name: "Copy file link" })).toBeDisabled();
        expect(screen.getByRole("button", { name: "Open in mobile app" })).toBeDisabled();
        expect(screen.getByRole("combobox", { name: "Permission level for bob" })).toBeDisabled();
        if (sessionError) expect(screen.getByText(sessionError.message)).toBeInTheDocument();
        fireEvent.click(screen.getByRole("button", { name: "Revoke" }));
        await waitFor(() => expect(revokeMock).toHaveBeenCalledWith("conv_abc", "bob"));
        await chooseGeneralAccess("No access");
        await waitFor(() => expect(revokeMock).toHaveBeenCalledWith("conv_abc", "__public__"));
        expect(grantMock).not.toHaveBeenCalled();
        expect(updateSessionMock).not.toHaveBeenCalled();
      },
    );

    it("explains session access and grants new recipients read access", async () => {
      listMock.mockResolvedValue([]);
      grantMock.mockResolvedValue({ user_id: "carol", conversation_id: "conv_abc", level: 1 });

      render(
        <PermissionsModal
          sessionId="conv_abc"
          filePath="artifacts/report.html"
          open={true}
          onOpenChange={() => {}}
        />,
        { wrapper: createWrapper() },
      );

      expect(screen.getByRole("heading", { name: "Share this file" })).toBeInTheDocument();
      expect(
        within(screen.getByRole("note")).getByText(
          "Shares the entire session and all workspace files",
        ),
      ).toBeInTheDocument();
      expect(screen.getByRole("note")).toHaveTextContent(
        "Includes all messages. Sharing enables files for every session reader. New invites default to read-only; existing permissions stay.",
      );
      expect(screen.getByRole("button", { name: "Copy file link" })).toBeEnabled();
      expect(
        within(screen.getByTestId("share-workspace-files")).getByText("artifacts/report.html"),
      ).toBeInTheDocument();
      await waitFor(() => expect(screen.getByText("No grants yet.")).toBeInTheDocument());
      fireEvent.change(screen.getByPlaceholderText("alice@example.com"), {
        target: { value: "carol" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Grant" }));
      await waitFor(() => expect(grantMock).toHaveBeenCalledWith("conv_abc", "carol", 1, true));
    });

    it.each([1, 2, 3, 4])(
      "preserves an existing level %i grant when sharing a file",
      async (level) => {
        listMock
          .mockResolvedValueOnce([])
          .mockResolvedValue([
            { user_id: "alice@example.com", conversation_id: "conv_abc", level },
          ]);

        render(
          <PermissionsModal
            sessionId="conv_abc"
            filePath="report.md"
            open={true}
            onOpenChange={() => {}}
          />,
          { wrapper: createWrapper() },
        );

        await screen.findByText("No grants yet.");
        fireEvent.change(screen.getByPlaceholderText("alice@example.com"), {
          target: { value: "alice@example.com" },
        });
        fireEvent.click(screen.getByRole("button", { name: "Grant" }));
        await waitFor(() =>
          expect(screen.getByPlaceholderText("alice@example.com")).toHaveValue(""),
        );
        expect(grantMock).not.toHaveBeenCalled();
        expect(revokeMock).not.toHaveBeenCalled();
      },
    );

    it.each([
      [false, true],
      [true, true],
      [true, false],
    ])(
      "waits for fresh permissions before granting file access (cached: %s, refresh succeeds: %s)",
      async (cached, succeeds) => {
        const queryClient = new QueryClient({
          defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
        });
        if (cached) queryClient.setQueryData(["permissions", "conv_abc"], []);
        let finishPermissions!: () => void;
        listMock.mockImplementation(
          () =>
            new Promise((resolve, reject) => {
              finishPermissions = () => {
                if (succeeds) {
                  resolve([
                    { user_id: "alice@example.com", conversation_id: "conv_abc", level: 3 },
                  ]);
                } else {
                  reject(new Error("Could not refresh permissions"));
                }
              };
            }),
        );
        render(
          <PermissionsModal
            sessionId="conv_abc"
            filePath="report.md"
            open={true}
            onOpenChange={() => {}}
          />,
          { wrapper: createWrapper(queryClient) },
        );

        const input = screen.getByPlaceholderText("alice@example.com");
        const grantButton = screen.getByRole("button", { name: "Grant" });
        fireEvent.change(input, { target: { value: "alice@example.com" } });
        if (!cached) {
          expect(grantButton).toBeDisabled();
          finishPermissions();
          await screen.findByText("alice@example.com");
        }
        fireEvent.click(grantButton);
        await waitFor(() => expect(listMock).toHaveBeenCalledTimes(2));
        expect(grantButton).toBeDisabled();
        expect(screen.getByRole("button", { name: "Copy file link" })).toBeDisabled();
        expect(grantMock).not.toHaveBeenCalled();
        expect(updateSessionMock).not.toHaveBeenCalled();

        finishPermissions();
        if (succeeds) {
          await waitFor(() => expect(input).toHaveValue(""));
        } else {
          expect(await screen.findByText("Could not refresh permissions")).toBeInTheDocument();
          expect(input).toHaveValue("alice@example.com");
        }
        expect(grantMock).not.toHaveBeenCalled();
        expect(revokeMock).not.toHaveBeenCalled();
      },
    );

    it("copies an encoded fullscreen file link without current viewer parameters", async () => {
      listMock.mockResolvedValue([]);
      const filePath = "artifacts/财务 report #1 & + %.html";
      const writeText = vi.fn().mockResolvedValue(undefined);
      Object.assign(navigator, { clipboard: { writeText } });
      const original = window.location;
      Object.defineProperty(window, "location", {
        configurable: true,
        value: {
          ...original,
          origin: "https://app.example.com",
          href: "https://app.example.com/c/other?file=old&diff=1&comment=stale&view=terminal",
        },
      });
      try {
        render(
          <PermissionsModal
            sessionId="conv_abc"
            filePath={filePath}
            open={true}
            onOpenChange={() => {}}
          />,
          { wrapper: createWrapper() },
        );
        fireEvent.click(screen.getByRole("button", { name: /copy file link/i }));
        await waitFor(() => expect(writeText).toHaveBeenCalled());
        const link = new URL(writeText.mock.calls[0][0]);
        expect(link.origin).toBe("https://app.example.com");
        expect(link.pathname).toBe("/c/conv_abc");
        expect([...link.searchParams]).toEqual([
          ["file", filePath],
          ["fullscreen", "1"],
        ]);
        expect(grantMock).not.toHaveBeenCalled();
      } finally {
        Object.defineProperty(window, "location", { configurable: true, value: original });
      }
    });

    it("rebases and transforms file links and preserves the file target in the app QR code", async () => {
      listMock.mockResolvedValue([]);
      const filePath = "artifacts/report #1.html";
      const query = new URLSearchParams({ file: filePath, fullscreen: "1" }).toString();
      const transform = vi.fn((path: string) => `https://host.example.com:8443/embed#${path}`);
      transformLinkMock.mockReturnValue(transform);
      const writeText = vi.fn().mockResolvedValue(undefined);
      Object.assign(navigator, { clipboard: { writeText } });

      render(
        <RoutingProvider value={basenamedRouting("/workspace/omni")}>
          <PermissionsModal
            sessionId="conv_abc"
            filePath={filePath}
            open={true}
            onOpenChange={() => {}}
          />
        </RoutingProvider>,
        { wrapper: createWrapper() },
      );

      fireEvent.click(screen.getByRole("button", { name: /copy file link/i }));
      await waitFor(() => {
        expect(writeText).toHaveBeenCalledWith(
          `https://host.example.com:8443/embed#/workspace/omni/c/conv_abc?${query}`,
        );
      });
      expect(transform).toHaveBeenCalledWith("/workspace/omni/c/conv_abc");
      fireEvent.click(screen.getByRole("button", { name: /open in mobile app/i }));
      expect(await screen.findByTestId("share-qr-code")).toHaveAttribute(
        "data-value",
        `omnigent://host.example.com:8443/c/conv_abc?${query}`,
      );
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

  describe("workspace-files toggle", () => {
    it("is hidden for a session with no workspace", async () => {
      listMock.mockResolvedValue([]);
      useSessionMock.mockReturnValue({
        session: sessionWith({ workspace: null }),
        isLoading: false,
        error: null,
      });

      render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
        wrapper: createInfoWrapper({}),
      });

      await waitFor(() => expect(listMock).toHaveBeenCalled());
      expect(screen.queryByTestId("share-workspace-files")).toBeNull();
    });

    it("turns sharing on for a workspace session that isn't sharing yet", async () => {
      listMock.mockResolvedValue([]);
      useSessionMock.mockReturnValue({
        session: sessionWith({ workspace: "/repo", shareWorkspaceFiles: false }),
        isLoading: false,
        error: null,
      });

      render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
        wrapper: createInfoWrapper({}),
      });

      await waitFor(() => expect(listMock).toHaveBeenCalled());
      const toggle = within(screen.getByTestId("share-workspace-files")).getByRole("switch");
      expect(toggle).toHaveAttribute("aria-checked", "false");

      fireEvent.click(toggle);
      await waitFor(() =>
        expect(updateSessionMock).toHaveBeenCalledWith("conv_abc", { shareWorkspaceFiles: true }),
      );
    });

    it("turns sharing off when it is already on", async () => {
      listMock.mockResolvedValue([]);
      useSessionMock.mockReturnValue({
        session: sessionWith({ workspace: "/repo", shareWorkspaceFiles: true }),
        isLoading: false,
        error: null,
      });

      render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
        wrapper: createInfoWrapper({}),
      });

      await waitFor(() => expect(listMock).toHaveBeenCalled());
      const toggle = within(screen.getByTestId("share-workspace-files")).getByRole("switch");
      expect(toggle).toHaveAttribute("aria-checked", "true");

      fireEvent.click(toggle);
      await waitFor(() =>
        expect(updateSessionMock).toHaveBeenCalledWith("conv_abc", { shareWorkspaceFiles: false }),
      );
    });
  });
});
