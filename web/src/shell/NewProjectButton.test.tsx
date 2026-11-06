import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { NewProjectButton } from "./NewProjectButton";

interface MockHost {
  host_id: string;
  name: string;
  status: string;
  sandbox_provider: string | null;
}

const hostsMock = vi.hoisted(() => ({ data: undefined as MockHost[] | undefined }));
vi.mock("@/hooks/useHosts", () => ({ useHosts: () => ({ data: hostsMock.data }) }));

const LAPTOP: MockHost = {
  host_id: "host_laptop",
  name: "laptop",
  status: "online",
  sandbox_provider: null,
};
const DESKTOP: MockHost = {
  host_id: "host_desktop",
  name: "desktop",
  status: "online",
  sandbox_provider: null,
};
const SANDBOX: MockHost = {
  host_id: "host_box",
  name: "sandbox",
  status: "online",
  sandbox_provider: "modal",
};
const CLONE_URL = "git@github.com:acme/widgets.git";
const CLONE_SERVER: ServerInfo = { ...FALLBACK_SERVER_INFO, host_repository_clone: true };

function renderButton(info: ServerInfo = CLONE_SERVER) {
  const onCreated = vi.fn();
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  const tree = () => (
    <QueryClientProvider client={client}>
      <CapabilitiesProvider info={info}>
        <TooltipProvider>
          <NewProjectButton onCreated={onCreated} />
        </TooltipProvider>
      </CapabilitiesProvider>
    </QueryClientProvider>
  );
  const { rerender } = render(tree());
  fireEvent.click(screen.getByTestId("new-project"));
  return { onCreated, rerenderWithHosts: () => rerender(tree()) };
}

function requestsTo(fetchMock: ReturnType<typeof vi.fn>, suffix: string) {
  return fetchMock.mock.calls
    .filter(([url]) => String(url).endsWith(suffix))
    .map(([, init]) => JSON.parse((init as RequestInit).body as string));
}

function cloneResponse() {
  return new Response(
    JSON.stringify({
      object: "repository_clone",
      path: "/Users/me/widgets",
      name: "widgets",
      reused_existing: false,
    }),
  );
}

describe("NewProjectButton clone section", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    localStorage.clear();
    hostsMock.data = [LAPTOP, SANDBOX];
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("clones first, then creates the project with the checkout as its default", async () => {
    let finishClone: (response: Response) => void = () => {};
    fetchMock.mockImplementation((url: string) =>
      url.endsWith("/repositories")
        ? new Promise<Response>((resolve) => {
            finishClone = resolve;
          })
        : Promise.resolve(new Response(JSON.stringify({ id: "proj_1", name: "widgets" }))),
    );
    const { onCreated } = renderButton();

    fireEvent.click(screen.getByTestId("new-project-clone-toggle"));
    const hostPicker = screen.getByTestId("new-project-clone-host");
    expect(hostPicker).toHaveTextContent("laptop");
    fireEvent.click(hostPicker);
    // Sandbox hosts already clone by URL at launch, so only real hosts are offered.
    expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual(["laptop"]);
    fireEvent.click(screen.getByRole("option", { name: "laptop" }));
    fireEvent.change(screen.getByTestId("clone-repository-url"), {
      target: { value: CLONE_URL },
    });
    expect(screen.getByTestId("new-project-name")).toHaveAttribute(
      "placeholder",
      "Project name (default: widgets)",
    );
    fireEvent.click(screen.getByTestId("new-project-confirm"));

    await waitFor(() =>
      expect(requestsTo(fetchMock, "/v1/hosts/host_laptop/repositories")).toEqual([
        { url: CLONE_URL, parent_directory: "~" },
      ]),
    );
    expect(requestsTo(fetchMock, "/v1/projects")).toEqual([]);
    finishClone(cloneResponse());

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("widgets"));
    expect(requestsTo(fetchMock, "/v1/projects")).toEqual([
      { name: "widgets", config: { host_id: "host_laptop", workspace: "/Users/me/widgets" } },
    ]);
  });

  it("creates no project when the clone fails, and clears the error when collapsed", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "/Users/me/widgets already exists." }), {
        status: 409,
      }),
    );
    const { onCreated } = renderButton();

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

    fireEvent.click(screen.getByTestId("new-project-clone-toggle"));
    expect(screen.queryByTestId("new-project-clone-error")).not.toBeInTheDocument();
  });

  it("keeps a chosen host that goes offline instead of cloning on another one", () => {
    hostsMock.data = [DESKTOP, LAPTOP];
    const { rerenderWithHosts } = renderButton();

    fireEvent.click(screen.getByTestId("new-project-clone-toggle"));
    fireEvent.click(screen.getByTestId("new-project-clone-host"));
    fireEvent.click(screen.getByRole("option", { name: "laptop" }));
    fireEvent.change(screen.getByTestId("clone-repository-url"), {
      target: { value: CLONE_URL },
    });
    expect(screen.getByTestId("new-project-confirm")).toBeEnabled();

    hostsMock.data = [DESKTOP, { ...LAPTOP, status: "offline" }];
    rerenderWithHosts();

    expect(screen.getByTestId("new-project-clone-host")).toHaveTextContent("laptop (offline)");
    expect(screen.getByTestId("new-project-clone-host-offline")).toHaveTextContent(
      "laptop is offline.",
    );
    expect(screen.getByTestId("new-project-confirm")).toBeDisabled();
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("retries on the default host after a failed clone, even once it leaves", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "Couldn't clone the repository." }), { status: 422 }),
    );
    hostsMock.data = [LAPTOP, DESKTOP];
    const { rerenderWithHosts } = renderButton();

    fireEvent.click(screen.getByTestId("new-project-clone-toggle"));
    fireEvent.change(screen.getByTestId("clone-repository-url"), {
      target: { value: CLONE_URL },
    });
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    expect(await screen.findByTestId("new-project-clone-error")).toBeInTheDocument();

    hostsMock.data = [{ ...LAPTOP, status: "offline" }, DESKTOP];
    rerenderWithHosts();
    expect(screen.getByTestId("new-project-clone-host")).toHaveTextContent("laptop (offline)");
    expect(screen.getByTestId("new-project-confirm")).toBeDisabled();

    hostsMock.data = [DESKTOP];
    rerenderWithHosts();
    expect(screen.getByTestId("new-project-clone-host-missing")).toHaveTextContent(
      "The chosen host is no longer connected.",
    );
    expect(screen.getByTestId("new-project-confirm")).toBeDisabled();
    fireEvent.click(screen.getByTestId("new-project-confirm"));
    expect(requestsTo(fetchMock, "/repositories")).toHaveLength(1);
    expect(requestsTo(fetchMock, "/v1/hosts/host_desktop/repositories")).toEqual([]);
  });

  it("uses the remembered folder of the default host once hosts load", () => {
    localStorage.setItem(
      "omnigent:clone-repository-parent",
      JSON.stringify({ host_laptop: "~/code" }),
    );
    hostsMock.data = undefined;
    const { rerenderWithHosts } = renderButton();

    fireEvent.click(screen.getByTestId("new-project-clone-toggle"));
    expect(screen.getByTestId("clone-repository-parent")).toHaveValue("~");

    hostsMock.data = [LAPTOP];
    rerenderWithHosts();

    expect(screen.getByTestId("clone-repository-parent")).toHaveValue("~/code");
  });

  it("offers no clone section on a server without the clone route", () => {
    renderButton(FALLBACK_SERVER_INFO);

    expect(screen.getByTestId("new-project-name")).toBeInTheDocument();
    expect(screen.queryByTestId("new-project-clone-toggle")).not.toBeInTheDocument();
  });

  it("still creates a plain project without the clone section", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ id: "proj_2", name: "Notes", config: {} })),
    );
    const { onCreated } = renderButton();

    fireEvent.change(screen.getByTestId("new-project-name"), { target: { value: "Notes" } });
    fireEvent.click(screen.getByTestId("new-project-confirm"));

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("Notes"));
    expect(requestsTo(fetchMock, "/v1/projects")).toEqual([{ name: "Notes" }]);
    expect(requestsTo(fetchMock, "/repositories")).toEqual([]);
  });
});
