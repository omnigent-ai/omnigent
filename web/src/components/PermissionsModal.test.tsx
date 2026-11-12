import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { ServerInfo, SharingMode } from "@/lib/capabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
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

const ROLE_LABELS = ["Read", "Comment", "Edit", "Manage", "Owner"];

/** Opens the dropdown behind a trigger button and returns the menu. */
async function openMenu(name: string | RegExp): Promise<HTMLElement> {
  const trigger = await screen.findByRole("button", { name });
  await waitFor(() => expect(trigger).toBeEnabled());
  trigger.focus();
  fireEvent.keyDown(trigger, { key: "Enter" });
  return screen.findByRole("menu");
}

/** Role labels a role menu lists, in order, e.g. ``["Read", "Edit"]``. */
function menuRoles(menu: HTMLElement): (string | undefined)[] {
  return within(menu)
    .queryAllByRole("menuitemradio")
    .map((item) => ROLE_LABELS.find((label) => item.textContent?.startsWith(label)));
}

/** Picks the menu item whose text starts with ``label``. */
function chooseItem(menu: HTMLElement, label: string) {
  const item = [
    ...within(menu).queryAllByRole("menuitemradio"),
    ...within(menu).queryAllByRole("menuitem"),
  ].find((candidate) => candidate.textContent?.startsWith(label));
  if (!item) throw new Error(`No menu item ${label}`);
  fireEvent.click(item);
}

async function chooseGeneralAccess(label: "Restricted" | "Anyone with the link") {
  chooseItem(await openMenu("General access"), label);
}

async function removeAccess(userId: string) {
  chooseItem(await openMenu(`Permission level for ${userId}`), "Remove access");
}

function typeNewUser(userId: string) {
  fireEvent.change(screen.getByLabelText("User ID"), { target: { value: userId } });
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
  it.each([
    ["General access", "start"],
    ["Permission level for bob@example.com", "end"],
  ])("opens the %s menu aligned to the %s of its trigger", async (label, align) => {
    listMock.mockResolvedValue([
      { user_id: "bob@example.com", conversation_id: "conv_abc", level: 2 },
    ]);

    render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
      wrapper: createSharingWrapper("on"),
    });

    const menu = await openMenu(label);
    await waitFor(() => {
      expect(menu).toHaveAttribute("data-side", "bottom");
      expect(menu).toHaveAttribute("data-align", align);
    });
    fireEvent.keyDown(menu, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("button", { name: label })).toHaveFocus());
  });

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
    // The role menu and Add button appear once an address is typed.
    expect(screen.queryByRole("button", { name: /^add$/i })).not.toBeInTheDocument();
    typeNewUser("carol@example.com");
    chooseItem(await openMenu("New user permission"), "Edit");
    fireEvent.click(screen.getByRole("button", { name: /^add$/i }));

    await waitFor(() => {
      expect(grantMock).toHaveBeenCalledWith("conv_abc", "carol@example.com", 2);
    });
  });

  it("calls revokePermission from a person's Remove access item", async () => {
    listMock.mockResolvedValue([
      { user_id: "bob@example.com", conversation_id: "conv_abc", level: 1 },
    ]);
    revokeMock.mockResolvedValue(undefined);

    render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
      wrapper: createWrapper(),
    });

    await removeAccess("bob@example.com");

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

    chooseItem(await openMenu("Permission level for bob@example.com"), "Edit");

    await waitFor(() => {
      expect(grantMock).toHaveBeenCalledWith("conv_abc", "bob@example.com", 2);
    });
    // Editing the level must never delete the existing grant.
    expect(revokeMock).not.toHaveBeenCalled();
  });

  it("renders the owner as fixed text with no role menu", async () => {
    listMock.mockResolvedValue([
      { user_id: "owner@example.com", conversation_id: "conv_abc", level: 4 },
      { user_id: "bob@example.com", conversation_id: "conv_abc", level: 1 },
    ]);

    render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(screen.getByText("owner@example.com")).toBeInTheDocument());
    expect(screen.getByText("Owner")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Permission level for owner@example.com" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Permission level for bob@example.com" }),
    ).toBeInTheDocument();
  });

  it("toggles public access via grant/revoke of __public__ sentinel", async () => {
    listMock.mockResolvedValue([]);
    grantMock.mockResolvedValue({ user_id: "__public__", conversation_id: "conv_abc", level: 1 });

    render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(listMock).toHaveBeenCalled());
    expect(screen.getByText("Only people added above can open this session")).toBeInTheDocument();

    await chooseGeneralAccess("Anyone with the link");

    await waitFor(() => {
      expect(grantMock).toHaveBeenCalledWith("conv_abc", "__public__", 1);
    });
  });

  it("describes link access with the link's role", async () => {
    listMock.mockResolvedValue([{ user_id: "__public__", conversation_id: "conv_abc", level: 2 }]);
    render(<PermissionsModal sessionId="conv_abc" open onOpenChange={() => {}} />, {
      wrapper: createInfoWrapper({ public_sharing_max_level: "edit" }),
    });
    expect(await screen.findByText("Everyone with this link can edit")).toBeInTheDocument();
  });

  it("displays server error messages from failed grant", async () => {
    listMock.mockResolvedValue([]);
    grantMock.mockRejectedValue(new Error("'rice' needs manage permission"));

    render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(listMock).toHaveBeenCalled());

    typeNewUser("rice");
    fireEvent.click(screen.getByRole("button", { name: /^add$/i }));

    await waitFor(() => {
      expect(screen.getByText("'rice' needs manage permission")).toBeInTheDocument();
    });
  });

  // Granting manage (level 3) is backend-only. A manager's row offers only
  // Remove access, and no other role menu lists Manage.
  it("never offers Manage in any role menu", async () => {
    listMock.mockResolvedValue([
      { user_id: "owner@example.com", conversation_id: "conv_abc", level: 4 },
      { user_id: "mallory@example.com", conversation_id: "conv_abc", level: 3 },
      { user_id: "bob@example.com", conversation_id: "conv_abc", level: 1 },
    ]);

    render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
      wrapper: createWrapper(),
    });

    const managerMenu = await openMenu("Permission level for mallory@example.com");
    expect(menuRoles(managerMenu)).toEqual([]);
    expect(within(managerMenu).getByRole("menuitem")).toHaveTextContent("Remove access");
    fireEvent.keyDown(managerMenu, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());

    const bobMenu = await openMenu("Permission level for bob@example.com");
    expect(menuRoles(bobMenu)).toEqual(["Read", "Edit"]);
  });

  it("offers Comment only when the server enables comment sharing", async () => {
    listMock.mockResolvedValue([
      { user_id: "bob@example.com", conversation_id: "conv_abc", level: 5 },
    ]);
    render(<PermissionsModal sessionId="conv_abc" open onOpenChange={() => {}} />, {
      wrapper: createInfoWrapper({ comment_sharing_enabled: true }),
    });
    const menu = await openMenu("Permission level for bob@example.com");
    // Comment (level 5) ranks between Read and Edit.
    expect(menuRoles(menu)).toEqual(["Read", "Comment", "Edit"]);
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

    await removeAccess("bob@example.com");

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
      // The grid that stacks the main and settings views must not size its
      // column to the longest email, or rows push their role menus off the edge.
      expect(screen.getByTestId("share-grants").closest(".grid.pr-4")).toHaveClass(
        "grid-cols-[minmax(0,1fr)]",
      );
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

      await waitFor(() => expect(screen.getByText("No one has access yet.")).toBeInTheDocument());
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

    it("keeps the combobox enabled for a home directory on a restricted server", async () => {
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
      expect(input).toBeEnabled();
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
      expect(screen.queryByLabelText("User ID")).not.toBeInTheDocument();
      expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    });

    it.each([
      ["on", false, ["Read", "Edit"]],
      ["on", true, ["Read", "Comment", "Edit"]],
      ["read_only", true, ["Read", "Comment"]],
      ["restricted_read_only", true, ["Read", "Comment"]],
    ] as const)(
      "%s (comment sharing %s): offers %j to new people",
      async (mode, commentSharing, roles) => {
        listMock.mockResolvedValue([]);
        render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
          wrapper: createInfoWrapper({
            sharing_mode: mode,
            comment_sharing_enabled: commentSharing,
          }),
        });
        await waitFor(() => expect(listMock).toHaveBeenCalledWith("conv_abc"));
        typeNewUser("bob");
        expect(menuRoles(await openMenu("New user permission"))).toEqual(roles);
      },
    );

    it("read_only without comment sharing: fixes new people at Read", async () => {
      listMock.mockResolvedValue([]);
      grantMock.mockResolvedValue({ conversation_id: "conv_abc", user_id: "bob", level: 1 });
      render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
        wrapper: createSharingWrapper("read_only"),
      });
      await waitFor(() => expect(listMock).toHaveBeenCalledWith("conv_abc"));
      typeNewUser("bob");
      expect(screen.queryByRole("button", { name: "New user permission" })).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: /^add$/i }));
      await waitFor(() => expect(grantMock).toHaveBeenCalledWith("conv_abc", "bob", 1));
    });

    it("keeps a grant made before the server turned read-only, offering only Read", async () => {
      listMock.mockResolvedValue([
        { user_id: "bob@example.com", conversation_id: "conv_abc", level: 2 },
      ]);
      render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
        wrapper: createSharingWrapper("read_only"),
      });
      const menu = await openMenu("Permission level for bob@example.com");
      expect(menuRoles(menu)).toEqual(["Read", "Edit"]);
      const edit = within(menu)
        .getAllByRole("menuitemradio")
        .find((item) => item.textContent?.startsWith("Edit"));
      expect(edit).toHaveAttribute("aria-checked", "true");
      expect(edit).toHaveAttribute("data-disabled");
      expect(edit).toHaveTextContent("Only read access can be shared on this server");
    });
  });

  describe("home or root folder on a restricted server", () => {
    it.each(["/home/alice", "/Users/bob", "/var/home/carol", "/root", "/"])(
      "shares %s at Read only and never offers file access",
      async (workspace) => {
        listMock.mockResolvedValue([]);
        grantMock.mockResolvedValue({ conversation_id: "conv_abc", user_id: "bob", level: 1 });
        useSessionMock.mockReturnValue({
          session: sessionWith({ workspace }),
          isLoading: false,
          error: null,
        });
        render(<PermissionsModal sessionId="conv_abc" open onOpenChange={() => {}} />, {
          wrapper: createInfoWrapper({
            sharing_mode: "restricted_read_only",
            comment_sharing_enabled: true,
          }),
        });

        const files = screen.getByTestId("share-workspace-files");
        expect(files).toHaveTextContent(
          "File access is disabled for sessions in a home or root folder",
        );
        expect(within(files).queryByRole("button", { name: "Change" })).not.toBeInTheDocument();
        expect(screen.getByRole("switch", { hidden: true })).toBeDisabled();

        typeNewUser("bob");
        // Comment is enabled on the server but still not offered here.
        expect(
          screen.queryByRole("button", { name: "New user permission" }),
        ).not.toBeInTheDocument();
        fireEvent.click(screen.getByRole("button", { name: /^add$/i }));
        await waitFor(() => expect(grantMock).toHaveBeenCalledWith("conv_abc", "bob", 1));
      },
    );

    it("treats a project folder in a home directory like any other session", async () => {
      listMock.mockResolvedValue([]);
      useSessionMock.mockReturnValue({
        session: sessionWith({ workspace: "/home/alice/project" }),
        isLoading: false,
        error: null,
      });
      render(<PermissionsModal sessionId="conv_abc" open onOpenChange={() => {}} />, {
        wrapper: createInfoWrapper({
          sharing_mode: "restricted_read_only",
          comment_sharing_enabled: true,
        }),
      });
      expect(screen.getByTestId("share-workspace-files")).toHaveTextContent(
        "File access is disabled for users with read access",
      );
      typeNewUser("bob");
      expect(menuRoles(await openMenu("New user permission"))).toEqual(["Read", "Comment"]);
    });

    it.each(["on", "read_only"] as const)(
      "does not restrict home directories in %s mode",
      async (mode) => {
        listMock.mockResolvedValue([]);
        useSessionMock.mockReturnValue({
          session: sessionWith({ workspace: "/home/alice" }),
          isLoading: false,
          error: null,
        });
        render(<PermissionsModal sessionId="conv_abc" open onOpenChange={() => {}} />, {
          wrapper: createInfoWrapper({ sharing_mode: mode, comment_sharing_enabled: true }),
        });
        expect(screen.getByTestId("share-workspace-files")).toHaveTextContent(
          "File access is disabled for users with read access",
        );
        typeNewUser("bob");
        expect(menuRoles(await openMenu("New user permission"))).toContain("Comment");
      },
    );

    it("detects the folder from the session when no workspace prop is passed", async () => {
      listMock.mockResolvedValue([
        { conversation_id: "conv_abc", user_id: "bob", level: 2 },
        { conversation_id: "conv_abc", user_id: "__public__", level: 1 },
      ]);
      revokeMock.mockResolvedValue(undefined);
      useSessionMock.mockReturnValue({
        session: sessionWith({ workspace: "/home/alice" }),
        isLoading: false,
        error: null,
      });
      render(<PermissionsModal sessionId="conv_abc" open onOpenChange={() => {}} />, {
        wrapper: createSharingWrapper("restricted_read_only"),
      });
      // An earlier Edit grant stays visible and removable; Read is the only other choice.
      const menu = await openMenu("Permission level for bob");
      expect(menuRoles(menu)).toEqual(["Read", "Edit"]);
      chooseItem(menu, "Remove access");
      await waitFor(() => expect(revokeMock).toHaveBeenCalledWith("conv_abc", "bob"));
      await chooseGeneralAccess("Restricted");
      await waitFor(() => expect(revokeMock).toHaveBeenCalledWith("conv_abc", "__public__"));
      expect(grantMock).not.toHaveBeenCalled();
    });
  });

  describe("link access", () => {
    it("disables link sharing when the server turns public access off", async () => {
      listMock.mockResolvedValue([]);

      render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
        wrapper: createInfoWrapper({ public_sharing_enabled: false }),
      });

      await waitFor(() => expect(listMock).toHaveBeenCalledWith("conv_abc"));
      // Adding people stays available; only the link control is off.
      expect(screen.getByLabelText("User ID")).toBeEnabled();
      expect(screen.getByRole("button", { name: "General access" })).toBeDisabled();
      expect(
        screen.getByText("Link sharing is turned off on this Omnigent server"),
      ).toBeInTheDocument();
    });

    it("grants public Edit only when the server explicitly permits it", async () => {
      listMock.mockResolvedValue([
        { user_id: "__public__", conversation_id: "conv_abc", level: 1 },
      ]);
      grantMock.mockResolvedValue({ user_id: "__public__", conversation_id: "conv_abc", level: 2 });
      render(<PermissionsModal sessionId="conv_abc" open onOpenChange={() => {}} />, {
        wrapper: createInfoWrapper({ public_sharing_max_level: "edit" }),
      });
      chooseItem(await openMenu("Link access permission"), "Edit");
      await waitFor(() => expect(grantMock).toHaveBeenCalledWith("conv_abc", "__public__", 2));
    });

    it("changes only the __public__ grant when link access is turned on", async () => {
      listMock.mockResolvedValue([
        { user_id: "editor@example.com", conversation_id: "conv_abc", level: 2 },
        { user_id: "reader@example.com", conversation_id: "conv_abc", level: 1 },
      ]);
      grantMock.mockResolvedValue({ user_id: "__public__", conversation_id: "conv_abc", level: 1 });
      render(<PermissionsModal sessionId="conv_abc" open onOpenChange={() => {}} />, {
        wrapper: createInfoWrapper({ public_sharing_max_level: "read" }),
      });
      await screen.findByText("editor@example.com");
      await chooseGeneralAccess("Anyone with the link");
      await waitFor(() => expect(grantMock).toHaveBeenCalled());
      expect(grantMock.mock.calls).toEqual([["conv_abc", "__public__", 1]]);
      // Adding people stays available while link access is on.
      expect(screen.getByLabelText("User ID")).toBeEnabled();
    });

    it.each([undefined, "read"] as const)(
      "fixes link access at Read under a %s ceiling",
      async (level) => {
        listMock.mockResolvedValue([
          { user_id: "__public__", conversation_id: "conv_abc", level: 1 },
        ]);
        render(<PermissionsModal sessionId="conv_abc" open onOpenChange={() => {}} />, {
          wrapper: createInfoWrapper({ public_sharing_max_level: level }),
        });
        expect(await screen.findByText("Everyone with this link can read")).toBeInTheDocument();
        expect(
          screen.queryByRole("button", { name: "Link access permission" }),
        ).not.toBeInTheDocument();
      },
    );

    it("caps link roles at Comment on a read-only server", async () => {
      listMock.mockResolvedValue([
        { user_id: "__public__", conversation_id: "conv_abc", level: 1 },
      ]);
      render(<PermissionsModal sessionId="conv_abc" open onOpenChange={() => {}} />, {
        wrapper: createInfoWrapper({
          sharing_mode: "read_only",
          public_sharing_max_level: "edit",
          comment_sharing_enabled: true,
        }),
      });
      expect(menuRoles(await openMenu("Link access permission"))).toEqual(["Read", "Comment"]);
    });

    it("keeps an existing public grant revocable when public sharing is disabled", async () => {
      listMock.mockResolvedValue([
        { user_id: "__public__", conversation_id: "conv_abc", level: 2 },
      ]);
      revokeMock.mockResolvedValue(undefined);
      render(<PermissionsModal sessionId="conv_abc" open onOpenChange={() => {}} />, {
        wrapper: createInfoWrapper({ public_sharing_enabled: false }),
      });
      await chooseGeneralAccess("Restricted");
      await waitFor(() => expect(revokeMock).toHaveBeenCalledWith("conv_abc", "__public__"));
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

  describe("workspace-files setting", () => {
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
      expect(screen.queryByRole("button", { name: "Share settings" })).toBeNull();
    });

    it("turns sharing on from the settings view", async () => {
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
      expect(screen.getByTestId("share-workspace-files")).toHaveTextContent(
        "File access is disabled for users with read access",
      );
      fireEvent.click(screen.getByRole("button", { name: "Change" }));
      const toggle = screen.getByRole("switch", {
        name: "Users with read access can browse workspace files",
      });
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
      expect(screen.getByTestId("share-workspace-files")).toHaveTextContent(
        "File access is enabled for users with read access",
      );
      fireEvent.click(screen.getByRole("button", { name: "Share settings" }));
      const toggle = screen.getByRole("switch");
      expect(toggle).toHaveAttribute("aria-checked", "true");

      fireEvent.click(toggle);
      await waitFor(() =>
        expect(updateSessionMock).toHaveBeenCalledWith("conv_abc", { shareWorkspaceFiles: false }),
      );
    });

    it.each([
      [false, "Edit access always include workspace files"],
      [true, "Comment and edit access always include workspace files"],
    ])(
      "explains which roles always include files (comment sharing %s)",
      async (commentSharing, text) => {
        listMock.mockResolvedValue([]);
        useSessionMock.mockReturnValue({
          session: sessionWith({ workspace: "/repo", shareWorkspaceFiles: true }),
          isLoading: false,
          error: null,
        });
        render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
          wrapper: createInfoWrapper({ comment_sharing_enabled: commentSharing }),
        });
        fireEvent.click(await screen.findByRole("button", { name: "Share settings" }));
        expect(screen.getByRole("switch")).toHaveAccessibleDescription(
          new RegExp(`added above or through the link\\. ${text}`),
        );
      },
    );
  });

  it("reports a failed permission load instead of an empty list", async () => {
    listMock.mockRejectedValueOnce(new Error("permissions unavailable"));
    render(<PermissionsModal sessionId="conv_abc" open={true} onOpenChange={() => {}} />, {
      wrapper: createWrapper(),
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("permissions unavailable");
    expect(screen.queryByText("No one has access yet.")).not.toBeInTheDocument();
    expect(screen.getByLabelText("User ID")).toBeDisabled();

    listMock.mockResolvedValue([
      { user_id: "bob@example.com", conversation_id: "conv_abc", level: 1 },
    ]);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("bob@example.com")).toBeInTheDocument();
    expect(screen.getByLabelText("User ID")).toBeEnabled();
  });
});
