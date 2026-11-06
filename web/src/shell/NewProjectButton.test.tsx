import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { NewProjectButton } from "./NewProjectButton";

vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({
    data: [
      { host_id: "host_laptop", name: "laptop", status: "online", sandbox_provider: null },
      { host_id: "host_box", name: "sandbox", status: "online", sandbox_provider: "modal" },
    ],
  }),
}));

const CLONE_URL = "git@github.com:acme/widgets.git";

function renderButton(onCreated = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <NewProjectButton onCreated={onCreated} />
      </TooltipProvider>
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByTestId("new-project"));
  return onCreated;
}

function requestsTo(fetchMock: ReturnType<typeof vi.fn>, suffix: string) {
  return fetchMock.mock.calls
    .filter(([url]) => String(url).endsWith(suffix))
    .map(([, init]) => JSON.parse((init as RequestInit).body as string));
}

describe("NewProjectButton clone section", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("clones first, then creates the project with the checkout as its default", async () => {
    fetchMock.mockImplementation(async (url: string) =>
      url.endsWith("/repositories")
        ? new Response(
            JSON.stringify({
              object: "repository_clone",
              path: "/Users/me/widgets",
              name: "widgets",
              reused_existing: false,
            }),
          )
        : new Response(JSON.stringify({ id: "proj_1", name: "widgets", config: {} })),
    );
    const onCreated = renderButton();

    fireEvent.click(screen.getByTestId("new-project-clone-toggle"));
    // Sandbox hosts already clone by URL at launch, so only real hosts are offered.
    expect(screen.getByTestId("new-project-clone-host")).toHaveTextContent("laptop");
    fireEvent.change(screen.getByTestId("clone-repository-url"), {
      target: { value: CLONE_URL },
    });
    expect(screen.getByTestId("new-project-name")).toHaveAttribute(
      "placeholder",
      "Project name (default: widgets)",
    );
    fireEvent.click(screen.getByTestId("new-project-confirm"));

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("widgets"));
    expect(requestsTo(fetchMock, "/v1/hosts/host_laptop/repositories")).toEqual([
      { url: CLONE_URL, parent_directory: "~" },
    ]);
    expect(requestsTo(fetchMock, "/v1/projects")).toEqual([
      { name: "widgets", config: { host_id: "host_laptop", workspace: "/Users/me/widgets" } },
    ]);
  });

  it("creates no project when the clone fails", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "/Users/me/widgets already exists." }), {
        status: 409,
      }),
    );
    const onCreated = renderButton();

    fireEvent.change(screen.getByTestId("new-project-name"), { target: { value: "Widgets" } });
    fireEvent.click(screen.getByTestId("new-project-clone-toggle"));
    fireEvent.change(screen.getByTestId("clone-repository-url"), {
      target: { value: CLONE_URL },
    });
    fireEvent.click(screen.getByTestId("new-project-confirm"));

    expect(await screen.findByTestId("new-project-clone-error")).toHaveTextContent(
      "/Users/me/widgets already exists.",
    );
    expect(requestsTo(fetchMock, "/v1/projects")).toEqual([]);
    expect(onCreated).not.toHaveBeenCalled();
  });

  it("still creates a plain project without the clone section", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ id: "proj_2", name: "Notes", config: {} })),
    );
    const onCreated = renderButton();

    fireEvent.change(screen.getByTestId("new-project-name"), { target: { value: "Notes" } });
    fireEvent.click(screen.getByTestId("new-project-confirm"));

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("Notes"));
    expect(requestsTo(fetchMock, "/v1/projects")).toEqual([{ name: "Notes" }]);
    expect(requestsTo(fetchMock, "/repositories")).toEqual([]);
  });
});
