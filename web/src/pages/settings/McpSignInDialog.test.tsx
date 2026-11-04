import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { copyText } from "@/lib/clipboard";
import { cancelMcpLogin, isSafeSignInUrl, type McpLogin } from "@/lib/mcpLoginApi";
import { authenticatedFetch } from "@/lib/identity";
import {
  MAX_POLL_FAILURES,
  MCP_LOGIN_POLL_MS,
  McpSignInDialog,
  type McpSignInDialogProps,
} from "./McpSignInDialog";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/lib/clipboard", () => ({ copyText: vi.fn().mockResolvedValue(undefined) }));

const AUTH_URL = "https://auth.example.test/authorize?client_id=c&redirect_uri=x";
const BASE = "/v1/hosts/host%2F1/mcp-servers/login";
const OTHER_BASE = "/v1/hosts/host%2F2/mcp-servers/login";
const SERVER_UPDATE = "Update the Omnigent server to sign in to MCP servers from here.";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function login(overrides: Partial<McpLogin> = {}): McpLogin {
  return {
    login_id: "L1",
    state: "pending",
    authorization_url: AUTH_URL,
    error: null,
    ...overrides,
  };
}

type Route = (init: RequestInit | undefined) => Response;
let routes: Record<string, Route[]>;
const fetchMock = vi.mocked(authenticatedFetch);

function on(method: string, path: string, ...responses: Route[]) {
  routes[`${method} ${path}`] = responses;
}

function calls(method: string, path: string) {
  return fetchMock.mock.calls.filter(
    ([url, init]) => url === path && (init?.method ?? "GET") === method,
  );
}

const props: McpSignInDialogProps = {
  open: true,
  onOpenChange: vi.fn(),
  host: { host_id: "host/1", name: "my-laptop", owner: "me", status: "online" },
  harness: "claude",
  harnessLabel: "Claude Code",
  server: "linear",
  serverLabel: "Linear",
  plugin: "toolkit",
  sourceId: "a".repeat(64),
  onSignedIn: vi.fn(),
};

async function renderDialog(overrides: Partial<McpSignInDialogProps> = {}) {
  const view = render(<McpSignInDialog {...props} {...overrides} />);
  await act(async () => {});
  return view;
}

// Moves the dialog to host/2, which starts sign-in L2; host/1 accepts a cancel of L1.
async function switchToSecondHost(view: ReturnType<typeof render>) {
  on("DELETE", `${BASE}/L1`, () => new Response(null, { status: 204 }));
  on("POST", OTHER_BASE, () => json(login({ login_id: "L2" })));
  on("GET", `${OTHER_BASE}/L2`, () => json(login({ login_id: "L2" })));
  view.rerender(<McpSignInDialog {...props} host={{ ...props.host, host_id: "host/2" }} />);
  await act(async () => {});
}

async function tick() {
  await act(async () => vi.advanceTimersByTime(MCP_LOGIN_POLL_MS));
}

beforeEach(() => {
  vi.useFakeTimers();
  routes = {};
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (input, init) => {
    const key = `${init?.method ?? "GET"} ${String(input)}`;
    const queue = routes[key];
    if (!queue?.length) throw new Error(`unexpected ${key}`);
    const route = queue.length > 1 ? queue.shift()! : queue[0];
    return route(init);
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("McpSignInDialog", () => {
  it("starts the harness sign-in, links the page, and reports success", async () => {
    on("POST", BASE, () => json(login()));
    on(
      "GET",
      `${BASE}/L1`,
      () => json(login()),
      () => json(login({ state: "succeeded", authorization_url: null })),
    );
    await renderDialog();

    expect(JSON.parse(String(calls("POST", BASE)[0][1]?.body))).toEqual({
      harness: "claude",
      server: "linear",
      plugin: "toolkit",
      source_id: "a".repeat(64),
    });
    const link = screen.getByRole("link", { name: /Open sign-in page/ });
    expect(link.getAttribute("href")).toBe(AUTH_URL);
    expect(link.getAttribute("target")).toBe("_blank");
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Copy sign-in link" })),
    );
    expect(copyText).toHaveBeenCalledWith(AUTH_URL);
    expect(screen.getByText("Waiting for you to finish in the browser…")).toBeTruthy();

    await tick();
    expect(props.onSignedIn).not.toHaveBeenCalled();
    await tick();
    expect(props.onSignedIn).toHaveBeenCalledTimes(1);
    await tick();
    expect(calls("GET", `${BASE}/L1`)).toHaveLength(2);
  });

  it("keeps the pending sign-in when the host is renamed", async () => {
    on("POST", BASE, () => json(login()));
    on("GET", `${BASE}/L1`, () => json(login()));
    const view = await renderDialog();

    view.rerender(<McpSignInDialog {...props} host={{ ...props.host, name: "work-laptop" }} />);
    await tick();

    expect(calls("POST", BASE)).toHaveLength(1);
    expect(calls("DELETE", `${BASE}/L1`)).toHaveLength(0);
    expect(screen.getByRole("link", { name: /Open sign-in page/ })).toBeTruthy();
  });

  it("moves a starting sign-in to the newly chosen host", async () => {
    let respond: (response: Response) => void = () => {};
    fetchMock.mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          respond = resolve;
        }),
    );
    const view = await renderDialog();

    await switchToSecondHost(view);
    await act(async () => respond(json(login())));
    await tick();

    expect(calls("DELETE", `${BASE}/L1`)).toHaveLength(1);
    expect(calls("GET", `${BASE}/L1`)).toHaveLength(0);
    expect(calls("POST", OTHER_BASE)).toHaveLength(1);
    expect(calls("GET", `${OTHER_BASE}/L2`)).toHaveLength(1);
    expect(screen.getByRole("link", { name: /Open sign-in page/ })).toBeTruthy();
  });

  it("cancels a pending sign-in on its own host when another host is chosen", async () => {
    on("POST", BASE, () => json(login()));
    on("GET", `${BASE}/L1`, () => json(login()));
    const view = await renderDialog();
    await tick();

    await switchToSecondHost(view);
    await tick();

    expect(fetchMock.mock.calls.map(([url, init]) => `${init?.method ?? "GET"} ${url}`)).toEqual([
      `POST ${BASE}`,
      `GET ${BASE}/L1`,
      `DELETE ${BASE}/L1`,
      `POST ${OTHER_BASE}`,
      `GET ${OTHER_BASE}/L2`,
    ]);
  });

  it("sends a pasted redirect address to the host and shows its rejection", async () => {
    on("POST", BASE, () => json(login()));
    on("GET", `${BASE}/L1`, () => json(login()));
    on(
      "POST",
      `${BASE}/L1/callback`,
      () => json({ detail: "That isn't the address the sign-in page redirected to" }, 400),
      () => json(login()),
    );
    await renderDialog();

    const input = screen.getByLabelText(/paste it here/);
    const finish = screen.getByRole("button", { name: "Finish" });
    expect(finish).toBeDisabled();
    fireEvent.change(input, { target: { value: " http://localhost:9/wrong " } });
    await act(async () => fireEvent.click(finish));
    expect(screen.getByRole("alert").textContent).toBe(
      "That isn't the address the sign-in page redirected to",
    );

    fireEvent.change(input, { target: { value: "http://localhost:9/callback?code=c&state=s" } });
    await act(async () => fireEvent.click(finish));
    const bodies = calls("POST", `${BASE}/L1/callback`).map(([, init]) =>
      JSON.parse(String(init?.body)),
    );
    expect(bodies).toEqual([
      { callback_url: "http://localhost:9/wrong" },
      { callback_url: "http://localhost:9/callback?code=c&state=s" },
    ]);
    expect(screen.queryByRole("alert")).toBeNull();
    expect((input as HTMLInputElement).value).toBe("");
    expect(finish).toBeDisabled();
  });

  it("ends the sign-in on the host's reply to a pasted address", async () => {
    const succeeded = () => json(login({ state: "succeeded", authorization_url: null }));
    on("POST", BASE, () => json(login()));
    on("POST", `${BASE}/L1/callback`, succeeded);
    const view = await renderDialog();
    let respond: (response: Response) => void = () => {};
    fetchMock.mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          respond = resolve;
        }),
    );
    await tick();

    fireEvent.change(screen.getByLabelText(/paste it here/), {
      target: { value: "http://localhost:9/callback?code=c&state=s" },
    });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Finish" })));
    expect(props.onSignedIn).toHaveBeenCalledTimes(1);

    // The poll that was already in flight sees the same ending.
    await act(async () => respond(succeeded()));
    await tick();
    expect(props.onSignedIn).toHaveBeenCalledTimes(1);
    expect(calls("GET", `${BASE}/L1`)).toHaveLength(1);
    view.unmount();
    expect(calls("DELETE", `${BASE}/L1`)).toHaveLength(0);
  });

  it.each([
    [
      "the dialog closed",
      async (view: ReturnType<typeof render>) => {
        on("DELETE", `${BASE}/L1`, () => new Response(null, { status: 204 }));
        view.rerender(<McpSignInDialog {...props} open={false} />);
        await act(async () => {});
      },
      0,
    ],
    ["another host was chosen", switchToSecondHost, 1],
  ] as const)("ignores the reply to a pasted address once %s", async (_, leave, links) => {
    on("POST", BASE, () => json(login()));
    const view = await renderDialog();
    let respond: (response: Response) => void = () => {};
    fetchMock.mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          respond = resolve;
        }),
    );
    fireEvent.change(screen.getByLabelText(/paste it here/), {
      target: { value: "http://localhost:9/callback?code=c&state=s" },
    });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Finish" })));

    await leave(view);
    await act(async () => respond(json(login({ state: "succeeded", authorization_url: null }))));
    await tick();

    expect(props.onSignedIn).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryAllByRole("link", { name: /Open sign-in page/ })).toHaveLength(links);
    expect(calls("DELETE", `${BASE}/L1`)).toHaveLength(1);
  });

  it("shows why the sign-in ended and starts a fresh one on retry", async () => {
    on(
      "POST",
      BASE,
      () => json(login()),
      () => json(login({ login_id: "L2" })),
    );
    on("GET", `${BASE}/L1`, () =>
      json(
        login({
          state: "failed",
          authorization_url: null,
          error: "The harness couldn't finish signing in",
        }),
      ),
    );
    on("GET", `${BASE}/L2`, () => json(login({ login_id: "L2" })));
    await renderDialog();
    await tick();

    expect(screen.getByRole("alert").textContent).toBe("The harness couldn't finish signing in");
    expect(screen.queryByRole("link")).toBeNull();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Try again" })));
    expect(calls("POST", BASE)).toHaveLength(2);
    expect(screen.getByRole("link", { name: /Open sign-in page/ })).toBeTruthy();
    expect(props.onSignedIn).not.toHaveBeenCalled();
  });

  it.each([
    ["expired", "Sign-in timed out."],
    ["cancelled", "Sign-in was cancelled."],
  ] as const)("explains a %s sign-in", async (state, message) => {
    on("POST", BASE, () => json(login({ state, authorization_url: null })));
    await renderDialog();
    expect(screen.getByRole("alert").textContent).toBe(message);
  });

  it("cancels a pending sign-in when the dialog closes", async () => {
    on("POST", BASE, () => json(login()));
    on("DELETE", `${BASE}/L1`, () => new Response(null, { status: 204 }));
    const view = await renderDialog();
    view.rerender(<McpSignInDialog {...props} open={false} />);
    await act(async () => {});
    expect(calls("DELETE", `${BASE}/L1`)).toHaveLength(1);
    await tick();
    expect(calls("GET", `${BASE}/L1`)).toHaveLength(0);
  });

  it("cancels a sign-in that starts after the dialog closed", async () => {
    let respond: (response: Response) => void = () => {};
    fetchMock.mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          respond = resolve;
        }),
    );
    on("DELETE", `${BASE}/L1`, () => new Response(null, { status: 204 }));
    const view = await renderDialog();
    view.rerender(<McpSignInDialog {...props} open={false} />);
    await act(async () => respond(json(login())));
    expect(calls("DELETE", `${BASE}/L1`)).toHaveLength(1);
    await tick();
    expect(calls("GET", `${BASE}/L1`)).toHaveLength(0);
    expect(props.onSignedIn).not.toHaveBeenCalled();
  });

  it("ignores a status reply that arrives after the dialog closed", async () => {
    on("POST", BASE, () => json(login()));
    on("DELETE", `${BASE}/L1`, () => new Response(null, { status: 204 }));
    const view = await renderDialog();
    let respond: (response: Response) => void = () => {};
    fetchMock.mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          respond = resolve;
        }),
    );
    await tick();

    view.rerender(<McpSignInDialog {...props} open={false} />);
    await act(async () => respond(json(login({ state: "succeeded", authorization_url: null }))));
    await tick();

    expect(props.onSignedIn).not.toHaveBeenCalled();
    expect(calls("GET", `${BASE}/L1`)).toHaveLength(1);
    expect(calls("DELETE", `${BASE}/L1`)).toHaveLength(1);
  });

  it("does not cancel a sign-in that already succeeded", async () => {
    on("POST", BASE, () => json(login({ state: "succeeded", authorization_url: null })));
    const view = await renderDialog();
    expect(props.onSignedIn).toHaveBeenCalledTimes(1);
    view.unmount();
    expect(calls("DELETE", `${BASE}/L1`)).toHaveLength(0);
  });

  it.each([
    [
      501,
      { detail: "update the host to sign in to MCP servers" },
      "Update my-laptop to sign in to MCP servers from here.",
    ],
    [503, { detail: "Too many sign-ins are in progress" }, "Too many sign-ins are in progress"],
    [404, { detail: "MCP server not found" }, "MCP server not found"],
    [502, { detail: "private host text" }, "The sign-in request failed. Try again."],
    [504, { detail: "private host text" }, "Couldn't reach my-laptop."],
  ])("explains a %s from the server", async (status, body, message) => {
    on("POST", BASE, () => json(body, status));
    await renderDialog();
    expect(screen.getByRole("alert").textContent).toBe(message);
    expect(screen.queryByText("private host text")).toBeNull();
  });

  it.each([
    ["the web UI's", { error: { code: "not_found", message: "Not found" } }],
    ["an API-only", { detail: "Not Found" }],
  ])("asks for a server update on %s missing-route 404", async (_server, body) => {
    on("POST", BASE, () => json(body, 404));
    await renderDialog();
    expect(screen.getByRole("alert").textContent).toBe(SERVER_UPDATE);
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("stops a pending sign-in once the server lacks the sign-in routes", async () => {
    const missing = () => json({ error: { code: "not_found", message: "Not found" } }, 404);
    on("POST", BASE, () => json(login()));
    on("POST", `${BASE}/L1/callback`, missing);
    on("GET", `${BASE}/L1`, missing);
    await renderDialog();

    fireEvent.change(screen.getByLabelText(/paste it here/), {
      target: { value: "http://localhost:9/callback?code=c&state=s" },
    });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Finish" })));
    expect(screen.getByRole("alert").textContent).toBe(SERVER_UPDATE);

    await tick();
    expect(screen.getByRole("alert").textContent).toBe(SERVER_UPDATE);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
    await tick();
    expect(calls("GET", `${BASE}/L1`)).toHaveLength(1);
    cleanup();
    expect(calls("DELETE", `${BASE}/L1`)).toHaveLength(0);
  });

  it("reports a sign-in that disappeared from the host", async () => {
    on("POST", BASE, () => json(login()));
    on("GET", `${BASE}/L1`, () => json({ detail: "not found" }, 404));
    await renderDialog();
    await tick();
    expect(screen.getByRole("alert").textContent).toBe("This sign-in ended on the host.");
    cleanup();
    expect(calls("DELETE", `${BASE}/L1`)).toHaveLength(0);
  });

  it("keeps polling through a brief outage and gives up after repeated failures", async () => {
    on(
      "POST",
      BASE,
      () => json(login()),
      () => json(login({ login_id: "L2" })),
    );
    on("DELETE", `${BASE}/L1`, () => new Response(null, { status: 204 }));
    on("GET", `${BASE}/L2`, () => json(login({ login_id: "L2" })));
    on(
      "GET",
      `${BASE}/L1`,
      () => json({ detail: "bad gateway" }, 502),
      () => json(login()),
      ...Array.from({ length: MAX_POLL_FAILURES }, () => () => json({ detail: "x" }, 502)),
    );
    await renderDialog();
    await tick();
    await tick();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("link", { name: /Open sign-in page/ })).toBeTruthy();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(MCP_LOGIN_POLL_MS * (MAX_POLL_FAILURES - 1));
    });
    expect(screen.queryByRole("alert")).toBeNull();
    await tick();
    expect(screen.getByRole("alert").textContent).toBe("The sign-in request failed. Try again.");
    const polls = calls("GET", `${BASE}/L1`).length;
    await tick();
    expect(calls("GET", `${BASE}/L1`)).toHaveLength(polls);

    // The host may still be running that sign-in, so a retry stops it first.
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Try again" })));
    await tick();
    expect(calls("DELETE", `${BASE}/L1`)).toHaveLength(1);
    expect(calls("GET", `${BASE}/L1`)).toHaveLength(polls);
    expect(calls("GET", `${BASE}/L2`)).toHaveLength(1);
    expect(props.onSignedIn).not.toHaveBeenCalled();
  });

  it("refuses to link an unsafe sign-in page and stops that sign-in", async () => {
    on("POST", BASE, () =>
      json(login({ authorization_url: "http://auth.example.test/authorize" })),
    );
    on("DELETE", `${BASE}/L1`, () => new Response(null, { status: 204 }));
    const view = await renderDialog();
    expect(screen.getByRole("alert").textContent).toBe(
      "The host returned an invalid sign-in link.",
    );
    expect(screen.queryByRole("link")).toBeNull();
    expect(calls("DELETE", `${BASE}/L1`)).toHaveLength(1);
    await tick();
    expect(calls("GET", `${BASE}/L1`)).toHaveLength(0);
    view.unmount();
    expect(calls("DELETE", `${BASE}/L1`)).toHaveLength(1);
  });
});

describe("cancelMcpLogin", () => {
  it("reports a cancel the server refused", async () => {
    on("DELETE", `${BASE}/L1`, () => json({ detail: "Host is offline" }, 503));
    await expect(cancelMcpLogin("host/1", "L1")).rejects.toMatchObject({ status: 503 });
  });
});

describe("isSafeSignInUrl", () => {
  it.each([
    ["https://auth.example.test/authorize", true],
    ["http://localhost:8123/authorize", true],
    ["http://127.0.0.1:8123/authorize", true],
    ["http://127.0.0.2:8123/authorize", true],
    ["http://[::1]:8123/authorize", true],
    ["http://auth.example.test/authorize", false],
    ["http://127.0.0.1.example.test/authorize", false],
    ["https://user:pass@auth.example.test/", false],
    ["javascript:alert(1)", false],
    ["not a url", false],
  ])("%s → %s", (url, safe) => {
    expect(isSafeSignInUrl(url)).toBe(safe);
  });
});
