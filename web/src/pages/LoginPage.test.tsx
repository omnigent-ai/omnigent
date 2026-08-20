import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LoginPage } from "./LoginPage";
import * as accountsApi from "@/lib/accountsApi";

// The authenticated auto-bounce path is the simplest way to observe the
// sanitized return_to reaching the navigation sink: getMe() resolving to an
// account makes the mount effect assign sanitizeReturnTo(return_to) straight
// to window.location.href.
vi.mock("@/lib/accountsApi", () => ({
  getMe: vi.fn(),
  login: vi.fn(),
}));

const ORIGIN = "https://app.example.com";

let hrefWrites: string[];
let originalLocation: Location;

function renderLoginAt(returnTo: string) {
  const path = `/login?return_to=${returnTo}`;
  return render(
    <MemoryRouter initialEntries={[path]}>
      <LoginPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  hrefWrites = [];
  originalLocation = window.location;
  // Capture href writes without navigating jsdom. origin must be defined so
  // sanitizeReturnTo can resolve relative paths against it.
  Object.defineProperty(window, "location", {
    configurable: true,
    value: {
      origin: ORIGIN,
      set href(value: string) {
        hrefWrites.push(value);
      },
      get href() {
        return hrefWrites[hrefWrites.length - 1] ?? `${ORIGIN}/login`;
      },
    },
  });
  vi.mocked(accountsApi.getMe).mockResolvedValue({
    id: "alice",
    is_admin: false,
    created_at: null,
    last_login_at: null,
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  // Restore the real location so the stub never leaks to other test files
  // (several read window.location.origin).
  Object.defineProperty(window, "location", {
    configurable: true,
    value: originalLocation,
  });
});

describe("LoginPage sanitizeReturnTo open-redirect defense", () => {
  it("replaces a backslash protocol-relative payload with the safe default", async () => {
    // %2F%5Cevil.com decodes to /\evil.com — passes a naive
    // startsWith("/") + !startsWith("//") check, but WHATWG URL parsing
    // resolves it to https://evil.com/. Must be neutralized to "/".
    renderLoginAt("%2F%5Cevil.com");
    await waitFor(() => expect(hrefWrites.length).toBeGreaterThan(0));
    expect(hrefWrites[0]).toBe("/");
    expect(hrefWrites[0]).not.toContain("evil.com");
  });

  it.each([
    ["//evil.com", "protocol-relative"],
    ["/%5Cevil.com", "backslash variant (/\\evil.com)"],
    ["https://evil.com", "absolute off-origin URL"],
    ["%5C%5Cevil.com", "double backslash (\\\\evil.com)"],
  ])("rejects %s (%s) → '/'", async (payload) => {
    renderLoginAt(payload);
    await waitFor(() => expect(hrefWrites.length).toBeGreaterThan(0));
    expect(hrefWrites[0]).toBe("/");
    expect(hrefWrites[0]).not.toContain("evil.com");
  });

  it("preserves a legitimate same-origin path with query and fragment", async () => {
    renderLoginAt("%2Fsessions%2Fabc%3Ftab%3Dlogs%23top");
    await waitFor(() => expect(hrefWrites.length).toBeGreaterThan(0));
    expect(hrefWrites[0]).toBe("/sessions/abc?tab=logs#top");
  });

  it("also sanitizes return_to on the post-login success navigation", async () => {
    // The submit sink (L113) is separate from the auto-bounce sink (L81).
    // Force the not-yet-authed branch so the form submit drives navigation,
    // then confirm the malicious return_to is neutralized there too.
    vi.mocked(accountsApi.getMe).mockResolvedValue(null);
    vi.mocked(accountsApi.login).mockResolvedValue({
      ok: true,
      user: { id: "alice", is_admin: false },
      token: "t",
      expires_in: 3600,
    });

    renderLoginAt("%2F%5Cevil.com");
    fireEvent.change(screen.getByLabelText(/username/i), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText(/password/i), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => expect(hrefWrites.length).toBeGreaterThan(0));
    expect(hrefWrites[0]).toBe("/");
    expect(hrefWrites[0]).not.toContain("evil.com");
  });
});

describe("LoginPage forced re-auth (?reauth=1)", () => {
  it("does NOT auto-bounce an already-signed-in user — shows the form", async () => {
    // getMe resolves to an account (beforeEach default), which normally
    // auto-returns to return_to. Under reauth=1 (device-grant consent), that
    // shortcut must be suppressed so the user re-enters their password.
    render(
      <MemoryRouter
        initialEntries={["/login?reauth=1&return_to=%2Foauth%2Fdevice%3Fuser_code%3DABCD"]}
      >
        <LoginPage />
      </MemoryRouter>,
    );
    // The password form is shown…
    await waitFor(() => expect(screen.getByLabelText(/password/i)).toBeInTheDocument());
    // …and no auto-navigation happened despite an existing session.
    expect(hrefWrites).toHaveLength(0);
    expect(accountsApi.getMe).not.toHaveBeenCalled();
  });

  it("navigates to return_to only after a fresh submit under reauth=1", async () => {
    vi.mocked(accountsApi.login).mockResolvedValue({
      ok: true,
      user: { id: "alice", is_admin: false },
      token: "t",
      expires_in: 3600,
    });
    render(
      <MemoryRouter
        initialEntries={["/login?reauth=1&return_to=%2Foauth%2Fdevice%3Fuser_code%3DABCD"]}
      >
        <LoginPage />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByLabelText(/password/i)).toBeInTheDocument());
    expect(hrefWrites).toHaveLength(0); // still no auto-bounce

    fireEvent.change(screen.getByLabelText(/username/i), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText(/password/i), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => expect(hrefWrites.length).toBeGreaterThan(0));
    expect(hrefWrites[0]).toBe("/oauth/device?user_code=ABCD");
  });
});
