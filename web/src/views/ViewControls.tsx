import { Columns3Icon, ListIcon, SlidersHorizontalIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import type { Project } from "@/lib/projectsApi";
import type { ViewDisplay, ViewEntity, ViewInput, ViewProperty } from "@/lib/workViewsApi";
import { newView } from "@/lib/workViewsApi";
import { ENTITY_NAMES, PROPERTY_NAMES } from "./viewPresentation";
import { ViewFilters } from "./ViewFilters";

export function ViewSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: Record<string, string>;
  onChange: (value: string) => void;
}) {
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger aria-label={label} className="min-w-36">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {Object.entries(options).map(([key, title]) => (
          <SelectItem key={key} value={key}>
            {title}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

export function EntityTabs({
  value,
  onChange,
}: {
  value: ViewEntity;
  onChange: (entity: ViewEntity) => void;
}) {
  return (
    <div className="flex items-center gap-2" role="group" aria-label="View item type">
      {Object.entries(ENTITY_NAMES).map(([key, title]) => (
        <Button
          key={key}
          variant={key === value ? "secondary" : "ghost"}
          className="rounded-full px-4"
          aria-pressed={key === value}
          onClick={() => onChange(key as ViewEntity)}
        >
          {title}
        </Button>
      ))}
    </div>
  );
}

function toggle<T>(values: T[], value: T): T[] {
  return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
}

export function ViewControls({
  value,
  projects,
  onChange,
  showEntityTabs = true,
}: {
  value: ViewInput;
  projects: Pick<Project, "id" | "name">[];
  onChange: (view: ViewInput) => void;
  showEntityTabs?: boolean;
}) {
  const filters = value.filters;
  const display = value.display;
  const setFilters = (changes: Partial<typeof filters>) =>
    onChange({ ...value, filters: { ...filters, ...changes } });
  const setDisplay = (changes: Partial<ViewDisplay>) =>
    onChange({ ...value, display: { ...display, ...changes } });
  const properties = Object.entries(PROPERTY_NAMES).filter(([key]) =>
    value.entity_type === "projects"
      ? key.endsWith("_at")
      : value.entity_type === "sessions"
        ? key !== "priority"
        : true,
  );
  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-border/60 px-5 py-3">
      {showEntityTabs ? (
        <EntityTabs
          value={value.entity_type}
          onChange={(entity) => {
            const next = newView(entity);
            onChange({
              ...next,
              name: value.name === `All ${value.entity_type}` ? next.name : value.name,
              description: value.description,
              favorite: value.favorite,
            });
          }}
        />
      ) : (
        <Badge variant="secondary" className="h-8 px-3">
          {ENTITY_NAMES[value.entity_type]}
        </Badge>
      )}
      <ViewFilters value={value} projects={projects} onChange={onChange} />
      <div className="ml-auto flex items-center gap-2">
        <Input
          aria-label="Filter by name"
          maxLength={200}
          placeholder={`Search ${value.entity_type}…`}
          value={filters.search}
          onChange={(event) => setFilters({ search: event.target.value })}
          className="h-8 w-40 border-transparent bg-muted/40 sm:w-56"
        />
        <Popover>
          <PopoverTrigger asChild>
            <Button
              variant="outline"
              size="icon"
              className="rounded-full"
              aria-label="Display options"
            >
              <SlidersHorizontalIcon />
            </Button>
          </PopoverTrigger>
          <PopoverContent align="end" className="w-80 space-y-5 p-4">
            <div className="grid grid-cols-2 gap-2" role="group" aria-label="Layout">
              <Button
                variant={display.layout === "list" ? "secondary" : "outline"}
                className="rounded-full"
                aria-pressed={display.layout === "list"}
                onClick={() => setDisplay({ layout: "list" })}
              >
                <ListIcon />
                List
              </Button>
              <Button
                variant={display.layout === "board" ? "secondary" : "outline"}
                className="rounded-full"
                aria-pressed={display.layout === "board"}
                onClick={() => setDisplay({ layout: "board" })}
              >
                <Columns3Icon />
                Board
              </Button>
            </div>
            <div className="flex items-center justify-between gap-3 text-sm">
              <span>{display.layout === "board" ? "Columns" : "Grouping"}</span>
              <ViewSelect
                label="Grouping"
                value={display.group_by}
                options={
                  value.entity_type === "projects"
                    ? { none: "No grouping" }
                    : {
                        none: "No grouping",
                        status: "Status",
                        project: "Project",
                        ...(value.entity_type === "tasks" ? { priority: "Priority" } : {}),
                      }
                }
                onChange={(group_by) =>
                  setDisplay({ group_by: group_by as ViewDisplay["group_by"] })
                }
              />
            </div>
            <div className="flex items-center justify-between gap-3 text-sm">
              <span>Ordering</span>
              <ViewSelect
                label="Ordering"
                value={display.order_by}
                options={{
                  updated_at: "Updated",
                  created_at: "Created",
                  ...(value.entity_type !== "sessions" ? { title: "Name" } : {}),
                  ...(value.entity_type === "tasks" ? { priority: "Priority" } : {}),
                }}
                onChange={(order_by) =>
                  setDisplay({
                    order_by: order_by as ViewDisplay["order_by"],
                    direction: order_by === "title" || order_by === "priority" ? "asc" : "desc",
                  })
                }
              />
            </div>
            <div className="flex items-center justify-between gap-3 text-sm">
              <span>Direction</span>
              <ViewSelect
                label="Sort direction"
                value={display.direction}
                options={{ asc: "Ascending", desc: "Descending" }}
                onChange={(direction) =>
                  setDisplay({ direction: direction as ViewDisplay["direction"] })
                }
              />
            </div>
            {display.group_by !== "none" && display.group_by !== "project" && (
              <label className="flex items-center justify-between text-sm">
                Show empty groups
                <Switch
                  checked={display.show_empty_groups}
                  onCheckedChange={(show_empty_groups) => setDisplay({ show_empty_groups })}
                />
              </label>
            )}
            <fieldset className="border-t border-border/60 pt-4">
              <legend className="text-sm text-muted-foreground">Display properties</legend>
              <div className="flex flex-wrap gap-2">
                {properties.map(([key, label]) => (
                  <Button
                    key={key}
                    size="sm"
                    className="rounded-full"
                    variant={
                      display.properties.includes(key as ViewProperty) ? "secondary" : "outline"
                    }
                    aria-pressed={display.properties.includes(key as ViewProperty)}
                    onClick={() =>
                      setDisplay({ properties: toggle(display.properties, key as ViewProperty) })
                    }
                  >
                    {label}
                  </Button>
                ))}
              </div>
            </fieldset>
          </PopoverContent>
        </Popover>
      </div>
    </div>
  );
}
