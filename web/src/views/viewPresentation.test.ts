import { expect, it } from "vitest";
import { newView, type ViewRecord } from "@/lib/workViewsApi";
import { groupViewRecords } from "./viewPresentation";

const records: ViewRecord[] = [
  { id: "done", title: "Finished", status: "done", priority: 0, created_at: 1, updated_at: 1 },
  {
    id: "urgent",
    title: "Important",
    status: "todo",
    priority: 1,
    project_id: "p",
    project_name: "Release",
    created_at: 1,
    updated_at: 1,
  },
];

it("uses workflow order for status groups and keeps results when switching layout", () => {
  const definition = newView();
  const list = groupViewRecords(records, definition);
  expect(list.map((group) => group.key)).toEqual(["todo", "done"]);
  definition.display.layout = "board";
  expect(groupViewRecords(records, definition)).toEqual(list);
  definition.display.show_empty_groups = true;
  const groups = groupViewRecords(records, definition);
  expect(groups).toHaveLength(6);
  expect(groups.find((group) => group.key === "review")?.records).toEqual([]);
  expect(groups.flatMap((group) => group.records)).toHaveLength(2);
});

it("keeps unassigned work in project and priority groups", () => {
  const definition = newView();
  definition.display.group_by = "project";
  expect(groupViewRecords(records, definition).map((group) => group.title)).toEqual([
    "No project",
    "Release",
  ]);
  definition.display.group_by = "priority";
  expect(groupViewRecords(records, definition).map((group) => group.key)).toEqual(["1", "0"]);
  definition.display.group_by = "none";
  expect(groupViewRecords(records, definition)).toEqual([{ key: "all", title: "Tasks", records }]);
});
