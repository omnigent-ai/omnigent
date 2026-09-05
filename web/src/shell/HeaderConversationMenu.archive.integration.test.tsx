import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import type * as ConversationsModule from "@/hooks/useConversations";
import type * as UnseenConversationsModule from "@/hooks/useUnseenConversations";
import { setOmnigentHostConfig } from "@/lib/host";
import { HeaderConversationMenu } from "./HeaderConversationMenu";

const mocks = vi.hoisted(() => ({
  fetch: vi.fn(),
  showToast: vi.fn(),
}));

vi.mock("@/components/ui/toast", () => ({ showToast: mocks.showToast }));

vi.mock("@/hooks/useIsMobileViewport", () => ({ useIsMobileViewport: () => true }));

// Keep the real archive hook and HTTP helper in this component test. Only the
// unrelated menu mutations are replaced so a click exercises the production
// optimistic overlay, rollback, error toast, and PATCH contract together.
vi.mock("@/hooks/useConversations", async (importOriginal) => {
  const actual = await importOriginal<typeof ConversationsModule>();
  return {
    ...actual,
    useProjects: () => ({ data: [] }),
    useTogglePinnedConversation: () => ({ mutate: vi.fn() }),
    useRenameConversation: () => ({ mutate: vi.fn(), isPending: false }),
    useMoveToProject: () => ({ mutate: vi.fn() }),
    useStopAndDeleteConversation: () => ({ mutate: vi.fn(), isPending: false }),
  };
});

vi.mock("@/hooks/useUnseenConversations", async (importOriginal) => {
  const actual = await importOriginal<typeof UnseenConversationsModule>();
  return { ...actual, markConversationUnread: vi.fn() };
});

const CONVERSATION: Conversation = {
  id: "conv-1",
  object: "conversation",
  title: "Quarterly planning",
  created_at: 1_700_000_000,
  updated_at: 1_700_000_100,
  labels: {},
  permission_level: 3,
  git_branch: "feature/quarterly-planning",
  archived: false,
};

const SECOND_CONVERSATION: Conversation = {
  ...CONVERSATION,
  id: "conv-2",
  title: "Release planning",
};

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 200 && status < 300 ? "OK" : "Server Error",
    json: async () => body,
  } as Response;
}

function LocationProbe() {
  return <output data-testid="location-probe">{useLocation().pathname}</output>;
}

function NavigateElsewhere() {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate("/settings/general")}>
      Go elsewhere
    </button>
  );
}

interface HarnessProps {
  client: QueryClient;
  conversation?: Conversation;
  showMenu?: boolean;
}

function harness({ client, conversation = CONVERSATION, showMenu = true }: HarnessProps) {
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/c/${CONVERSATION.id}`]}>
        {showMenu && (
          <HeaderConversationMenu
            conversation={conversation}
            currentProject={null}
            canShare={false}
            onShare={() => {}}
          />
        )}
        <NavigateElsewhere />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function createClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
}

function seedConversation(client: QueryClient, conversation = CONVERSATION) {
  client.setQueryData(["conversations", "", true], {
    pages: [
      {
        data: [conversation],
        first_id: conversation.id,
        last_id: conversation.id,
        has_more: false,
      },
    ],
    pageParams: [undefined],
  });
}

function cachedArchived(client: QueryClient): boolean | undefined {
  const data = client.getQueryData(["conversations", "", true]) as {
    pages: { data: Conversation[] }[];
  };
  return data.pages[0].data[0].archived;
}

function openMenu() {
  fireEvent.pointerDown(screen.getByRole("button", { name: "Conversation actions" }), {
    button: 0,
  });
}

function clickArchive() {
  openMenu();
  fireEvent.click(screen.getByRole("menuitem", { name: "Archive this session" }));
}

async function settleMutation(client: QueryClient) {
  await waitFor(() => {
    expect(client.getMutationCache().getAll().at(-1)?.state.status).not.toBe("pending");
  });
  const mutation = client.getMutationCache().getAll().at(-1);
  if (mutation?.state.status === "error") throw mutation.state.error;
  await act(async () => {
    await Promise.resolve();
  });
}

beforeEach(() => {
  mocks.fetch.mockReset();
  mocks.showToast.mockReset();
  vi.stubGlobal("fetch", mocks.fetch);
  setOmnigentHostConfig({
    serverIdentity: "test-server",
    fetcher: (path, init) => fetch(path, init),
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  setOmnigentHostConfig({});
});

describe("HeaderConversationMenu archive integration", () => {
  it("sends the archive PATCH once, waits for success, confirms it, and returns home", async () => {
    const pending = deferred<Response>();
    mocks.fetch.mockReturnValueOnce(pending.promise);
    const client = createClient();
    seedConversation(client);
    render(harness({ client }));

    clickArchive();

    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledOnce());
    expect(screen.getByTestId("location-probe")).toHaveTextContent("/c/conv-1");
    expect(cachedArchived(client)).toBe(true);

    const [url, init] = mocks.fetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv-1");
    expect(init.method).toBe("PATCH");
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    expect(JSON.parse(init.body as string)).toEqual({ archived: true });

    // Reopening the menu while the request is pending exposes a disabled row;
    // selecting it cannot dispatch a second PATCH.
    openMenu();
    const pendingAction = screen.getByRole("menuitem", { name: "Archive this session" });
    expect(pendingAction).toHaveAttribute("data-disabled");
    fireEvent.click(pendingAction);
    expect(mocks.fetch).toHaveBeenCalledOnce();

    pending.resolve(response({ ...CONVERSATION, archived: true, updated_at: 1_700_000_200 }));

    await waitFor(() => {
      expect(screen.getByTestId("location-probe")).toHaveTextContent("/");
      expect(mocks.showToast).toHaveBeenCalledOnce();
    });
    const toast = mocks.showToast.mock.calls[0][0] as ReactNode;
    const toastView = render(<MemoryRouter>{toast}</MemoryRouter>);
    expect(toastView.container).toHaveTextContent("Session archived. View it in Settings");
  });

  it("stays on the session and restores its cache row when the PATCH fails", async () => {
    const pending = deferred<Response>();
    mocks.fetch.mockReturnValueOnce(pending.promise);
    const client = createClient();
    seedConversation(client);
    render(harness({ client }));

    clickArchive();

    await waitFor(() => {
      expect(mocks.fetch).toHaveBeenCalledOnce();
      expect(cachedArchived(client)).toBe(true);
    });
    pending.resolve(response({ error: "nope" }, 500));

    await waitFor(() => {
      expect(cachedArchived(client)).toBe(false);
      expect(mocks.showToast).toHaveBeenCalledWith(
        "Couldn't archive the session — it's back in the sidebar.",
      );
    });
    expect(screen.getByTestId("location-probe")).toHaveTextContent("/c/conv-1");
  });

  it("does not redirect or announce late success after the user navigates elsewhere", async () => {
    const pending = deferred<Response>();
    mocks.fetch.mockReturnValueOnce(pending.promise);
    const client = createClient();
    render(harness({ client }));

    clickArchive();
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole("button", { name: "Go elsewhere" }));
    expect(screen.getByTestId("location-probe")).toHaveTextContent("/settings/general");

    pending.resolve(response({ ...CONVERSATION, archived: true }));
    await settleMutation(client);

    expect(screen.getByTestId("location-probe")).toHaveTextContent("/settings/general");
    expect(mocks.showToast).not.toHaveBeenCalled();
  });

  it("does not redirect or announce late success after the menu owner unmounts", async () => {
    const pending = deferred<Response>();
    mocks.fetch.mockReturnValueOnce(pending.promise);
    const client = createClient();
    const view = render(harness({ client }));

    clickArchive();
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledOnce());
    view.rerender(harness({ client, showMenu: false }));

    pending.resolve(response({ ...CONVERSATION, archived: true }));
    await settleMutation(client);

    expect(screen.getByTestId("location-probe")).toHaveTextContent("/c/conv-1");
    expect(mocks.showToast).not.toHaveBeenCalled();
  });

  it("does not redirect a newer session when the pending owner changes", async () => {
    const pending = deferred<Response>();
    mocks.fetch.mockReturnValueOnce(pending.promise);
    const client = createClient();
    const view = render(harness({ client }));

    clickArchive();
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledOnce());
    view.rerender(harness({ client, conversation: SECOND_CONVERSATION }));

    pending.resolve(response({ ...CONVERSATION, archived: true }));
    await settleMutation(client);

    expect(screen.getByTestId("location-probe")).toHaveTextContent("/c/conv-1");
    expect(mocks.showToast).not.toHaveBeenCalled();
  });
});
