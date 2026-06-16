// Tests for RegisterPage (the /register?invite=… invite-redemption page).
//
// Mirrors LoginPage.test.tsx: window.location.href is stubbed so success
// navigation is observable without jsdom navigating. The page calls
// register() from accountsApi; success hard-navigates to "/", failure shows a
// role="alert". The page also gates on the invite query param (missing → alert,
// no form).

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RegisterPage } from "./RegisterPage";
import * as accountsApi from "@/lib/accountsApi";

vi.mock("@/lib/accountsApi", () => ({ register: vi.fn() }));

const ORIGIN = "https://app.example.com";
let hrefWrites: string[];
let originalLocation: Location;

function renderRegisterAt(search: string) {
  return render(
    <MemoryRouter initialEntries={[`/register${search}`]}>
      <RegisterPage />
    </MemoryRouter>,
  );
}

function fillForm(username: string, password: string, confirm: string) {
  fireEvent.change(screen.getByLabelText(/username/i), { target: { value: username } });
  fireEvent.change(screen.getByLabelText(/^password/i), { target: { value: password } });
  fireEvent.change(screen.getByLabelText(/confirm password/i), { target: { value: confirm } });
}

beforeEach(() => {
  hrefWrites = [];
  originalLocation = window.location;
  Object.defineProperty(window, "location", {
    configurable: true,
    value: {
      origin: ORIGIN,
      set href(value: string) {
        hrefWrites.push(value);
      },
      get href() {
        return hrefWrites[hrefWrites.length - 1] ?? `${ORIGIN}/register`;
      },
    },
  });
  vi.mocked(accountsApi.register).mockResolvedValue({ ok: true });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  Object.defineProperty(window, "location", { configurable: true, value: originalLocation });
});

describe("RegisterPage", () => {
  it("shows an invite-required alert and no form when the invite token is missing", () => {
    renderRegisterAt("");
    expect(screen.getByRole("alert")).toHaveTextContent(/invite token/i);
    expect(screen.queryByLabelText(/username/i)).not.toBeInTheDocument();
  });

  it("renders the form when an invite token is present", () => {
    renderRegisterAt("?invite=tok123");
    expect(screen.getByLabelText(/username/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /create account/i })).toBeInTheDocument();
  });

  it("disables submit until a username and an 8+ char password are entered", () => {
    renderRegisterAt("?invite=tok123");
    const submit = screen.getByRole("button", { name: /create account/i });
    expect(submit).toBeDisabled();

    fillForm("alice", "short", "short");
    expect(submit).toBeDisabled(); // password < 8

    fillForm("alice", "longenough", "longenough");
    expect(submit).toBeEnabled();
  });

  it("blocks submit and shows an error when the passwords don't match", () => {
    renderRegisterAt("?invite=tok123");
    fillForm("alice", "longenough", "different1");
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));

    expect(screen.getByRole("alert")).toHaveTextContent("Passwords don't match.");
    expect(accountsApi.register).not.toHaveBeenCalled();
  });

  it("redeems the invite and navigates home on success", async () => {
    renderRegisterAt("?invite=tok123");
    fillForm("alice", "longenough", "longenough");
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));

    await waitFor(() =>
      expect(accountsApi.register).toHaveBeenCalledWith({
        invite: "tok123",
        username: "alice",
        password: "longenough",
      }),
    );
    await waitFor(() => expect(hrefWrites[0]).toBe("/"));
  });

  it("surfaces the server error on failure and does not navigate", async () => {
    vi.mocked(accountsApi.register).mockResolvedValue({ ok: false, error: "invite expired" });
    renderRegisterAt("?invite=tok123");
    fillForm("alice", "longenough", "longenough");
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent("invite expired");
    expect(hrefWrites).toHaveLength(0);
  });
});
