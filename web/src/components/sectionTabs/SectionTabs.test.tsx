import { render, screen } from "@testing-library/react";
import { FileDiffIcon, GitCommitHorizontalIcon, MessagesSquareIcon } from "lucide-react";
import { describe, expect, it } from "vitest";

import { Tabs } from "@/components/ui/tabs";

import { type SectionTab, SectionTabs, type SectionTabsNarrow } from "./SectionTabs";

const TABS: SectionTab[] = [
  { id: "summary", label: "Overview", icon: MessagesSquareIcon },
  { id: "commits", label: "Commits", icon: GitCommitHorizontalIcon, count: 2 },
  { id: "changes", label: "Changes", icon: FileDiffIcon, count: 30, partial: true },
];

function renderTabs(narrow: SectionTabsNarrow) {
  render(
    <Tabs value="summary">
      <SectionTabs tabs={TABS} narrow={narrow} aria-label="Pull request" />
    </Tabs>,
  );
}

describe("SectionTabs", () => {
  it.each<SectionTabsNarrow>(["drop-icons", "drop-labels"])(
    "names each tab by its label and count, apart (%s)",
    (narrow) => {
      renderTabs(narrow);
      expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
        "Overview ",
        "Commits 2",
        "Changes 30+",
      ]);
      expect(screen.getByRole("tab", { name: "Overview" })).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: "Commits 2" })).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: "Changes 30+" })).toBeInTheDocument();
    },
  );
});
