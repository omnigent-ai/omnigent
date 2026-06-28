/**
 * Entities page (`/entities`).
 *
 * Manages reusable entities — `{ id, title, instruction }` building blocks that
 * can be wired into any flow as a step (see {@link entityStore}) — and the
 * **groups** that organize them in the flow builder's picker (see
 * {@link entityGroupStore}). Built-in groups (Jira/GitHub) and their actions are
 * code-owned and read-only; users can create their own groups, upload a custom
 * icon per group, and assign entities to a group.
 */

import { useRef, useState } from "react";
import { PlusIcon, Trash2Icon, BlocksIcon, ImageIcon } from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { getIconComponent } from "@/components/icons/iconRegistry";
import {
  createEntity,
  deleteEntity,
  updateEntity,
  useEntities,
  type Entity,
} from "@/lib/entityStore";
import {
  createEntityGroup,
  deleteEntityGroup,
  uploadEntityGroupIcon,
  useEntityGroups,
  type EntityGroup,
} from "@/lib/entityGroupStore";

/** A group's icon: bundled component (built-ins) or uploaded image (custom). */
function GroupIcon({ group, className }: { group: EntityGroup; className?: string }) {
  if (group.iconKey) {
    const Cmp = getIconComponent(group.iconKey);
    if (Cmp) return <Cmp className={className} />;
  }
  if (group.iconUrl) {
    return <img src={group.iconUrl} alt="" className={className} />;
  }
  return <BlocksIcon className={className} />;
}

/** A custom (user) group row: rename via icon upload + delete. */
function GroupCard({ group }: { group: EntityGroup }) {
  const fileRef = useRef<HTMLInputElement>(null);
  return (
    <div className="flex items-center gap-3 rounded-lg border border-border bg-card px-3 py-2">
      <GroupIcon group={group} className="size-5 shrink-0 text-muted-foreground" />
      <span className="min-w-0 flex-1 truncate text-sm font-medium">{group.name}</span>
      {group.isBuiltin ? (
        <span className="text-[11px] text-muted-foreground">built-in</span>
      ) : (
        <>
          <input
            ref={fileRef}
            type="file"
            accept="image/png,image/jpeg,image/webp,image/svg+xml,image/gif"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void uploadEntityGroupIcon(group.id, f);
              e.target.value = "";
            }}
          />
          <Button variant="outline" size="sm" onClick={() => fileRef.current?.click()}>
            <ImageIcon className="size-3.5" /> Icon
          </Button>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Delete group"
            onClick={() => {
              if (window.confirm(`Delete group “${group.name}”? Its entities become ungrouped.`))
                void deleteEntityGroup(group.id);
            }}
          >
            <Trash2Icon className="size-3.5" />
          </Button>
        </>
      )}
    </div>
  );
}

function EntityCard({ entity, groups }: { entity: Entity; groups: EntityGroup[] }) {
  const [title, setTitle] = useState(entity.title);
  const [instruction, setInstruction] = useState(entity.instruction);
  const dirty = title !== entity.title || instruction !== entity.instruction;

  if (entity.isBuiltin) {
    // Built-in entities are read-only: show title + group, no editing.
    const group = groups.find((g) => g.id === entity.groupId);
    return (
      <div className="flex items-center gap-3 rounded-xl border border-border bg-card p-4">
        {group ? (
          <GroupIcon group={group} className="size-5 shrink-0 text-muted-foreground" />
        ) : (
          <BlocksIcon className="size-5 shrink-0 text-muted-foreground" />
        )}
        <span className="min-w-0 flex-1 truncate text-sm font-medium">{entity.title}</span>
        <span className="text-[11px] text-muted-foreground">
          {group ? `${group.name} · built-in` : "built-in"}
        </span>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2 rounded-xl border border-border bg-card p-4">
      <div className="flex items-start gap-3">
        <BlocksIcon className="mt-1 size-5 shrink-0 text-muted-foreground" />
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Title"
            className="w-full rounded-md border border-input bg-background px-2 py-1 text-sm font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          <textarea
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            placeholder="Instruction text (folded into the flow when this entity is used)"
            rows={2}
            className="w-full resize-y rounded-md border border-input bg-background px-2 py-1 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          <div className="flex items-center gap-2">
            <select
              aria-label="Group"
              value={entity.groupId ?? ""}
              onChange={(e) => updateEntity(entity.id, { groupId: e.target.value || null })}
              className="h-8 rounded-md border border-input bg-background px-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <option value="">No group</option>
              {groups
                .filter((g) => !g.isBuiltin)
                .map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.name}
                  </option>
                ))}
            </select>
            <span className="font-mono text-[11px] text-muted-foreground">{entity.id}</span>
            <span className="flex-1" />
            <Button
              variant="outline"
              size="sm"
              disabled={!dirty || !title.trim()}
              onClick={() =>
                updateEntity(entity.id, { title: title.trim(), instruction: instruction.trim() })
              }
            >
              Save
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="Delete entity"
              onClick={() => {
                if (window.confirm(`Delete “${entity.title}”?`)) deleteEntity(entity.id);
              }}
            >
              <Trash2Icon className="size-3.5" />
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

export function EntitiesPage() {
  const entities = useEntities();
  const groups = useEntityGroups();
  const [newGroup, setNewGroup] = useState("");

  return (
    <PageScroll contentClassName="px-6">
      <div className="mb-2 flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Entities</h1>
        <Button onClick={() => createEntity("New entity", "")}>
          <PlusIcon className="size-4" /> New entity
        </Button>
      </div>
      <p className="mb-6 text-sm text-muted-foreground">
        Reusable building blocks you can wire into any flow as a step, organized into groups.
        Built-in groups (Jira, GitHub) are read-only; create your own and upload an icon.
      </p>

      {/* Groups */}
      <h2 className="mb-2 text-sm font-semibold">Groups</h2>
      <div className="mb-3 flex items-center gap-2">
        <input
          value={newGroup}
          onChange={(e) => setNewGroup(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && newGroup.trim()) {
              void createEntityGroup(newGroup);
              setNewGroup("");
            }
          }}
          placeholder="New group name"
          className="h-8 w-64 rounded-md border border-input bg-background px-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        <Button
          variant="outline"
          size="sm"
          disabled={!newGroup.trim()}
          onClick={() => {
            void createEntityGroup(newGroup);
            setNewGroup("");
          }}
        >
          <PlusIcon className="size-3.5" /> Add group
        </Button>
      </div>
      <div className="mb-8 flex flex-col gap-2">
        {groups.map((g) => (
          <GroupCard key={g.id} group={g} />
        ))}
      </div>

      {/* Entities */}
      <h2 className="mb-2 text-sm font-semibold">Entities</h2>
      {entities.length === 0 ? (
        <div className="flex flex-col items-center gap-2 py-16 text-center">
          <BlocksIcon className="size-8 text-muted-foreground/50" />
          <p className="text-sm font-medium">No entities yet</p>
          <Button className="mt-2" variant="outline" onClick={() => createEntity("New entity", "")}>
            <PlusIcon className="size-4" /> New entity
          </Button>
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          {entities.map((e) => (
            <EntityCard key={e.id} entity={e} groups={groups} />
          ))}
        </div>
      )}
    </PageScroll>
  );
}
