import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CLONE_UNSUPPORTED_MESSAGE } from "@/hooks/useCloneHostRepository";
import {
  CloneRepositoryDialog,
  cloneDestination,
  readLastCloneParent,
  rememberCloneParent,
} from "./CloneRepository";

const HOST_ID = "host_abc";

function renderWithClient(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

describe("cloneDestination", () => {
  it.each([
    ["https://github.com/acme/widgets.git", "~", "~/widgets"],
    ["git@github.com:acme/widgets.git", "/Users/me/code/", "/Users/me/code/widgets"],
    ["https://github.com/acme/widgets", "C:\\src", "C:\\src\\widgets"],
  ])("joins %s under %s", (url, parent, expected) => {
    expect(cloneDestination(url, parent)).toBe(expected);
  });

  it.each([
    ["https://user:token@github.com/acme/widgets", "~"],
    ["ftp://example.com/widgets", "~"],
    ["https://github.com/acme/widgets", "code"],
  ])("is null for unusable input %s into %s", (url, parent) => {
    expect(cloneDestination(url, parent)).toBeNull();
  });
});

describe("remembered clone folders", () => {
  beforeEach(() => localStorage.clear());

  it.each(['"abc"', "[1, 2]", "null", "{not json"])("ignores and replaces stored %s", (stored) => {
    localStorage.setItem("omnigent:clone-repository-parent", stored);

    expect(readLastCloneParent(HOST_ID)).toBe("~");
    rememberCloneParent(HOST_ID, "~/code");
    expect(JSON.parse(localStorage.getItem("omnigent:clone-repository-parent") ?? "")).toEqual({
      [HOST_ID]: "~/code",
    });
  });
});

describe("CloneRepositoryDialog", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("clones into the chosen folder, reports the checkout, and remembers the folder", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        object: "repository_clone",
        path: "/Users/me/code/widgets",
        name: "widgets",
        reused_existing: false,
      }),
    );
    const onCloned = vi.fn();
    const onOpenChange = vi.fn();
    renderWithClient(
      <CloneRepositoryDialog
        open
        onOpenChange={onOpenChange}
        hostId={HOST_ID}
        onCloned={onCloned}
      />,
    );

    expect(screen.getByTestId("clone-repository-confirm")).toBeDisabled();
    fireEvent.change(screen.getByTestId("clone-repository-url"), {
      target: { value: "git@github.com:acme/widgets.git" },
    });
    fireEvent.change(screen.getByTestId("clone-repository-parent"), {
      target: { value: "~/code" },
    });
    expect(screen.getByTestId("clone-repository-destination")).toHaveTextContent(
      "Clones into ~/code/widgets",
    );
    fireEvent.click(screen.getByTestId("clone-repository-confirm"));

    await waitFor(() =>
      expect(onCloned).toHaveBeenCalledWith({
        path: "/Users/me/code/widgets",
        name: "widgets",
        reusedExisting: false,
      }),
    );
    expect(onOpenChange).toHaveBeenCalledWith(false);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`/v1/hosts/${HOST_ID}/repositories`);
    expect(JSON.parse(init?.body as string)).toEqual({
      url: "git@github.com:acme/widgets.git",
      parent_directory: "~/code",
    });
    expect(readLastCloneParent(HOST_ID)).toBe("~/code");
    expect(readLastCloneParent("other-host")).toBe("~");
  });

  it("keeps the dialog open with the server's reason when the clone fails", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        { detail: "Couldn't clone the repository: Permission denied (publickey)." },
        422,
      ),
    );
    const onCloned = vi.fn();
    const onOpenChange = vi.fn();
    renderWithClient(
      <CloneRepositoryDialog
        open
        onOpenChange={onOpenChange}
        hostId={HOST_ID}
        onCloned={onCloned}
      />,
    );

    fireEvent.change(screen.getByTestId("clone-repository-url"), {
      target: { value: "git@github.com:acme/private.git" },
    });
    fireEvent.keyDown(screen.getByTestId("clone-repository-url"), { key: "Enter" });

    expect(await screen.findByTestId("clone-repository-error")).toHaveTextContent(
      "Permission denied (publickey).",
    );
    expect(onCloned).not.toHaveBeenCalled();
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it.each([
    [404, "Not Found", CLONE_UNSUPPORTED_MESSAGE],
    [405, "Method Not Allowed", CLONE_UNSUPPORTED_MESSAGE],
    [404, "host not found", "host not found"],
  ])("explains a %i %s response", async (status, detail, shown) => {
    fetchMock.mockResolvedValue(jsonResponse({ detail }, status));
    renderWithClient(
      <CloneRepositoryDialog open onOpenChange={vi.fn()} hostId={HOST_ID} onCloned={vi.fn()} />,
    );

    fireEvent.change(screen.getByTestId("clone-repository-url"), {
      target: { value: "https://github.com/acme/widgets" },
    });
    fireEvent.click(screen.getByTestId("clone-repository-confirm"));

    expect(await screen.findByTestId("clone-repository-error")).toHaveTextContent(shown);
  });

  it("flags a URL with an embedded token without sending it", () => {
    renderWithClient(
      <CloneRepositoryDialog open onOpenChange={vi.fn()} hostId={HOST_ID} onCloned={vi.fn()} />,
    );

    fireEvent.change(screen.getByTestId("clone-repository-url"), {
      target: { value: "https://me:ghp_secret@github.com/acme/widgets" },
    });
    fireEvent.keyDown(screen.getByTestId("clone-repository-url"), { key: "Enter" });

    expect(screen.getByTestId("clone-repository-url-error")).toBeInTheDocument();
    expect(screen.getByTestId("clone-repository-confirm")).toBeDisabled();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
