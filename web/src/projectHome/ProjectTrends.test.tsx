import { describe, expect, it } from "vitest";
import type { ProjectStatusTrends } from "@/lib/projectTrendsApi";
import { trendCharts } from "./ProjectTrends";

function trends(overrides: Partial<ProjectStatusTrends["days"][number]> = {}): ProjectStatusTrends {
  return {
    object: "project.status_trends",
    project_id: "project",
    window: "7d",
    today_utc: "2026-03-10",
    bucket: "day",
    days: [
      {
        day_utc: "2026-03-10",
        tickets_filed: 2,
        tickets_finished: 1,
        open_tickets: 4,
        changes_landed: null,
        prs_merged: 0,
        spend_usd: 1.5,
        ...overrides,
      },
    ],
  };
}

describe("trendCharts", () => {
  it("charts changes landed instead of PRs once the project lands commits", () => {
    expect(trendCharts(trends()).map((chart) => chart.title)).toEqual([
      "Tickets filed",
      "Tickets finished",
      "Open tickets",
      "PRs merged",
      "Spend",
    ]);
    expect(trendCharts(trends({ changes_landed: 3 })).map((chart) => chart.field)).toContain(
      "changes_landed",
    );
  });

  it("drops a chart whose source isn't on this server", () => {
    const titles = trendCharts(trends({ spend_usd: null, prs_merged: null })).map(
      (chart) => chart.title,
    );
    expect(titles).toEqual(["Tickets filed", "Tickets finished", "Open tickets"]);
  });
});
