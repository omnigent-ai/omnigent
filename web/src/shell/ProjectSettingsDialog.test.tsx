import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ProjectSettingsDialog } from "./ProjectSettingsDialog";
import { getProject, updateProjectConfig, createProject } from "@/lib/projectsApi";

vi.mock("@/lib/projectsApi", () => ({
  getProject: vi.fn(),
  updateProjectConfig: vi.fn(),
  createProject: vi.fn(),
}));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({ data: [{ host_id: "h1", name: "Laptop", owner: "me", status: "online" }] }),
}));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: () => ({
    data: [
      {
        id: "ag_1",
        name: "hello",
        display_name: "Hello",
        description: null,
        harness: null,
        skills: [],
      },
    ],
  }),
  // The reused agent picker prefetches details on open; no-op in the dialog test.
  prefetchAvailableAgentDetails: vi.fn(),
}));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ managed_sandboxes_enabled: false, sandbox_provider: null }),
}));
// The filesystem browser owns its own data-fetching; stub it to a marker plus
// a button that reports a navigated path, so we can drive the disclosure and
// the live workspace update without the host-filesystem plumbing.
vi.mock("./WorkspacePicker", () => ({
  isNavigablePath: (p: string) => p.startsWith("/"),
  WorkspacePicker: ({ onNavigate }: { onNavigate: (p: string) => void }) => (
    <div data-testid="mock-workspace-picker">
      <button type="button" onClick={() => onNavigate("/picked/dir")}>
        pick dir
      </button>
    </div>
  ),
}));

const getProjectMock = vi.mocked(getProject);
const updateMock = vi.mocked(updateProjectConfig);
const createMock = vi.mocked(createProject);

function renderDialog(projectId: string | null = "p_1") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ProjectSettingsDialog open onOpenChange={vi.fn()} projectId={projectId} projectName="Work" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getProjectMock.mockReset();
  updateMock.mockReset();
  createMock.mockReset();
  updateMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
});

afterEach(cleanup);

describe("ProjectSettingsDialog", () => {
  it("seeds fields from the project's stored config", async () => {
    // No stored host → the working directory can't be chosen yet, so it shows
    // the "pick a host first" placeholder rather than a path input / browser.
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { use_worktree: true },
    });
    renderDialog();
    // use_worktree:true was stored → toggle seeds ON once the fetch settles.
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-worktree")).toHaveAttribute(
        "data-state",
        "checked",
      ),
    );
    // No stored host → the working directory can't be chosen yet, so it shows
    // the "pick a host first" placeholder rather than a path input / browser.
    expect(screen.getByTestId("project-settings-workspace")).toHaveTextContent(
      /pick a host first/i,
    );
  });

  it("saves only the fields that are set (unset slots omitted)", async () => {
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    renderDialog();
    // Wait for the config fetch to settle (Save enabled) so the seeding effect
    // has run before we interact — otherwise the seed would clobber our change.
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );

    // Turn the worktree default ON — the only field touched.
    fireEvent.click(screen.getByTestId("project-settings-worktree"));
    fireEvent.click(screen.getByTestId("project-settings-save"));

    await waitFor(() => expect(updateMock).toHaveBeenCalled());
    expect(updateMock).toHaveBeenCalledWith("p_1", { use_worktree: true });
  });

  it("stores nothing for the worktree toggle when left at its default (OFF)", async () => {
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
    // Leave the toggle OFF (default) → config clears to {} (use_worktree absent).
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() => expect(updateMock).toHaveBeenCalledWith("p_1", {}));
  });

  it("shows the base-branch field only when the worktree default is on, and saves it", async () => {
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
    // Base branch is hidden while the worktree default is OFF (nothing to fork).
    expect(screen.queryByTestId("project-settings-base-branch")).not.toBeInTheDocument();

    // Turning the worktree default ON reveals the base-branch field.
    fireEvent.click(screen.getByTestId("project-settings-worktree"));
    const input = screen.getByTestId("project-settings-base-branch");
    fireEvent.change(input, { target: { value: "  main  " } });
    fireEvent.click(screen.getByTestId("project-settings-save"));

    // Trimmed and stored alongside the worktree default.
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", { use_worktree: true, base_branch: "main" }),
    );
  });

  it("drops the base branch when the worktree default is off", async () => {
    // A base branch stored from an earlier ON state must not linger as an
    // invisible default once the worktree toggle is turned back off.
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { use_worktree: true, base_branch: "develop" },
    });
    renderDialog();
    // Seeded ON → the base-branch field shows its stored value.
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-base-branch")).toHaveValue("develop"),
    );
    // Turn the worktree default OFF → base branch drops, config clears to {}.
    fireEvent.click(screen.getByTestId("project-settings-worktree"));
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() => expect(updateMock).toHaveBeenCalledWith("p_1", {}));
  });

  it("round-trips a multi-line worktree script verbatim", async () => {
    // WHY: the stored value is a PROGRAM — internal newlines and indentation
    // must survive the editor untouched, or we silently rewrite the user's
    // script. Only the outer whitespace is trimmed on save.
    const script = "#!/usr/bin/env bash\nset -euo pipefail\n\nbun install\n  cp ../../.env .";
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { worktree_post_create_command: script },
    });
    renderDialog();
    const editor = await waitFor(() => screen.getByTestId("project-settings-post-create"));
    // A textarea, not a single-line input — a script needs the rows.
    expect(editor.tagName).toBe("TEXTAREA");
    await waitFor(() => expect(editor).toHaveValue(script));

    const edited = `${script}\nbun run build`;
    fireEvent.change(editor, { target: { value: `\n  ${edited}  \n` } });
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", {
        worktree_post_create_command: edited,
      }),
    );
  });

  it("shows a multi-line placeholder so the field reads as a script", async () => {
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    renderDialog();
    const editor = await waitFor(() => screen.getByTestId("project-settings-post-create"));
    const placeholder = editor.getAttribute("placeholder") ?? "";
    expect(placeholder).toContain("\n");
    expect(placeholder).toContain("bun install");
    // The teardown field gets its own example, not the setup one.
    const teardown = screen.getByTestId("project-settings-pre-delete");
    expect(teardown.getAttribute("placeholder")).toContain("docker compose down");
  });

  it("round-trips the worktree lifecycle commands and their timeout", async () => {
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: {
        worktree_post_create_command: "bun install",
        worktree_pre_delete_command: "./teardown.sh",
        worktree_hook_timeout_seconds: 600,
      },
    });
    renderDialog();
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-post-create")).toHaveValue("bun install"),
    );
    expect(screen.getByTestId("project-settings-pre-delete")).toHaveValue("./teardown.sh");
    // The timeout field (and the unsandboxed-execution note) only appear once a
    // command is configured — they mean nothing on their own.
    expect(screen.getByTestId("project-settings-hook-timeout")).toHaveValue(600);
    expect(screen.getByTestId("project-settings-hook-note")).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("project-settings-post-create"), {
      target: { value: "  pnpm install  " },
    });
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", {
        worktree_post_create_command: "pnpm install",
        worktree_pre_delete_command: "./teardown.sh",
        worktree_hook_timeout_seconds: 600,
      }),
    );
  });

  it("clears a worktree command when its field is emptied", async () => {
    // Blank means "no hook" server-side, so an emptied field must drop the key
    // rather than store an empty command the host would run as a blank shell.
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { worktree_post_create_command: "bun install" },
    });
    renderDialog();
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-post-create")).toHaveValue("bun install"),
    );
    fireEvent.change(screen.getByTestId("project-settings-post-create"), {
      target: { value: "   " },
    });
    // Emptying the command hides the timeout field with it — it bounds
    // nothing on its own.
    expect(screen.queryByTestId("project-settings-hook-timeout")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() => expect(updateMock).toHaveBeenCalledWith("p_1", {}));
  });

  it("hides the sandbox option when managed sandboxes are disabled", async () => {
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
    // The online host is offered, but no "Sandbox" option (managed sandboxes off).
    expect(screen.getAllByText("Laptop").length).toBeGreaterThan(0);
    expect(screen.queryByText(/Sandbox/)).not.toBeInTheDocument();
  });

  it("promotes a label-only folder (id=null) on save via createProject", async () => {
    createMock.mockResolvedValue({ id: "p_new", name: "Work" });
    renderDialog(null);
    // No fetch for a label-only folder (nothing to read).
    expect(getProjectMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("project-settings-worktree"));
    fireEvent.click(screen.getByTestId("project-settings-save"));

    await waitFor(() => expect(createMock).toHaveBeenCalledWith("Work"));
    expect(updateMock).toHaveBeenCalledWith("p_new", { use_worktree: true });
  });

  it("persists host, workspace, and agent from a seeded config on save", async () => {
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { host_id: "h1", workspace: "/repo", agent_id: "ag_1" },
    });
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
    // Save without touching anything — the seeded fields round-trip back out.
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", {
        host_id: "h1",
        workspace: "/repo",
        agent_id: "ag_1",
      }),
    );
  });

  it("opens the working-directory browser, updates the path, then closes on outside click", async () => {
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: { host_id: "h1" } });
    renderDialog();
    // A stored online host makes the working directory browsable — a compact
    // "Browse…" trigger, collapsed by default (no picker mounted yet).
    await waitFor(() => expect(screen.getByText("Browse…")).toBeInTheDocument());
    expect(screen.queryByTestId("mock-workspace-picker")).not.toBeInTheDocument();

    // Expand → the browser mounts; navigating updates the trigger label live.
    fireEvent.click(screen.getByText("Browse…"));
    expect(screen.getByTestId("mock-workspace-picker")).toBeInTheDocument();
    fireEvent.click(screen.getByText("pick dir"));
    expect(screen.getByText("/picked/dir")).toBeInTheDocument();

    // The click-away backdrop closes the browser, keeping the picked path.
    fireEvent.click(screen.getByRole("button", { name: /close directory browser/i }));
    expect(screen.queryByTestId("mock-workspace-picker")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", { host_id: "h1", workspace: "/picked/dir" }),
    );
  });

  it("seeds and saves the worktree location, trimmed", async () => {
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { worktree_root: ".worktrees" },
    });
    renderDialog();
    const field = () => screen.getByTestId("project-settings-worktree-root") as HTMLInputElement;
    await waitFor(() => expect(field().value).toBe(".worktrees"));

    fireEvent.change(field(), { target: { value: "  ../worktrees " } });
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", { worktree_root: "../worktrees" }),
    );
  });

  it("stores no worktree location when the field is left blank", async () => {
    // Blank must clear the key, not store an empty string the server would
    // then try to resolve into a directory.
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { worktree_root: ".worktrees" },
    });
    renderDialog();
    const field = () => screen.getByTestId("project-settings-worktree-root") as HTMLInputElement;
    await waitFor(() => expect(field().value).toBe(".worktrees"));

    fireEvent.change(field(), { target: { value: "   " } });
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() => expect(updateMock).toHaveBeenCalledWith("p_1", {}));
  });

  it("keeps the worktree location independent of the random-worktree toggle", async () => {
    // The location applies to every worktree Omnigent creates for the project
    // — including one an agent asks for via sys_worktree_create — so it must
    // not be dropped when the composer default is off.
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );

    fireEvent.change(screen.getByTestId("project-settings-worktree-root"), {
      target: { value: ".worktrees" },
    });
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", { worktree_root: ".worktrees" }),
    );
  });

  it("caps its height and scrolls the fields, keeping Save reachable", async () => {
    // An uncapped dialog taller than the viewport is clipped at both ends with
    // nothing to scroll — which is what happened as settings were added. The
    // cap now comes from DialogContent itself, against the VISIBLE viewport
    // rather than a static `85vh` that overhangs the mobile URL bar; this
    // caller only supplies its width. jsdom does no layout, so assert the
    // contract that prevents it: a height cap on the dialog, a scrollable
    // fields region, and Save OUTSIDE that region.
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );

    const content = document.querySelector('[data-slot="dialog-content"]');
    expect(content).not.toBeNull();
    expect(content!.className).toMatch(/max-h-\[var\(--omnigent-dialog-max-height\)\]/);
    expect(content!.className).toMatch(/top-\[var\(--omnigent-dialog-center\)\]/);
    // No stale `vh` cap: `vh` is the LARGE viewport, so it overhangs the
    // visible area whenever mobile browser chrome is showing.
    expect(content!.className).not.toMatch(/max-h-\[\d+vh\]/);

    // The fields region is the scroller that holds the fields but not Save.
    const scrollers = Array.from(content!.querySelectorAll(".overflow-y-auto"));
    const field = screen.getByTestId("project-settings-worktree-root");
    const save = screen.getByTestId("project-settings-save");
    const scroller = scrollers.find((el) => el.contains(field) && !el.contains(save));
    expect(scroller).toBeDefined();
    // The form between DialogContent's shared scroller and the fields is
    // flex-constrained, so only the fields ever move and Save stays put.
    const form = content!.querySelector("form")!;
    expect(form.className).toContain("min-h-0");
    expect(form.className).toContain("flex-1");
  });

  it("blocks Save (does not clear defaults) when the config load fails", async () => {
    // A transient GET failure must NOT be read as "no config" — otherwise
    // saving the blank draft would send `{}` and wipe the stored defaults.
    getProjectMock.mockRejectedValue(new Error("500 Server Error"));
    renderDialog();

    // The load-error notice shows and Save stays disabled.
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-load-error")).toBeInTheDocument(),
    );
    expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(true);

    // Even if a submit is forced, onSubmit bails — no clearing PATCH is sent.
    fireEvent.submit(screen.getByTestId("project-settings-save").closest("form")!);
    expect(updateMock).not.toHaveBeenCalled();
  });
});
