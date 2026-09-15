// Integration test for the project emoji-icon flow (OMNI-3742). Drives the real
// ProjectLandingIcon + useUpdateProjectConfig + projects API client end to end,
// mocking only the network (@/lib/projectsApi) and the emoji-mart picker (its
// ~600KB JSON dataset can't load under vitest). The focus is the data-loss
// guard: because the config PATCH replaces the whole blob, every set/remove
// must merge onto a fully-loaded config and must not fire before it loads.

import { useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ProjectIconControl, ProjectLandingIcon } from "./ProjectIconPicker";
import { createProject, updateProjectConfig } from "@/lib/projectsApi";
import type { ProjectConfig } from "@/lib/projectsApi";

vi.mock("@/lib/projectsApi", () => ({
  getProject: vi.fn(),
  updateProjectConfig: vi.fn(),
  createProject: vi.fn(),
}));
vi.mock("next-themes", () => ({ useTheme: () => ({ resolvedTheme: "light" }) }));
// The real emoji-mart picker fetches a large JSON dataset that Node rejects
// under vitest; stub it to one button that reports a chosen emoji, so the
// open → select → save path is exercised without the dataset.
vi.mock("@emoji-mart/react", () => ({
  default: ({ onEmojiSelect }: { onEmojiSelect: (e: { native: string }) => void }) => (
    <button type="button" data-testid="pick-fire" onClick={() => onEmojiSelect({ native: "🔥" })}>
      🔥
    </button>
  ),
}));
vi.mock("@emoji-mart/data", () => ({ default: {} }));

const updateMock = vi.mocked(updateProjectConfig);
const createMock = vi.mocked(createProject);

interface Overrides {
  projectId?: string | null;
  projectName?: string;
  config?: ProjectConfig | undefined;
  configReady?: boolean;
}

function renderIcon(overrides: Overrides = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ProjectLandingIcon
        projectId={"projectId" in overrides ? (overrides.projectId ?? null) : "p_1"}
        projectName={overrides.projectName ?? "Work"}
        config={"config" in overrides ? overrides.config : {}}
        configReady={overrides.configReady ?? true}
      />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  updateMock.mockReset();
  createMock.mockReset();
  updateMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
  createMock.mockResolvedValue({ id: "p_new", name: "Work", config: {} });
});

afterEach(cleanup);

describe("ProjectLandingIcon", () => {
  it("merges the picked emoji onto the loaded config, preserving other defaults", async () => {
    const config: ProjectConfig = {
      host_id: "h1",
      workspace: "/repo",
      agent_id: "a1",
      use_worktree: true,
      base_branch: "main",
    };
    renderIcon({ config });

    fireEvent.click(screen.getByTestId("project-icon-tile"));
    fireEvent.click(await screen.findByTestId("pick-fire"));

    await waitFor(() => expect(updateMock).toHaveBeenCalledTimes(1));
    // The whole prior config survives; only `icon` is added.
    expect(updateMock).toHaveBeenCalledWith("p_1", { ...config, icon: "🔥" });
  });

  it("removes the icon while preserving the other defaults", async () => {
    renderIcon({ config: { host_id: "h1", icon: "🔥" } });

    fireEvent.click(screen.getByTestId("project-icon-remove"));

    await waitFor(() => expect(updateMock).toHaveBeenCalledTimes(1));
    expect(updateMock).toHaveBeenCalledWith("p_1", { host_id: "h1" });
  });

  it("does not write before the config has loaded (guards the data-loss race)", async () => {
    renderIcon({ config: undefined, configReady: false });

    // The edit affordance is disabled and the tile won't open the picker, so a
    // config-wiping `{ icon }` PATCH can't be issued against unloaded state.
    expect(screen.getByTestId("project-icon-edit")).toBeDisabled();
    fireEvent.click(screen.getByTestId("project-icon-tile"));
    expect(screen.queryByTestId("pick-fire")).toBeNull();
    expect(updateMock).not.toHaveBeenCalled();
  });

  it("promotes a label-only folder (no id) when an icon is first set", async () => {
    renderIcon({ projectId: null, config: undefined, configReady: true });

    fireEvent.click(screen.getByTestId("project-icon-tile"));
    fireEvent.click(await screen.findByTestId("pick-fire"));

    await waitFor(() => expect(updateMock).toHaveBeenCalledTimes(1));
    // The label-only folder is promoted (created) first, then the icon is set
    // on the fresh first-class id.
    expect(createMock).toHaveBeenCalledWith("Work");
    expect(updateMock).toHaveBeenCalledWith("p_new", { icon: "🔥" });
  });
});

// The controlled tile that hosts (landing page, settings form) build on. Its
// defining property: it reports edits through `onChange` and never writes on
// its own, so a form can stage the pick and decide when to persist.
describe("ProjectIconControl (controlled)", () => {
  it("renders the folder default when no glyph is set", () => {
    render(<ProjectIconControl value={undefined} onChange={() => {}} />);
    // The tile is present but shows no glyph, and there's nothing to remove yet.
    expect(within(screen.getByTestId("project-icon-tile")).queryByText("🔥")).toBeNull();
    expect(screen.queryByTestId("project-icon-remove")).toBeNull();
  });

  it("renders the current glyph and a remove affordance when set", () => {
    render(<ProjectIconControl value="🔥" onChange={() => {}} />);
    expect(within(screen.getByTestId("project-icon-tile")).getByText("🔥")).toBeInTheDocument();
    expect(screen.getByTestId("project-icon-remove")).toBeInTheDocument();
  });

  it("reports a picked glyph via onChange and never writes to the network", async () => {
    const onChange = vi.fn();
    render(<ProjectIconControl value={undefined} onChange={onChange} />);

    fireEvent.click(screen.getByTestId("project-icon-tile"));
    fireEvent.click(await screen.findByTestId("pick-fire"));

    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith("🔥");
    // No PATCH-on-click — the host owns the single write, so a form's Cancel/Save
    // can't be bypassed or raced.
    expect(updateMock).not.toHaveBeenCalled();
    expect(createMock).not.toHaveBeenCalled();
  });

  it("reports removal via onChange(undefined) without writing", () => {
    const onChange = vi.fn();
    render(<ProjectIconControl value="🔥" onChange={onChange} />);

    fireEvent.click(screen.getByTestId("project-icon-remove"));

    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith(undefined);
    expect(updateMock).not.toHaveBeenCalled();
  });

  it("gates editing while disabled", () => {
    const onChange = vi.fn();
    render(<ProjectIconControl value="🔥" onChange={onChange} disabled />);

    expect(screen.getByTestId("project-icon-edit")).toBeDisabled();
    expect(screen.getByTestId("project-icon-remove")).toBeDisabled();
    // The tile is inert too — clicking it opens no picker.
    fireEvent.click(screen.getByTestId("project-icon-tile"));
    expect(screen.queryByTestId("pick-fire")).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("shows the busy spinner while pending", () => {
    render(<ProjectIconControl value="🔥" onChange={() => {}} pending />);
    expect(screen.getByTestId("project-icon-pending")).toBeInTheDocument();
    // The spinner replaces the glyph while a write is in flight.
    expect(within(screen.getByTestId("project-icon-tile")).queryByText("🔥")).toBeNull();
  });
});

// The behaviour the issue is really about: hosted in a form, the control must
// let Cancel discard the pick and let a single submit carry it — neither
// possible while the picker PATCHed on click.
describe("ProjectIconControl in a form host", () => {
  // A minimal stand-in for the settings dialog: stage the icon in local state,
  // commit only on submit, discard on cancel.
  function IconForm({
    initial,
    onSubmit,
  }: {
    initial?: string;
    onSubmit: (glyph: string | undefined) => void;
  }) {
    const [staged, setStaged] = useState<string | undefined>(initial);
    return (
      <form
        onSubmit={(e) => {
          e.preventDefault();
          onSubmit(staged);
        }}
      >
        <ProjectIconControl value={staged} onChange={setStaged} />
        <button type="button" onClick={() => setStaged(initial)}>
          Cancel
        </button>
        <button type="submit">Save</button>
      </form>
    );
  }

  it("discards a pick on Cancel and persists nothing on its own", async () => {
    const onSubmit = vi.fn();
    render(<IconForm onSubmit={onSubmit} />);

    // Stage an emoji — the tile reflects it immediately from form state.
    fireEvent.click(screen.getByTestId("project-icon-tile"));
    fireEvent.click(await screen.findByTestId("pick-fire"));
    expect(within(screen.getByTestId("project-icon-tile")).getByText("🔥")).toBeInTheDocument();

    // Cancel drops the staged pick, and nothing was ever written.
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(within(screen.getByTestId("project-icon-tile")).queryByText("🔥")).toBeNull();
    expect(onSubmit).not.toHaveBeenCalled();
    expect(updateMock).not.toHaveBeenCalled();
    expect(createMock).not.toHaveBeenCalled();
  });

  it("emits the staged glyph in a single submit", async () => {
    const onSubmit = vi.fn();
    render(<IconForm onSubmit={onSubmit} />);

    fireEvent.click(screen.getByTestId("project-icon-tile"));
    fireEvent.click(await screen.findByTestId("pick-fire"));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    // One write, owned by the form — the picker issued no PATCH of its own.
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith("🔥");
    expect(updateMock).not.toHaveBeenCalled();
    expect(createMock).not.toHaveBeenCalled();
  });
});
