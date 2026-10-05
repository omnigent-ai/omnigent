// Tests for the New design dialog. Agents, hosts, the host filesystem, and the
// session requests are mocked at their seams; the slug, first message, and
// remembered defaults run for real.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useHostFilesystem } from "@/hooks/useHostFilesystem";
import { useHosts, type Host } from "@/hooks/useHosts";
import { readDesignDefaults, rememberDesignDefaults } from "@/lib/designStudio";
import { nativeWrapperLabelsForAgent } from "@/lib/nativeCodingAgents";
import { createSession, postEvent } from "@/lib/sessionsApi";
import { testAgent } from "@/test/agentFixtures";
import { NewDesignDialog } from "./NewDesignDialog";

vi.mock("@/hooks/useAvailableAgents", () => ({ useAvailableAgents: vi.fn() }));
vi.mock("@/hooks/useHosts", () => ({ useHosts: vi.fn() }));
vi.mock("@/hooks/useHostFilesystem", () => ({ useHostFilesystem: vi.fn() }));
vi.mock("@/lib/sessionsApi", () => ({ createSession: vi.fn(), postEvent: vi.fn() }));
vi.mock("@/lib/agentLabels", () => ({ useBrainHarnessLabels: () => ({}) }));
vi.mock("@/shell/WorkspacePicker", () => ({
  isNavigablePath: (path: string) => path.startsWith("/"),
  WorkspacePicker: ({ onSelect }: { onSelect?: (p: string) => void }) => (
    <button type="button" onClick={() => onSelect?.("/work/picked")}>
      pick-folder
    </button>
  ),
}));
vi.mock("@/shell/NewChatDialog", () => ({
  AgentHarnessPicker: ({
    effectiveAgentId,
    onSelectAgent,
    agentEntries,
  }: {
    effectiveAgentId: string | null;
    onSelectAgent: (agent: AvailableAgent) => void;
    agentEntries: AvailableAgent[];
  }) => (
    <div data-testid="agent-picker" data-effective={effectiveAgentId ?? ""}>
      {agentEntries.map((agent) => (
        <button key={agent.id} type="button" onClick={() => onSelectAgent(agent)}>
          {`pick ${agent.display_name}`}
        </button>
      ))}
    </div>
  ),
}));

const CLAUDE = testAgent("ag_claude", "claude-native-ui", {
  display_name: "Claude Code",
  harness: "claude-native",
});
const POLLY = testAgent("ag_polly", "polly", { display_name: "Polly", harness: "claude-sdk" });
const LAPTOP: Host = { host_id: "host_1", name: "laptop", owner: "me", status: "online" };
const DESK: Host = { host_id: "host_2", name: "desk", owner: "me", status: "offline" };

const createMock = vi.mocked(createSession);
const postMock = vi.mocked(postEvent);
const filesystemMock = vi.mocked(useHostFilesystem);
let listings: Record<string, string[] | "missing">;

function listing(path: string | null) {
  const names = path === null ? undefined : listings[path];
  if (names === "missing") {
    return { data: undefined, error: Object.assign(new Error("404"), { status: 404 }) };
  }
  return {
    data: names && { entries: names.map((name) => ({ name, path: `${path}/${name}` })) },
    error: null,
  };
}

function renderDialog(
  props: Partial<Parameters<typeof NewDesignDialog>[0]> = {},
): ReturnType<typeof render> & { onCreated: ReturnType<typeof vi.fn> } {
  const onCreated = vi.fn();
  const client = new QueryClient();
  const view = render(
    <QueryClientProvider client={client}>
      <NewDesignDialog
        open
        onOpenChange={vi.fn()}
        takenDeckNames={() => []}
        onCreated={onCreated}
        {...props}
      />
    </QueryClientProvider>,
  );
  return { ...view, onCreated };
}

function typePrompt(text: string) {
  fireEvent.change(screen.getByLabelText("Prompt"), { target: { value: text } });
}

function create() {
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
}

beforeEach(() => {
  listings = {};
  vi.mocked(useAvailableAgents).mockReturnValue({
    data: [POLLY, CLAUDE],
  } as unknown as ReturnType<typeof useAvailableAgents>);
  vi.mocked(useHosts).mockReturnValue({ data: [DESK, LAPTOP] } as unknown as ReturnType<
    typeof useHosts
  >);
  filesystemMock.mockImplementation(
    (_host, path) => listing(path) as unknown as ReturnType<typeof useHostFilesystem>,
  );
  createMock.mockResolvedValue({ id: "conv_new" } as Awaited<ReturnType<typeof createSession>>);
  postMock.mockResolvedValue({ queued: true } as Awaited<ReturnType<typeof postEvent>>);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  localStorage.clear();
});

describe("NewDesignDialog defaults", () => {
  it("defaults to the web default agent and the first online host", () => {
    renderDialog();
    expect(screen.getByTestId("agent-picker")).toHaveAttribute("data-effective", "ag_claude");
    expect(screen.getByTestId("design-host-trigger")).toHaveTextContent("laptop");
  });

  it("offers only online hosts", () => {
    renderDialog();
    const trigger = screen.getByTestId("design-host-trigger");
    fireEvent.pointerDown(trigger, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
    fireEvent.click(trigger);
    expect(screen.getByRole("option", { name: "laptop" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /desk/ })).toBeNull();
  });

  it("uses the last agent and the last folder for that host", () => {
    rememberDesignDefaults("ag_polly", "host_1", "/work/site");
    renderDialog();
    expect(screen.getByTestId("agent-picker")).toHaveAttribute("data-effective", "ag_polly");
    expect(screen.getByTestId("design-folder")).toHaveTextContent("/work/site");
  });

  it("prefills the prompt", () => {
    renderDialog({ initialPrompt: "Product launch" });
    expect(screen.getByLabelText("Prompt")).toHaveValue("Product launch");
  });
});

describe("NewDesignDialog validation", () => {
  it("needs a prompt and a folder before Create", () => {
    renderDialog();
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();
    typePrompt("Pitch deck");
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: /Choose folder/ }));
    fireEvent.click(screen.getByRole("button", { name: "pick-folder" }));

    expect(screen.getByTestId("design-folder")).toHaveTextContent("/work/picked");
    expect(screen.getByRole("button", { name: "Create" })).toBeEnabled();
  });

  it("is blocked without an online host", () => {
    vi.mocked(useHosts).mockReturnValue({ data: [DESK] } as unknown as ReturnType<typeof useHosts>);
    rememberDesignDefaults("ag_claude", "host_2", "/work/site");
    renderDialog({ initialPrompt: "Pitch" });
    expect(screen.getByText(/No online hosts/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();
  });
});

describe("NewDesignDialog create", () => {
  beforeEach(() => rememberDesignDefaults("ag_claude", "host_1", "/work/site"));

  it("creates the session, sends the first message, and reports the deck", async () => {
    const { onCreated } = renderDialog();
    typePrompt("Pitch deck from my notes");
    create();

    await waitFor(() =>
      expect(onCreated).toHaveBeenCalledWith(
        "conv_new",
        "decks/pitch-deck-from-my-notes.slides.html",
      ),
    );
    expect(createMock).toHaveBeenCalledWith("ag_claude", [], {
      hostId: "host_1",
      workspace: "/work/site",
      labels: nativeWrapperLabelsForAgent(CLAUDE),
    });
    expect(nativeWrapperLabelsForAgent(CLAUDE)).toBeDefined();
    expect(postMock).toHaveBeenCalledWith("conv_new", {
      type: "message",
      data: {
        role: "user",
        content: [
          {
            type: "input_text",
            text: expect.stringMatching(
              /^Pitch deck from my notes\n\nUse the slide-decks skill\. Write the deck to `decks\/pitch-deck-from-my-notes\.slides\.html`\./,
            ),
          },
        ],
      },
    });
    expect(createMock.mock.invocationCallOrder[0]).toBeLessThan(
      postMock.mock.invocationCallOrder[0],
    );
    expect(readDesignDefaults()).toEqual({
      agentId: "ag_claude",
      hostId: "host_1",
      folders: { host_1: "/work/site" },
    });
  });

  it("sends no wrapper labels for a non-native agent", async () => {
    renderDialog({ initialPrompt: "Pitch" });
    fireEvent.click(screen.getByRole("button", { name: "pick Polly" }));
    create();
    await waitFor(() => expect(createMock).toHaveBeenCalled());
    expect(createMock.mock.calls[0][2]).toEqual({ hostId: "host_1", workspace: "/work/site" });
  });

  it("picks a free slug from the landing list and the folder's decks/", async () => {
    listings["/work/site/decks"] = ["pitch-deck-2.slides.html", "notes.md"];
    const { onCreated } = renderDialog({
      initialPrompt: "Pitch deck",
      takenDeckNames: (folder) => (folder === "/work/site" ? ["pitch-deck"] : []),
    });
    create();
    await waitFor(() =>
      expect(onCreated).toHaveBeenCalledWith("conv_new", "decks/pitch-deck-3.slides.html"),
    );
  });

  it("waits for the first decks/ listing before Create", async () => {
    filesystemMock.mockImplementation(
      (_host, path) =>
        (path === "/work/site/decks" && !listings[path]
          ? { data: undefined, error: null, isLoading: true }
          : listing(path)) as unknown as ReturnType<typeof useHostFilesystem>,
    );
    const { onCreated } = renderDialog({ initialPrompt: "Pitch" });
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();

    listings["/work/site/decks"] = ["pitch-deck.slides.html"];
    typePrompt("Pitch deck");
    expect(screen.getByRole("button", { name: "Create" })).toBeEnabled();
    create();
    await waitFor(() =>
      expect(onCreated).toHaveBeenCalledWith("conv_new", "decks/pitch-deck-2.slides.html"),
    );
  });

  it("keeps the dialog, the error, and the prompt when create fails", async () => {
    createMock.mockRejectedValueOnce(new Error("Workspace not found"));
    const { onCreated } = renderDialog({ initialPrompt: "Pitch deck" });
    create();

    expect(await screen.findByRole("alert")).toHaveTextContent("Workspace not found");
    expect(screen.getByLabelText("Prompt")).toHaveValue("Pitch deck");
    expect(onCreated).not.toHaveBeenCalled();
    expect(postMock).not.toHaveBeenCalled();
  });

  it("retries only the send on the same session after a send failure", async () => {
    postMock.mockRejectedValueOnce(new Error("Runner did not come online"));
    const { onCreated } = renderDialog({ initialPrompt: "Pitch deck" });
    create();
    expect(await screen.findByRole("alert")).toHaveTextContent("Runner did not come online");

    create();

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("conv_new", expect.any(String)));
    expect(createMock).toHaveBeenCalledTimes(1);
    expect(postMock).toHaveBeenCalledTimes(2);
  });
});

describe("NewDesignDialog kit hint", () => {
  beforeEach(() => rememberDesignDefaults("ag_claude", "host_1", "/work/site"));

  it("says Kit found when the folder has a design kit", () => {
    listings["/work/site/.omnigent/design-kit"] = ["kit.json", "logo.svg"];
    renderDialog();
    expect(screen.getByText("Kit found")).toBeInTheDocument();
  });

  it("says No kit when the kit folder is missing", () => {
    listings["/work/site/.omnigent/design-kit"] = "missing";
    renderDialog();
    expect(screen.getByText("No kit")).toBeInTheDocument();
  });
});
