import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CompanyBrainPage } from "./CompanyBrainPage";
import * as hooks from "@/hooks/useCompanyBrain";

vi.mock("@/hooks/useIsAdmin", () => ({ useIsAdmin: () => true }));
vi.mock("@/hooks/useCompanyBrain", () => ({
  useCompanyBrain: vi.fn(),
  useCompanyBrainProviders: vi.fn(),
  useCompanyBrainResources: vi.fn(),
  useStartCompanyBrainOAuth: vi.fn(),
  usePreviewCompanyBrainResources: vi.fn(),
  useActivateCompanyBrainResources: vi.fn(),
  useUpdateCompanyBrainSelection: vi.fn(),
  useSyncCompanyBrainSelection: vi.fn(),
  useDisconnectCompanyBrainConnection: vi.fn(),
}));

const syncMutate = vi.fn();
const previewMutate = vi.fn();
const disconnectMutate = vi.fn();
const updateMutate = vi.fn();

beforeEach(() => {
  syncMutate.mockReset();
  previewMutate.mockReset();
  disconnectMutate.mockReset();
  updateMutate.mockReset();
  vi.mocked(hooks.useCompanyBrain).mockReturnValue({
    data: {
      installation: {
        id: "brain-1",
        status: "ready",
        repoUrl: "https://github.com/example/company-brain",
        mcpUrl: "https://brain.example/mcp",
        createdAt: 1_700_000_000,
        updatedAt: null,
      },
      connections: [
        {
          id: "connection-1",
          provider: "notion",
          accountLabel: "Policies",
          grantedScopes: [],
          status: "connected",
          lastError: null,
          createdAt: 1_700_000_000,
          updatedAt: null,
        },
      ],
      selections: [
        {
          id: "selection-1",
          connectionId: "connection-1",
          externalResourceId: "page-1",
          resourceName: "Security policies",
          resourceType: "notion_page",
          sourceUrl: "https://www.notion.so/page-1",
          transformProfile: "notion-page.v1",
          visibilityClass: "org-shared",
          rrule: "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
          timezone: "UTC",
          state: "active",
          lastSyncedAt: 1_700_000_100,
          pageCount: 14,
          lastError: "Notion returned a redacted rate-limit error",
          createdAt: 1_700_000_000,
          updatedAt: null,
        },
      ],
      runs: [],
    },
    isLoading: false,
    refetch: vi.fn(),
  } as never);
  vi.mocked(hooks.useCompanyBrainProviders).mockReturnValue({
    data: [{ id: "notion", configured: true, scopes: [] }],
  } as never);
  vi.mocked(hooks.useCompanyBrainResources).mockReturnValue({
    data: [
      {
        id: "page-2",
        name: "Vendor review",
        resourceType: "notion_page",
        sourceUrl: "https://www.notion.so/page-2",
        orgShared: true,
        metadata: {},
      },
    ],
    isLoading: false,
  } as never);
  vi.mocked(hooks.useStartCompanyBrainOAuth).mockReturnValue({
    isPending: false,
    mutateAsync: vi.fn(),
  } as never);
  vi.mocked(hooks.usePreviewCompanyBrainResources).mockReturnValue({
    isPending: false,
    mutateAsync: previewMutate,
  } as never);
  vi.mocked(hooks.useActivateCompanyBrainResources).mockReturnValue({
    isPending: false,
    mutateAsync: vi.fn(),
  } as never);
  vi.mocked(hooks.useUpdateCompanyBrainSelection).mockReturnValue({
    isPending: false,
    mutateAsync: updateMutate,
  } as never);
  vi.mocked(hooks.useSyncCompanyBrainSelection).mockReturnValue({
    isPending: false,
    mutateAsync: syncMutate,
  } as never);
  vi.mocked(hooks.useDisconnectCompanyBrainConnection).mockReturnValue({
    isPending: false,
    mutateAsync: disconnectMutate,
  } as never);
});

afterEach(cleanup);

describe("CompanyBrainPage", () => {
  it("shows ownership, source status, redacted error, and retry action", () => {
    render(<CompanyBrainPage />);

    expect(screen.getByRole("heading", { name: "Company brain" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open company-owned repository/ })).toHaveAttribute(
      "href",
      "https://github.com/example/company-brain",
    );
    expect(screen.getByText("Security policies")).toBeInTheDocument();
    expect(screen.getByText("Needs attention")).toBeInTheDocument();
    expect(screen.getByText("Notion returned a redacted rate-limit error")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Retry sync" }));
    expect(syncMutate).toHaveBeenCalledWith({ id: "selection-1", retry: true });
  });

  it("moves through resource selection into a transformed-page preview", async () => {
    previewMutate.mockResolvedValue([
      {
        provider: "notion",
        title: "Vendor review",
        markdown: "# Vendor review\n\nApproved with conditions.\n",
        canonicalSourceUrl: "https://www.notion.so/page-2",
        contentSha256: "a".repeat(64),
        transformSchemaVersion: "notion-page.v1",
        visibilityClass: "org-shared",
      },
    ]);
    render(<CompanyBrainPage />);

    fireEvent.click(screen.getByRole("button", { name: "Connect source" }));
    fireEvent.click(screen.getByRole("button", { name: /Notion/ }));
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Preview pages" }));

    await waitFor(() =>
      expect(screen.getByText("Approved with conditions.", { exact: false })).toBeInTheDocument(),
    );
    expect(screen.getByRole("dialog")).toHaveClass("w-[calc(100%-2rem)]", "sm:max-w-4xl");
    expect(screen.getAllByText("Vendor review").length).toBeGreaterThan(0);
    expect(screen.getByText("Org shared")).toBeInTheDocument();
  });

  it("requires confirmation before disconnecting and retains history", () => {
    render(<CompanyBrainPage />);

    fireEvent.click(screen.getByRole("button", { name: "Disconnect source" }));

    expect(screen.getByRole("heading", { name: "Disconnect this source?" })).toBeInTheDocument();
    expect(
      screen.getByText(/Existing Git history and indexed knowledge are retained/),
    ).toBeInTheDocument();
    expect(disconnectMutate).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(disconnectMutate).not.toHaveBeenCalled();
  });

  it("clears an existing schedule when Manual is selected", async () => {
    render(<CompanyBrainPage />);

    fireEvent.click(screen.getByRole("combobox", { name: "Sync schedule" }));
    fireEvent.click(await screen.findByRole("option", { name: "Manual" }));

    expect(updateMutate).toHaveBeenCalledWith({
      id: "selection-1",
      input: { rrule: null, timezone: "UTC" },
    });
  });
});
