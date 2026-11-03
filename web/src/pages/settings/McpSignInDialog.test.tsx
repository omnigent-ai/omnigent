import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { cancelMcpLogin, isSafeSignInUrl, type McpLogin } from "@/hooks/useMcpLogin";
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
    expect(screen.getByRole("button", { name: "Copy sign-in link" })).toBeTruthy();
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
    [502, { detail: "private host text" }, "Couldn't reach my-laptop."],
  ])("explains a %s from the server", async (status, body, message) => {
    on("POST", BASE, () => json(body, status));
    await renderDialog();
    expect(screen.getByRole("alert").textContent).toBe(message);
    expect(screen.queryByText("private host text")).toBeNull();
  });

  it("reports a sign-in that disappeared from the host", async () => {
    on("POST", BASE, () => json(login()));
    on("GET", `${BASE}/L1`, () => json({ detail: "not found" }, 404));
    await renderDialog();
    await tick();
    expect(screen.getByRole("alert").textContent).toBe("This sign-in ended on the host.");
  });

  it("keeps polling through a brief outage and gives up after repeated failures", async () => {
    on("POST", BASE, () => json(login()));
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
    expect(screen.getByRole("alert").textContent).toBe("Couldn't reach my-laptop.");
    const polls = calls("GET", `${BASE}/L1`).length;
    await tick();
    expect(calls("GET", `${BASE}/L1`)).toHaveLength(polls);
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
    ["http://[::1]:8123/authorize", true],
    ["http://auth.example.test/authorize", false],
    ["https://user:pass@auth.example.test/", false],
    ["javascript:alert(1)", false],
    ["not a url", false],
  ])("%s → %s", (url, safe) => {
    expect(isSafeSignInUrl(url)).toBe(safe);
  });
});
