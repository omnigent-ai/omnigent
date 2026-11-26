import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type * as ProjectTicketsApi from "@/lib/projectTicketsApi";
import { ProjectFileViewerHost } from "./ProjectFileViewer";

const PROJECT = "a".repeat(32);
const REPORT = "b".repeat(32);
const APPENDIX = "c".repeat(32);
const TITLES: Record<string, string> = { [REPORT]: "Weekly report", [APPENDIX]: "Appendix" };

vi.mock("@/lib/projectTicketsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof ProjectTicketsApi>()),
  getProjectFile: vi.fn(async (path: string) => {
    const id = path.split("/").pop()!;
    return { id, title: TITLES[id], filename: `${TITLES[id]}.md` };
  }),
}));

// The viewer itself is out of scope; render a link to the other file so a click can follow it.
vi.mock("@/shell/FileViewer", () => ({
  FileViewer: ({ path }: { path: string }) => (
    <div>
      <p>Viewing {path}</p>
      <a href={`/v1/projects/${PROJECT}/files/${APPENDIX}/content`}>appendix link</a>
    </div>
  ),
}));

function renderHost() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={[`/projects/${PROJECT}?artifact=${REPORT}`]}>
        <ProjectFileViewerHost projectId={PROJECT} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ProjectFileViewerHost", () => {
  it("follows a link to another project file and offers Back to the first", async () => {
    renderHost();
    expect(await screen.findByText("Viewing Weekly report.md")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Back to/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getByText("appendix link"));
    expect(await screen.findByText("Viewing Appendix.md")).toBeInTheDocument();

    fireEvent.click(await screen.findByRole("button", { name: "Back to Weekly report" }));
    expect(await screen.findByText("Viewing Weekly report.md")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Back to/ })).not.toBeInTheDocument();
  });
});
