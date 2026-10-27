import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { showToast } from "@/components/ui/toast";
import { copyText } from "@/lib/clipboard";
import { authenticatedFetch } from "@/lib/identity";
import { HarnessFixDialog, type HarnessFixDialogProps } from "./HarnessFixDialog";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));
vi.mock("@/lib/clipboard", () => ({ copyText: vi.fn().mockResolvedValue(undefined) }));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({
    databricks_features: false,
    features: { harness_install: true },
    installable_harnesses: [
      "claude-native",
      "codex-native",
      "opencode-native",
      "pi-native",
      "qwen-native",
    ],
  }),
}));

function json(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

const catalog = {
  data: [],
  setup_steps: {
    "codex-native": [
      {
        kind: "auth",
        title: "Sign in",
        detail: "",
        action: "command",
        command: "codex login",
        status_key: "authed",
      },
    ],
    "claude-native": [
      {
        kind: "auth",
        title: "Sign in",
        detail: "",
        action: "command",
        command: "claude auth login --claudeai",
        status_key: "authed",
      },
    ],
    "pi-native": [
      {
        kind: "auth",
        title: "Sign in",
        detail: "",
        action: "command",
        command: null,
        status_key: "authed",
      },
    ],
  },
};

const baseProps: HarnessFixDialogProps = {
  open: true,
  onOpenChange: vi.fn(),
  mode: "reconfigure",
  host: {
    host_id: "host/1",
    name: "Local laptop",
    owner: "me",
    status: "online",
    harness_accounts: {},
    harness_accounts_supported: true,
    harness_login_supported: true,
  },
  harness: "codex-native",
  harnessLabel: "Codex",
  onFixed: vi.fn(),
};
let dialogQueryClient: QueryClient;
let unmountDialog: () => void;

function renderDialog(
  overrides: Partial<HarnessFixDialogProps> = {},
  configureClient?: (client: QueryClient) => void,
) {
  const props = { ...baseProps, ...overrides };
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  client.setQueryData(
    ["detected-credentials", props.host.host_id],
    [{ family: "databricks", source: "isaac CLI", env_var: null }],
  );
  configureClient?.(client);
  dialogQueryClient = client;
  const rendered = render(
    <QueryClientProvider client={client}>
      <HarnessFixDialog {...props} />
    </QueryClientProvider>,
  );
  unmountDialog = rendered.unmount;
  return props;
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
    const url = String(input);
    if (url === "/v1/harnesses") return json(catalog);
    if (url.endsWith("/credentials/detected")) {
      return json({
        credentials: [{ family: "databricks", source: "isaac CLI", env_var: null }],
      });
    }
    throw new Error(`Unexpected URL: ${String(input)}`);
  });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("HarnessFixDialog", () => {
  it.each([
    ["install", "not installed"],
    ["update", "out of date"],
  ] as const)(
    "opens manual setup without an install request when Cursor is %s (%s)",
    async (mode, _state) => {
      renderDialog({ mode, harness: "cursor-native", harnessLabel: "Cursor" });

      expect(screen.getByRole("heading", { name: "Set up Cursor on Local laptop" })).toBeVisible();
      expect(
        screen.getByText((_, element) =>
          Boolean(
            element?.tagName === "P" &&
            element.textContent === "Run omni setup on Local laptop to finish setting up Cursor.",
          ),
        ),
      ).toBeVisible();
      expect(screen.getByRole("button", { name: "Copy omni setup" })).toBeVisible();
      expect(
        screen
          .getAllByRole("button", { name: "Close" })
          .find((button) => button.textContent === "Close"),
      ).toBeVisible();
      expect(screen.getByRole("button", { name: "Check again" })).toBeVisible();
      expect(
        vi
          .mocked(authenticatedFetch)
          .mock.calls.some(([input]) => String(input).endsWith("/install")),
      ).toBe(false);
    },
  );

  it("shows the Isaac setup fallback without calling the credential endpoint", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      throw new Error(`Unexpected URL: ${url} ${init?.method}`);
    });
    renderDialog();
    const user = userEvent.setup();

    expect(screen.getByRole("heading", { name: "Reconfigure Codex" })).toBeVisible();
    expect(screen.getByRole("radio", { name: /Isaac/ })).toHaveAttribute("aria-checked", "true");
    await user.click(screen.getByRole("button", { name: "Authenticate" }));

    expect(
      screen.getByRole("heading", { name: "Set up Isaac for Codex on Local laptop" }),
    ).toBeVisible();
    expect(
      screen.getByText("Run this in a terminal on Local laptop, then come back."),
    ).toBeVisible();
    expect(screen.getByText("omni setup")).toBeVisible();
    expect(
      screen.getByText("Choose Codex, then the Databricks Unity Gateway credential."),
    ).toBeVisible();
    expect(screen.getAllByRole("button", { name: "Close" })).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Check again" })).toBeVisible();
    expect(
      vi
        .mocked(authenticatedFetch)
        .mock.calls.every(([input]) => !String(input).endsWith("/credential")),
    ).toBe(true);
  });

  it("finishes Isaac setup when Check again reports gateway readiness", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/harnesses/refresh")) {
        expect(init).toEqual({ method: "POST" });
        return json({
          configured_harnesses: { "codex-native": true },
          gateway_inference: { "codex-native": true },
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog();
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    await user.click(screen.getByRole("button", { name: "Check again" }));

    await waitFor(() =>
      expect(props.onFixed).toHaveBeenCalledWith({
        configuredHarnesses: { "codex-native": true },
        credentialKind: "isaac",
        gatewayInference: { "codex-native": true },
      }),
    );
    expect(showToast).toHaveBeenCalledWith("Codex is ready on Local laptop");
    expect(props.onOpenChange).toHaveBeenCalledWith(false);
  });

  it("keeps Isaac setup open when Check again is not ready", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/harnesses/refresh")) {
        return json({
          configured_harnesses: { "codex-native": "needs-auth" },
          gateway_inference: { "codex-native": false },
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog();
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    await user.click(screen.getByRole("button", { name: "Check again" }));

    expect(await screen.findByText("Still not signed in on Local laptop.")).toBeVisible();
    expect(props.onFixed).not.toHaveBeenCalled();
    expect(props.onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("shows Claude's server command and succeeds after Check again", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/harnesses/refresh")) {
        expect(init).toEqual({ method: "POST" });
        return json({
          configured_harnesses: { "claude-native": true },
          gateway_inference: null,
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog({ harness: "claude-native", harnessLabel: "Claude Code" });
    const user = userEvent.setup();

    await screen.findByRole("radio", { name: /Personal subscription/ });
    await user.click(await screen.findByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));

    expect(
      screen.getByRole("heading", { name: "Sign in to Claude Code on Local laptop" }),
    ).toBeVisible();
    expect(screen.getByText("claude auth login --claudeai")).toBeVisible();
    expect(
      screen.getByText(
        "Open the link it prints, then paste the code from your browser back into the terminal.",
      ),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Copy login command" }));
    expect(copyText).toHaveBeenCalledWith("claude auth login --claudeai");
    expect(await screen.findByText("Copied")).toBeVisible();
    expect(showToast).toHaveBeenCalledWith("Copied to clipboard.");
    await user.click(screen.getByRole("button", { name: "Check again" }));

    await waitFor(() =>
      expect(props.onFixed).toHaveBeenCalledWith({
        configuredHarnesses: { "claude-native": true },
        credentialKind: "personal",
        gatewayInference: null,
      }),
    );
  });

  it("shows persistent guidance when copying a command fails", async () => {
    vi.mocked(copyText).mockRejectedValueOnce(new Error("clipboard unavailable"));
    renderDialog();
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    await user.click(screen.getByRole("button", { name: "Copy omni setup" }));

    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith("Couldn't copy — select and copy manually.", {
        duration: 0,
      }),
    );
    expect(screen.getByRole("button", { name: "Copy omni setup" })).toHaveTextContent("Copy");
  });

  it("falls back to the Codex copy command when device login is unsupported", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/login")) {
        return json(
          { error: { code: "login_unsupported", message: "Update the host." } },
          { status: 409 },
        );
      }
      if (url.endsWith("/harnesses/refresh")) {
        return json({
          configured_harnesses: { "codex-native": "needs-auth" },
          gateway_inference: { "codex-native": false },
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog();
    const user = userEvent.setup();

    await screen.findByRole("radio", { name: /Personal subscription/ });
    await user.click(screen.getByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));

    expect(screen.getByText("codex login --device-auth")).toBeVisible();
    expect(screen.getByText("Open the link it prints and enter the one-time code.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Check again" }));
    expect(await screen.findByText("Still not signed in on Local laptop.")).toBeVisible();
    expect(props.onFixed).not.toHaveBeenCalled();
    expect(props.onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("skips device login for an older server without explicit login support", async () => {
    const olderHost = { ...baseProps.host };
    delete olderHost.harness_login_supported;
    const user = userEvent.setup();
    renderDialog({ host: olderHost });

    await user.click(screen.getByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));

    expect(screen.getByText("codex login --device-auth")).toBeVisible();
    expect(
      vi.mocked(authenticatedFetch).mock.calls.some(([input]) => String(input).endsWith("/login")),
    ).toBe(false);
  });

  it("shows, polls, and completes Codex device-code sign-in", async () => {
    let loginChecks = 0;
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/login") && init?.method === "POST") {
        return json({
          login_id: "login/1",
          verification_url: "https://auth.example.test/device",
          user_code: "WQ7K-3XPM",
          expires_at: Math.floor(Date.now() / 1000) + 15 * 60,
        });
      }
      if (url.endsWith("/login/login%2F1")) {
        loginChecks += 1;
        return json(
          loginChecks === 1
            ? { status: "pending", error: null }
            : {
                status: "succeeded",
                error: null,
                configured_harnesses: { "codex-native": true },
                harness_accounts: {
                  codex: {
                    kind: "chatgpt",
                    email: "codex@example.com",
                    plan: "Plus",
                  },
                },
              },
        );
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog();
    const user = userEvent.setup();

    await user.click(screen.getByRole("radio", { name: /Personal subscription/ }));
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "Authenticate" }));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText("WQ7K-3XPM")).toBeVisible();
    expect(screen.getByRole("link", { name: /Open sign-in page/ })).toHaveAttribute(
      "href",
      "https://auth.example.test/device",
    );
    fireEvent.click(screen.getByRole("button", { name: "Copy one-time code" }));
    await act(async () => Promise.resolve());
    expect(copyText).toHaveBeenCalledWith("WQ7K-3XPM");

    await act(async () => vi.advanceTimersByTime(2_000));
    await act(async () => vi.advanceTimersByTime(2_000));
    expect(loginChecks).toBe(2);
    expect(props.onFixed).toHaveBeenCalledWith({
      configuredHarnesses: { "codex-native": true },
      credentialKind: "personal",
      harnessAccounts: {
        codex: {
          kind: "chatgpt",
          email: "codex@example.com",
          plan: "Plus",
        },
      },
    });
    expect(showToast).toHaveBeenCalledWith(
      "Codex is ready on Local laptop. Signed in as codex@example.com · Plus.",
    );
  });

  it("cancels an active Codex device login", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/login") && init?.method === "POST") {
        return json({
          login_id: "active",
          verification_url: "https://auth.example.test/device",
          user_code: "ABCD-EFGH",
          expires_at: Math.floor(Date.now() / 1000) + 15 * 60,
        });
      }
      if (url.endsWith("/login/active") && init?.method === "DELETE") {
        return new Response(null, { status: 204 });
      }
      if (url.endsWith("/login/active")) return json({ status: "pending", error: null });
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog();
    const user = userEvent.setup();

    await user.click(screen.getByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    await screen.findByText("ABCD-EFGH");
    const cancel = screen.getByRole("button", { name: "Cancel" });
    expect(cancel).toBeEnabled();
    await user.click(cancel);

    expect(authenticatedFetch).toHaveBeenCalledWith(
      "/v1/hosts/host%2F1/harnesses/codex/login/active",
      { method: "DELETE" },
    );
    expect(props.onOpenChange).toHaveBeenCalledWith(false);
  });

  it("cancels a device login after a polling transport error", async () => {
    let deleteCalls = 0;
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/login") && init?.method === "POST") {
        return json({
          login_id: "poll-error",
          verification_url: "https://auth.example.test/device",
          user_code: "POLL-ERR",
          expires_at: Math.floor(Date.now() / 1000) + 900,
        });
      }
      if (url.endsWith("/login/poll-error") && init?.method === "DELETE") {
        deleteCalls += 1;
        return new Response(null, { status: 204 });
      }
      if (url.endsWith("/login/poll-error")) throw new Error("Connection lost");
      throw new Error(`Unexpected URL: ${url}`);
    });
    const user = userEvent.setup();
    renderDialog();

    await user.click(screen.getByRole("radio", { name: /Personal subscription/ }));
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "Authenticate" }));
    await act(async () => Promise.resolve());
    await act(async () => vi.advanceTimersByTime(2_000));
    expect(screen.getByText("Connection lost")).toBeVisible();

    fireEvent.click(screen.getAllByRole("button", { name: "Close" })[0]);
    await act(async () => Promise.resolve());

    expect(deleteCalls).toBe(1);
  });

  it("cancels a stale login result after the dialog closes during start", async () => {
    let resolveStart: ((response: Response) => void) | undefined;
    const startResponse = new Promise<Response>((resolve) => {
      resolveStart = resolve;
    });
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/login") && init?.method === "POST") return startResponse;
      if (url.endsWith("/login/stale") && init?.method === "DELETE") {
        return new Response(null, { status: 204 });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const user = userEvent.setup();
    renderDialog();

    await user.click(screen.getByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    await user.click(screen.getByRole("button", { name: "Close" }));
    await act(async () =>
      resolveStart?.(
        json({
          login_id: "stale",
          verification_url: "https://auth.example.test/device",
          user_code: "STALE",
          expires_at: Math.floor(Date.now() / 1000) + 900,
        }),
      ),
    );

    await waitFor(() =>
      expect(authenticatedFetch).toHaveBeenCalledWith(
        "/v1/hosts/host%2F1/harnesses/codex/login/stale",
        { method: "DELETE" },
      ),
    );
  });

  it("cancels an active login when the dialog unmounts", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/login") && init?.method === "POST") {
        return json({
          login_id: "unmounted",
          verification_url: "https://auth.example.test/device",
          user_code: "UNMOUNT",
          expires_at: Math.floor(Date.now() / 1000) + 900,
        });
      }
      if (url.endsWith("/login/unmounted") && init?.method === "DELETE") {
        return new Response(null, { status: 204 });
      }
      if (url.endsWith("/login/unmounted")) return json({ status: "pending", error: null });
      throw new Error(`Unexpected URL: ${url}`);
    });
    const user = userEvent.setup();
    renderDialog();

    await user.click(screen.getByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    await screen.findByText("UNMOUNT");
    unmountDialog();

    await waitFor(() =>
      expect(authenticatedFetch).toHaveBeenCalledWith(
        "/v1/hosts/host%2F1/harnesses/codex/login/unmounted",
        { method: "DELETE" },
      ),
    );
  });

  it("rejects an unsafe device link before rendering link or copy actions", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/login") && init?.method === "POST") {
        return json({
          login_id: "unsafe",
          verification_url: "javascript:alert(1)",
          user_code: "UNSAFE",
          expires_at: Math.floor(Date.now() / 1000) + 900,
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const user = userEvent.setup();
    renderDialog();

    await user.click(screen.getByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));

    expect(await screen.findByText("The host returned an invalid sign-in link.")).toBeVisible();
    expect(screen.queryByRole("link", { name: /Open sign-in page/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Copy sign-in link" })).toBeNull();
  });

  it("never overlaps login polls or completes twice", async () => {
    let statusChecks = 0;
    let resolveRefresh: ((response: Response) => void) | undefined;
    const refreshResponse = new Promise<Response>((resolve) => {
      resolveRefresh = resolve;
    });
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/login") && init?.method === "POST") {
        return json({
          login_id: "single-poll",
          verification_url: "https://auth.example.test/device",
          user_code: "SINGLE",
          expires_at: Math.floor(Date.now() / 1000) + 900,
        });
      }
      if (url.endsWith("/login/single-poll")) {
        statusChecks += 1;
        return json({ status: "succeeded", error: null });
      }
      if (url.endsWith("/harnesses/refresh")) return refreshResponse;
      throw new Error(`Unexpected URL: ${url}`);
    });
    const user = userEvent.setup();
    const props = renderDialog();

    await user.click(screen.getByRole("radio", { name: /Personal subscription/ }));
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "Authenticate" }));
    await act(async () => Promise.resolve());
    await act(async () => vi.advanceTimersByTime(2_000));
    await act(async () => vi.advanceTimersByTime(10_000));
    expect(statusChecks).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(props.onOpenChange).toHaveBeenCalledWith(false);
    await act(async () =>
      resolveRefresh?.(
        json({
          configured_harnesses: { "codex-native": true },
          harness_accounts: { codex: { kind: "chatgpt", email: "new@example.test" } },
        }),
      ),
    );

    expect(props.onFixed).not.toHaveBeenCalled();
    expect(showToast).not.toHaveBeenCalled();
  });

  it("resumes and can cancel a busy Codex login", async () => {
    let loginChecks = 0;
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/login") && init?.method === "POST") {
        return json({ code: "login_busy", login_id: "busy/1" }, { status: 409 });
      }
      if (url.endsWith("/login/busy%2F1") && init?.method === "DELETE") {
        return new Response(null, { status: 204 });
      }
      if (url.endsWith("/login/busy%2F1")) {
        loginChecks += 1;
        return json({ status: "pending", error: null });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog({ harness: "native-codex" });
    const user = userEvent.setup();

    await user.click(screen.getByRole("radio", { name: /Personal subscription/ }));
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "Authenticate" }));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(
      screen.getByText(
        "Another sign-in is already in progress on Local laptop. Waiting for it to finish…",
      ),
    ).toBeVisible();
    await act(async () => vi.advanceTimersByTime(2_000));
    expect(loginChecks).toBe(1);

    const cancel = screen.getByRole("button", { name: "Cancel" });
    expect(cancel).toBeEnabled();
    fireEvent.click(cancel);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(authenticatedFetch).toHaveBeenCalledWith(
      "/v1/hosts/host%2F1/harnesses/codex/login/busy%2F1",
      { method: "DELETE" },
    );
    expect(props.onOpenChange).toHaveBeenCalledWith(false);
  });

  it("shows a device-code error and retries", async () => {
    let starts = 0;
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/login") && init?.method === "POST") {
        starts += 1;
        return json({
          login_id: `attempt-${starts}`,
          verification_url: "https://auth.example.test/device",
          user_code: "RETRY-ME",
          expires_at: Math.floor(Date.now() / 1000) + 15 * 60,
        });
      }
      if (url.endsWith("/login/attempt-1")) {
        return json({ status: "failed", error: "The code was rejected." });
      }
      if (url.endsWith("/login/attempt-2")) return json({ status: "pending", error: null });
      throw new Error(`Unexpected URL: ${url}`);
    });
    const user = userEvent.setup();
    renderDialog();

    await user.click(screen.getByRole("radio", { name: /Personal subscription/ }));
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "Authenticate" }));
    await act(async () => Promise.resolve());
    await act(async () => vi.advanceTimersByTime(2_000));
    expect(screen.getByText("The code was rejected.")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await act(async () => Promise.resolve());
    expect(starts).toBe(2);
    expect(screen.getByText("RETRY-ME")).toBeVisible();
  });

  it("confirms Claude switching, logs out, and adds the email hint", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/logout") && init?.method === "POST") {
        return json({
          configured_harnesses: { "claude-native": "needs-auth" },
          harness_accounts: {},
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const user = userEvent.setup();
    const host = {
      ...baseProps.host,
      configured_harnesses: { "claude-native": true },
      harness_accounts: {
        claude: {
          kind: "subscription" as const,
          email: "old@example.com",
          plan: "Pro",
        },
      },
    };
    renderDialog({
      mode: "switch",
      harness: "claude-native",
      harnessLabel: "Claude Code",
      host,
    });
    dialogQueryClient.setQueryData(["hosts", { includeSandbox: false }], [host]);

    expect(screen.getByText(/Every session on Local laptop/)).toBeVisible();
    expect(screen.getByText("old@example.com · Pro")).toBeVisible();
    expect(screen.getByRole("dialog")).toHaveClass("text-foreground");
    const email = screen.getByRole("textbox", { name: "Account to sign in with" });
    expect(email).toHaveValue("");
    expect(email).toHaveAttribute("placeholder", "name@example.com");
    await user.type(email, "new@example.com");
    await user.click(screen.getByRole("button", { name: "Sign out and switch" }));

    expect(
      await screen.findByText("claude auth login --claudeai --email new@example.com"),
    ).toBeVisible();
    expect(authenticatedFetch).toHaveBeenCalledWith("/v1/hosts/host%2F1/harnesses/claude/logout", {
      method: "POST",
    });
    expect(dialogQueryClient.getQueryData(["hosts", { includeSandbox: false }])).toEqual([
      {
        ...host,
        configured_harnesses: { "claude-native": "needs-auth" },
        harness_accounts: {},
      },
    ]);
  });

  it("keeps cached readiness and accounts when logout snapshots are null", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/logout") && init?.method === "POST") {
        return json({ configured_harnesses: null, harness_accounts: null });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const host = {
      ...baseProps.host,
      configured_harnesses: { "claude-native": true },
      harness_accounts: {
        claude: { kind: "subscription" as const, email: "old@example.com", plan: "Pro" },
      },
    };
    renderDialog({
      mode: "switch",
      harness: "claude-native",
      harnessLabel: "Claude Code",
      host,
    });
    dialogQueryClient.setQueryData(["hosts", { includeSandbox: false }], [host]);
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Sign out and switch" }));

    expect(await screen.findByText("claude auth login --claudeai")).toBeVisible();
    expect(dialogQueryClient.getQueryData(["hosts", { includeSandbox: false }])).toEqual([host]);
  });

  it("falls back to the copy command when switch logout is unsupported", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/logout") && init?.method === "POST") {
        return json(
          { error: { code: "login_unsupported", message: "Update the host." } },
          { status: 409 },
        );
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const user = userEvent.setup();
    renderDialog({ mode: "switch", harness: "claude-native", harnessLabel: "Claude Code" });

    await user.click(screen.getByRole("button", { name: "Sign out and switch" }));

    expect(await screen.findByText("claude auth login --claudeai")).toBeVisible();
    expect(screen.queryByText("Update the host.")).toBeNull();
  });

  it("ignores a stale switch error after the dialog closes", async () => {
    let resolveLogout: ((response: Response) => void) | undefined;
    const logoutResponse = new Promise<Response>((resolve) => {
      resolveLogout = resolve;
    });
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/logout") && init?.method === "POST") return logoutResponse;
      throw new Error(`Unexpected URL: ${url}`);
    });
    const user = userEvent.setup();
    renderDialog({ mode: "switch", harness: "claude-native", harnessLabel: "Claude Code" });

    await user.click(screen.getByRole("button", { name: "Sign out and switch" }));
    await user.click(screen.getByRole("button", { name: "Close" }));
    await act(async () =>
      resolveLogout?.(
        json({ error: { code: "login_unsupported", message: "late failure" } }, { status: 409 }),
      ),
    );

    expect(screen.queryByText("late failure")).toBeNull();
    expect(screen.queryByText("claude auth login --claudeai")).toBeNull();
  });

  it("starts Codex device sign-in after switch logout", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/logout") && init?.method === "POST") {
        return json({
          configured_harnesses: { "codex-native": "needs-auth" },
          harness_accounts: {},
        });
      }
      if (url.endsWith("/login") && init?.method === "POST") {
        return json({
          login_id: "switched",
          verification_url: "https://auth.example.test/device",
          user_code: "NEW-LOGIN",
          expires_at: Math.floor(Date.now() / 1000) + 15 * 60,
        });
      }
      if (url.endsWith("/login/switched")) return json({ status: "pending", error: null });
      throw new Error(`Unexpected URL: ${url}`);
    });
    const user = userEvent.setup();
    renderDialog({ mode: "switch" });

    await user.click(screen.getByRole("button", { name: "Sign out and switch" }));
    expect(await screen.findByText("NEW-LOGIN")).toBeVisible();
  });

  it.each([
    [
      "unsupported",
      {
        error: {
          code: "refresh_unsupported",
          message: "This host cannot refresh harness readiness immediately.",
        },
      },
      409,
    ],
    ["a gateway timeout", { detail: "host readiness refresh timed out" }, 504],
  ])("falls back to the hosts query when immediate refresh is %s", async (_case, body, status) => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/harnesses/refresh")) {
        return json(body, { status });
      }
      if (url === "/v1/hosts") {
        return json({
          hosts: [
            {
              host_id: "host/1",
              name: "Local laptop",
              owner: "me",
              status: "online",
              configured_harnesses: { "claude-native": true },
              gateway_inference: { "claude-native": false },
            },
          ],
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const user = userEvent.setup();
    const props = renderDialog({ harness: "claude-native", harnessLabel: "Claude Code" });

    await screen.findByRole("radio", { name: /Personal subscription/ });
    await user.click(screen.getByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    await user.click(screen.getByRole("button", { name: "Check again" }));

    await waitFor(() =>
      expect(props.onFixed).toHaveBeenCalledWith({
        configuredHarnesses: { "claude-native": true },
        credentialKind: "personal",
        gatewayInference: { "claude-native": false },
      }),
    );
  });

  it("labels a gateway-backed refresh as Isaac even from the personal phase", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/harnesses/refresh")) {
        return json({
          configured_harnesses: { "claude-native": true },
          gateway_inference: { "claude-native": true },
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog({ harness: "claude-native", harnessLabel: "Claude Code" });
    const user = userEvent.setup();

    await user.click(await screen.findByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    await user.click(screen.getByRole("button", { name: "Check again" }));

    await waitFor(() =>
      expect(props.onFixed).toHaveBeenCalledWith({
        configuredHarnesses: { "claude-native": true },
        credentialKind: "isaac",
        gatewayInference: { "claude-native": true },
      }),
    );
  });

  it("preserves the selected credential after an error and Try again", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/harnesses/refresh")) {
        return json({ detail: "Refresh failed." }, { status: 500 });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    renderDialog({ harness: "claude-native", harnessLabel: "Claude Code" });
    const user = userEvent.setup();

    await screen.findByRole("radio", { name: /Personal subscription/ });
    await user.click(await screen.findByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    await user.click(screen.getByRole("button", { name: "Check again" }));
    await user.click(await screen.findByRole("button", { name: "Try again" }));

    expect(screen.getByRole("radio", { name: /Personal subscription/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("updates with a bodyless install request and accepts a null version", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/install")) {
        expect(init).toEqual({ method: "POST" });
        return json({
          object: "harness_install",
          harness: "opencode-native",
          configured_harnesses: { "opencode-native": true },
          version: null,
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog({
      mode: "update",
      harness: "opencode-native",
      harnessLabel: "OpenCode",
    });

    expect(screen.getByRole("heading", { name: "Updating OpenCode…" })).toBeVisible();
    expect(
      screen.getByText(/Updating OpenCode to the latest version on Local laptop/),
    ).toBeVisible();
    await waitFor(() =>
      expect(props.onFixed).toHaveBeenCalledWith({
        configuredHarnesses: { "opencode-native": true },
        credentialKind: undefined,
      }),
    );
    expect(authenticatedFetch).toHaveBeenCalledWith(
      "/v1/hosts/host%2F1/harnesses/opencode-native/install",
      { method: "POST" },
    );
  });

  it("installs without a body and then shows the selected Isaac setup", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/install")) {
        expect(init).toEqual({ method: "POST" });
        return json({
          object: "harness_install",
          harness: "codex-native",
          configured_harnesses: { "codex-native": "needs-auth" },
          version: "1.0.0",
        });
      }
      if (url.endsWith("/harnesses/refresh")) {
        expect(init).toEqual({ method: "POST" });
        return json({
          configured_harnesses: { "codex-native": true },
          gateway_inference: { "codex-native": true },
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog({ mode: "install" });
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Install" }));
    expect(
      await screen.findByRole("heading", { name: "Set up Isaac for Codex on Local laptop" }),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Check again" }));
    await waitFor(() => expect(props.onFixed).toHaveBeenCalled());
    expect(props.onFixed).toHaveBeenCalledWith({
      configuredHarnesses: { "codex-native": true },
      credentialKind: "isaac",
      gatewayInference: { "codex-native": true },
    });
  });

  it.each([
    ["claude-native", "Claude Code"],
    ["codex-native", "Codex"],
    ["pi-native", "Pi"],
  ])("offers and preselects Isaac for %s when Databricks is detected", (harness, harnessLabel) => {
    renderDialog({ harness, harnessLabel });

    expect(screen.getByRole("radio", { name: /Isaac/ })).toHaveAttribute("aria-checked", "true");
    if (harness === "pi-native") {
      expect(screen.queryByRole("radio", { name: /Personal subscription/ })).toBeNull();
    }
  });

  it.each([
    ["without a Databricks entry", [], "Personal subscription"],
    [
      "with a Databricks entry",
      [{ family: "databricks", source: "isaac CLI", env_var: null }],
      "Isaac",
    ],
  ])(
    "settles the default %s without starting a duplicate update",
    async (_case, credentials, expectedChoice) => {
      let resolveDetection: ((response: Response) => void) | undefined;
      const detection = new Promise<Response>((resolve) => {
        resolveDetection = resolve;
      });
      let installRequests = 0;
      vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
        const url = String(input);
        if (url === "/v1/harnesses") return json(catalog);
        if (url.endsWith("/credentials/detected")) return detection;
        if (url.endsWith("/install") && init?.method === "POST") {
          installRequests += 1;
          return json({
            object: "harness_install",
            harness: "codex-native",
            configured_harnesses: { "codex-native": "needs-auth" },
            version: "2.0.0",
          });
        }
        throw new Error(`Unexpected URL: ${url}`);
      });

      renderDialog({ mode: "update" }, (client) => {
        client.removeQueries({ queryKey: ["detected-credentials"] });
      });

      await waitFor(() => expect(installRequests).toBe(1));
      expect(
        screen.getByText("Checking which credentials are available on Local laptop…"),
      ).toBeVisible();
      expect(screen.queryByRole("radio", { name: /Isaac/ })).toBeNull();
      await act(async () => resolveDetection?.(json({ credentials })));

      expect(
        await screen.findByRole("radio", { name: new RegExp(expectedChoice) }),
      ).toHaveAttribute("aria-checked", "true");
      await waitFor(() => expect(installRequests).toBe(1));
    },
  );

  it("keeps an active device login when background detection resolves", async () => {
    let resolveDetection: ((response: Response) => void) | undefined;
    const detection = new Promise<Response>((resolve) => {
      resolveDetection = resolve;
    });
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/credentials/detected")) return detection;
      if (url.endsWith("/login") && init?.method === "POST") {
        return json({
          login_id: "active-during-detection",
          verification_url: "https://auth.example.test/device",
          user_code: "KEEP-ME",
          expires_at: Math.floor(Date.now() / 1000) + 15 * 60,
        });
      }
      if (url.endsWith("/login/active-during-detection")) {
        return json({ status: "pending", error: null });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const user = userEvent.setup();
    renderDialog({}, (client) => {
      client.setQueryData(["detected-credentials", "host/1"], []);
      void client.invalidateQueries({
        queryKey: ["detected-credentials", "host/1"],
        refetchType: "none",
      });
    });

    expect(screen.getByRole("radio", { name: /Personal subscription/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    expect(await screen.findByText("KEEP-ME")).toBeVisible();

    await act(async () =>
      resolveDetection?.(
        json({
          credentials: [{ family: "databricks", source: "isaac CLI", env_var: null }],
        }),
      ),
    );
    expect(screen.getByText("KEEP-ME")).toBeVisible();
  });

  it("never renders the Claude credential form before detection and the catalog load", async () => {
    let resolveCatalog: ((response: Response) => void) | undefined;
    const catalogResponse = new Promise<Response>((resolve) => {
      resolveCatalog = resolve;
    });
    let resolveDetection: ((response: Response) => void) | undefined;
    const detectionResponse = new Promise<Response>((resolve) => {
      resolveDetection = resolve;
    });
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return catalogResponse;
      if (url.endsWith("/credentials/detected")) return detectionResponse;
      throw new Error(`Unexpected URL: ${url}`);
    });

    renderDialog({ harness: "claude-native", harnessLabel: "Claude Code" }, (client) => {
      client.removeQueries({ queryKey: ["detected-credentials"] });
    });

    expect(screen.queryByTestId("harness-credential-form")).not.toBeInTheDocument();
    expect(
      screen.getByText("Checking which credentials are available on Local laptop…"),
    ).toBeVisible();
    await act(async () => resolveDetection?.(json({ credentials: [] })));
    expect(await screen.findByRole("radio", { name: /Personal subscription/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.queryByTestId("harness-credential-form")).not.toBeInTheDocument();

    await act(async () => resolveCatalog?.(json(catalog)));
  });

  it("does not flash an Isaac choice for Pi while detection is pending", async () => {
    let resolveDetection: ((response: Response) => void) | undefined;
    const detection = new Promise<Response>((resolve) => {
      resolveDetection = resolve;
    });
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/credentials/detected")) return detection;
      throw new Error(`Unexpected URL: ${url}`);
    });
    renderDialog({ harness: "pi-native", harnessLabel: "Pi" }, (client) => {
      client.removeQueries({ queryKey: ["detected-credentials"] });
    });

    expect(
      screen.getByText("Checking which credentials are available on Local laptop…"),
    ).toBeVisible();
    expect(screen.queryByRole("radio", { name: /Isaac/ })).toBeNull();
    await act(async () => resolveDetection?.(json({ credentials: [] })));

    expect(await screen.findByText("Use an API key")).toBeVisible();
    expect(screen.queryByRole("radio", { name: /Isaac/ })).toBeNull();
  });

  it("preselects Personal subscription when Databricks is not detected", async () => {
    renderDialog({}, (client) => {
      client.setQueryData(["detected-credentials", baseProps.host.host_id], []);
    });

    expect(await screen.findByRole("radio", { name: /Personal subscription/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.queryByRole("radio", { name: /Isaac/ })).not.toBeInTheDocument();
  });

  it("falls back to Isaac when detection is unavailable", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/credentials/detected")) {
        return json({ detail: "unsupported" }, { status: 404 });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    renderDialog({}, (client) => {
      client.removeQueries({ queryKey: ["detected-credentials"] });
    });

    expect(await screen.findByRole("radio", { name: /Isaac/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("keeps Isaac available when an older host cannot report accounts", async () => {
    const olderHost = { ...baseProps.host };
    delete olderHost.harness_accounts_supported;

    renderDialog({ host: olderHost }, (client) => {
      client.setQueryData(["detected-credentials", olderHost.host_id], []);
    });

    expect(await screen.findByRole("radio", { name: /Isaac/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("does not offer credential forms before installing a harness", () => {
    renderDialog({ mode: "install", harness: "pi-native", harnessLabel: "Pi" });

    expect(screen.getByRole("radio", { name: /Isaac/ })).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Use an API key or gateway URL instead" }),
    ).not.toBeInTheDocument();
  });

  it("keeps API key and gateway forms reachable through the secondary link", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/credentials/detected")) return json({ credentials: [] });
      throw new Error(`Unexpected URL: ${url}`);
    });
    renderDialog();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Use an API key or gateway URL instead" }));

    expect(screen.getByText("Use an API key")).toBeVisible();
    expect(screen.getByText("Connect a gateway")).toBeVisible();
    expect(screen.getByPlaceholderText("Paste an API key")).toBeVisible();
    expect(screen.getByPlaceholderText(/Gateway base URL/)).toBeVisible();
    expect(screen.queryByText("Sign in with your subscription")).not.toBeInTheDocument();
  });

  it("installs a cardless harness before showing the omni setup fallback", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/install")) {
        expect(init).toEqual({ method: "POST" });
        return json({
          object: "harness_install",
          harness: "opencode-native",
          configured_harnesses: { "opencode-native": "needs-auth" },
          version: "1.2.3",
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    renderDialog({ mode: "install", harness: "opencode-native", harnessLabel: "OpenCode" });
    const user = userEvent.setup();

    expect(screen.getByRole("heading", { name: "Install OpenCode" })).toBeVisible();
    expect(screen.getByText("Install OpenCode on Local laptop.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Install" }));

    expect(
      await screen.findByRole("heading", { name: "Set up OpenCode on Local laptop" }),
    ).toBeVisible();
    expect(screen.getAllByText("omni setup")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Check again" })).toBeVisible();
    expect(
      vi
        .mocked(authenticatedFetch)
        .mock.calls.some(([input]) => String(input).endsWith("/install")),
    ).toBe(true);
  });

  it("never shows credential forms for a cardless harness", () => {
    renderDialog({ harness: "qwen-native", harnessLabel: "Qwen" });

    expect(screen.getByRole("heading", { name: "Set up Qwen on Local laptop" })).toBeVisible();
    expect(screen.getAllByText("omni setup")).toHaveLength(2);
    expect(screen.queryByText("Use an API key")).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Paste an API key")).not.toBeInTheDocument();
  });

  it("routes an updated but unready harness back to credential choice", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/install")) {
        return json({
          object: "harness_install",
          harness: "codex-native",
          configured_harnesses: { "codex-native": "needs-auth" },
          version: "2.0.0",
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog({ mode: "update" });

    expect(
      await screen.findByText("Codex was updated but still needs to be set up on Local laptop."),
    ).toBeVisible();
    expect(screen.getByRole("radio", { name: /Isaac/ })).toBeVisible();
    expect(props.onFixed).not.toHaveBeenCalled();
  });

  it("passes the installed version to the ready notice", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/install")) {
        return json({
          object: "harness_install",
          harness: "opencode-native",
          configured_harnesses: { "opencode-native": true },
          version: "2.4.0",
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog({
      mode: "update",
      harness: "opencode-native",
      harnessLabel: "OpenCode",
    });

    await waitFor(() =>
      expect(props.onFixed).toHaveBeenCalledWith({
        configuredHarnesses: { "opencode-native": true },
        credentialKind: undefined,
        version: "2.4.0",
      }),
    );
  });

  it("uses the credential response directly without refreshing readiness", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/credentials/detected")) return json({ credentials: [] });
      if (url.endsWith("/credential")) {
        expect(init?.method).toBe("POST");
        return json({
          object: "harness_credential",
          harness: "codex-native",
          configured_harnesses: { "codex-native": true },
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Use an API key or gateway URL instead" }));
    await user.type(screen.getByPlaceholderText("Paste an API key"), "secret");
    await user.click(screen.getAllByRole("button", { name: "Save" })[0]);

    await waitFor(() =>
      expect(props.onFixed).toHaveBeenCalledWith({
        configuredHarnesses: { "codex-native": true },
        credentialKind: undefined,
      }),
    );
    expect(showToast).toHaveBeenCalledTimes(1);
    expect(showToast).toHaveBeenCalledWith("Codex is ready on Local laptop.");
    expect(
      vi
        .mocked(authenticatedFetch)
        .mock.calls.every(([input]) => !String(input).endsWith("/refresh")),
    ).toBe(true);
  });

  it("disables Check again while the readiness request is in flight", async () => {
    let resolveRefresh: ((response: Response) => void) | undefined;
    const refresh = new Promise<Response>((resolve) => {
      resolveRefresh = resolve;
    });
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/harnesses/refresh")) return refresh;
      throw new Error(`Unexpected URL: ${url}`);
    });
    renderDialog();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    await user.click(screen.getByRole("button", { name: "Check again" }));

    expect(screen.getByRole("button", { name: "Checking…" })).toBeDisabled();
    expect(
      vi
        .mocked(authenticatedFetch)
        .mock.calls.filter(([input]) => String(input).endsWith("/harnesses/refresh")),
    ).toHaveLength(1);
    resolveRefresh?.(
      json({
        configured_harnesses: { "codex-native": "needs-auth" },
        gateway_inference: null,
      }),
    );
    expect(await screen.findByRole("button", { name: "Check again" })).toBeEnabled();
  });

  it("includes reversed native aliases in credential families", async () => {
    renderDialog({ harness: "native-codex", harnessLabel: "Codex" });
    expect(screen.getByRole("radio", { name: /Isaac/ })).toBeVisible();
    expect(screen.getByRole("radio", { name: /Personal subscription/ })).toBeVisible();
  });
});
