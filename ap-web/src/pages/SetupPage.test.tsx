// Tests for SetupPage (first-run "Create admin" page, shown when /v1/info
// reports needs_setup).
//
// Mirrors LoginPage.test.tsx's window.location capture. The page calls setup()
// from accountsApi; success hard-navigates to "/", a 409 (someone else claimed
// admin first) bounces to "/login", and other failures show a role="alert".

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SetupPage } from "./SetupPage";
import * as accountsApi from "@/lib/accountsApi";

vi.mock("@/lib/accountsApi", () => ({ setup: vi.fn() }));

const ORIGIN = "https://app.example.com";
let hrefWrites: string[];
let originalLocation: Location;

function renderSetup() {
  return render(
    <MemoryRouter initialEntries={["/setup"]}>
      <SetupPage />
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
        return hrefWrites[hrefWrites.length - 1] ?? `${ORIGIN}/setup`;
      },
    },
  });
  vi.mocked(accountsApi.setup).mockResolvedValue({ ok: true });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  Object.defineProperty(window, "location", { configurable: true, value: originalLocation });
});

describe("SetupPage", () => {
  it("disables submit until a username and an 8+ char password are entered", () => {
    renderSetup();
    const submit = screen.getByRole("button", { name: /create admin/i });
    expect(submit).toBeDisabled();

    fillForm("root", "short", "short");
    expect(submit).toBeDisabled();

    fillForm("root", "longenough", "longenough");
    expect(submit).toBeEnabled();
  });

  it("blocks submit and shows an error when the passwords don't match", () => {
    renderSetup();
    fillForm("root", "longenough", "different1");
    fireEvent.click(screen.getByRole("button", { name: /create admin/i }));

    expect(screen.getByRole("alert")).toHaveTextContent("Passwords don't match.");
    expect(accountsApi.setup).not.toHaveBeenCalled();
  });

  it("claims the admin and navigates home on success", async () => {
    renderSetup();
    fillForm("root", "longenough", "longenough");
    fireEvent.click(screen.getByRole("button", { name: /create admin/i }));

    await waitFor(() =>
      expect(accountsApi.setup).toHaveBeenCalledWith({
        username: "root",
        password: "longenough",
      }),
    );
    await waitFor(() => expect(hrefWrites[0]).toBe("/"));
  });

  it("bounces to /login when setup 409s (admin already claimed)", async () => {
    vi.mocked(accountsApi.setup).mockResolvedValue({
      ok: false,
      error: "setup already completed",
      status: 409,
    });
    renderSetup();
    fillForm("root", "longenough", "longenough");
    fireEvent.click(screen.getByRole("button", { name: /create admin/i }));

    await waitFor(() => expect(hrefWrites[0]).toBe("/login"));
  });

  it("surfaces a non-409 error inline without navigating", async () => {
    vi.mocked(accountsApi.setup).mockResolvedValue({ ok: false, error: "weak password" });
    renderSetup();
    fillForm("root", "longenough", "longenough");
    fireEvent.click(screen.getByRole("button", { name: /create admin/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent("weak password");
    expect(hrefWrites).toHaveLength(0);
  });
});
