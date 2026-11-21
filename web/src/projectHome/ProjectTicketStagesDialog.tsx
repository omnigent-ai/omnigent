import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  DndContext,
  KeyboardSensor,
  MouseSensor,
  TouchSensor,
  closestCenter,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import {
  SortableContext,
  arrayMove,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { GripVerticalIcon, PlusIcon, Trash2Icon } from "lucide-react";
import { customAlphabet } from "nanoid";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import {
  updateTicketStages,
  type TicketStage,
  type TicketStageChanges,
  type TicketStages,
} from "@/lib/projectTicketsApi";
import { TicketError } from "./TicketControls";
import { useTicketProject, useTicketStages } from "./useProjectTickets";

const stageToken = customAlphabet("abcdefghijklmnopqrstuvwxyz0123456789", 12);
const NO_MOVE_DESTINATION = "__none__";

export function ProjectTicketStagesDialog({
  projectName,
  onClose,
}: {
  projectName: string;
  onClose: () => void;
}) {
  const { projectId, projects, ensure } = useTicketProject(projectName);
  const query = useTicketStages(projectId, true);
  const [loaded, setLoaded] = useState<TicketStages | null>(null);
  useEffect(() => {
    if (!projects.isLoading && !projects.error && query.data) setLoaded(query.data);
  }, [projects.isLoading, projects.error, query.data]);
  const loadError = projects.error ?? query.error;
  const client = useQueryClient();
  const save = useMutation({
    mutationFn: async (changes: TicketStageChanges) => {
      const id = projectId ?? (await ensure.mutateAsync());
      await updateTicketStages(id, changes);
      return id;
    },
    onSuccess: (id) => {
      void client.invalidateQueries({ queryKey: ["project-tickets", id] });
      onClose();
    },
    onError: () => {
      if (projectId) void query.refetch();
    },
  });
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !save.isPending) onClose();
      }}
    >
      <DialogContent
        className="max-h-[85dvh] overflow-y-auto sm:max-w-2xl"
        aria-describedby={undefined}
      >
        <DialogHeader>
          <DialogTitle>Edit stages</DialogTitle>
        </DialogHeader>
        {loaded ? (
          <StageEditor
            configuration={loaded}
            pending={save.isPending}
            error={save.error}
            onSave={(changes) => save.mutate(changes)}
            onClose={onClose}
          />
        ) : (
          <>
            {(projects.isLoading || query.isPending) && <Spinner aria-label="Loading stages" />}
            <TicketError error={loadError} />
            {loadError && (
              <Button
                variant="ghost"
                onClick={() => void (projects.error ? projects.refetch() : query.refetch())}
              >
                Try again
              </Button>
            )}
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

function StageEditor({
  configuration,
  pending,
  error,
  onSave,
  onClose,
}: {
  configuration: TicketStages;
  pending: boolean;
  error: Error | null;
  onSave: (changes: TicketStageChanges) => void;
  onClose: () => void;
}) {
  const [stages, setStages] = useState(() => configuration.stages.map((stage) => ({ ...stage })));
  const [moves, setMoves] = useState<Record<string, string>>({});
  const removed = configuration.stages.filter(
    (stage) => !stages.some((current) => current.id === stage.id),
  );
  const occupied = new Set(configuration.occupied_stage_ids ?? []);
  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 5 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );
  const names = stages.map((stage) => stage.name.trim().toLocaleLowerCase());
  const validation = names.some((name) => !name)
    ? "Give every stage a name."
    : new Set(names).size !== names.length
      ? "Stage names must be unique."
      : removed.some(
            (stage) =>
              occupied.has(stage.id) && !stages.some((target) => target.id === moves[stage.id]),
          )
        ? "Choose where tickets from each removed stage should move."
        : null;
  const change = (id: string, fields: Partial<TicketStage>) =>
    setStages((current) =>
      current.map((stage) => (stage.id === id ? { ...stage, ...fields } : stage)),
    );
  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (pending || validation) return;
        onSave({
          stages: stages.map((stage, index) => ({
            ...stage,
            name: stage.name.trim(),
            instructions: stage.instructions.trim(),
            is_complete: index === stages.length - 1,
          })),
          initial_stage_id: stages[0].id,
          moves: Object.fromEntries(
            removed
              .filter((stage) => stages.some((target) => target.id === moves[stage.id]))
              .map((stage) => [stage.id, moves[stage.id]]),
          ),
        });
      }}
    >
      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragEnd={({ active, over }) => {
          if (pending || !over || active.id === over.id) return;
          const from = stages.findIndex((stage) => stage.id === active.id);
          const to = stages.findIndex((stage) => stage.id === over.id);
          if (from >= 0 && to >= 0) setStages(arrayMove(stages, from, to));
        }}
      >
        <SortableContext
          items={stages.map((stage) => stage.id)}
          strategy={verticalListSortingStrategy}
        >
          <ol aria-label="Project stages" className="space-y-2">
            {stages.map((stage, index) => (
              <StageRow
                key={stage.id}
                stage={stage}
                index={index}
                pending={pending}
                onlyStage={stages.length === 1}
                onChange={(fields) => change(stage.id, fields)}
                onRemove={() => {
                  const remaining = stages.filter((current) => current.id !== stage.id);
                  setStages(remaining);
                }}
              />
            ))}
          </ol>
        </SortableContext>
      </DndContext>
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={pending || stages.length >= 20}
        onClick={() =>
          setStages([
            ...stages,
            { id: `stage-${stageToken()}`, name: "", instructions: "", is_complete: false },
          ])
        }
      >
        <PlusIcon aria-hidden /> Add stage
      </Button>
      {removed.map((stage) => (
        <div key={stage.id} className="space-y-2 rounded-lg border border-border/60 p-3 text-sm">
          <div className="flex items-center justify-between gap-2">
            <p>
              Removing <span className="font-medium">{stage.name}</span>. Tickets and history are
              kept.
            </p>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={pending || stages.length >= 20}
              onClick={() => setStages([...stages, stage])}
            >
              Undo removal of {stage.name}
            </Button>
          </div>
          <label className="grid gap-1.5">
            Move tickets from {stage.name} to
            <Select
              value={stages.some((target) => target.id === moves[stage.id]) ? moves[stage.id] : ""}
              disabled={pending}
              onValueChange={(value) =>
                setMoves({ ...moves, [stage.id]: value === NO_MOVE_DESTINATION ? "" : value })
              }
            >
              <SelectTrigger aria-label={`Move tickets from ${stage.name} to`} className="w-full">
                <SelectValue
                  placeholder={
                    occupied.has(stage.id)
                      ? "Choose a stage…"
                      : "No tickets currently — choose a fallback (optional)"
                  }
                />
              </SelectTrigger>
              <SelectContent position="popper">
                <SelectItem value={NO_MOVE_DESTINATION}>
                  {occupied.has(stage.id) ? "Choose a stage…" : "No fallback"}
                </SelectItem>
                {stages.map((target) => (
                  <SelectItem key={target.id} value={target.id}>
                    {target.name || "Unnamed stage"}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>
        </div>
      ))}
      {validation && <p className="text-sm text-muted-foreground">{validation}</p>}
      <TicketError error={error} />
      <DialogFooter>
        <Button type="button" variant="ghost" disabled={pending} onClick={onClose}>
          Cancel
        </Button>
        <Button type="submit" loading={pending} disabled={!!validation}>
          Save stages
        </Button>
      </DialogFooter>
    </form>
  );
}

function StageRow({
  stage,
  index,
  onlyStage,
  pending,
  onChange,
  onRemove,
}: {
  stage: TicketStage;
  index: number;
  onlyStage: boolean;
  pending: boolean;
  onChange: (fields: Partial<TicketStage>) => void;
  onRemove: () => void;
}) {
  const {
    attributes,
    listeners,
    setNodeRef,
    setActivatorNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: stage.id, disabled: pending });
  return (
    <li
      ref={setNodeRef}
      className="flex items-center gap-2 rounded-lg border border-border/60 bg-background p-3"
      style={{
        transform: transform ? `translate3d(${transform.x}px, ${transform.y}px, 0)` : undefined,
        transition,
        opacity: isDragging ? 0.5 : 1,
      }}
    >
      <Button
        ref={setActivatorNodeRef}
        {...attributes}
        {...listeners}
        type="button"
        variant="ghost"
        size="icon-sm"
        className="touch-none text-muted-foreground"
        disabled={pending}
        aria-label={`Reorder ${stage.name || `stage ${index + 1}`}`}
      >
        <GripVerticalIcon aria-hidden />
      </Button>
      <div className="grid min-w-0 flex-1 gap-2">
        <Input
          aria-label={`Stage name ${index + 1}`}
          required
          maxLength={60}
          value={stage.name}
          disabled={pending}
          placeholder="Stage name"
          onChange={(event) => onChange({ name: event.target.value })}
        />
        <Input
          aria-label={`Instructions for ${stage.name || `stage ${index + 1}`}`}
          value={stage.instructions}
          maxLength={500}
          disabled={pending}
          placeholder="Optional instruction for this stage…"
          onChange={(event) => onChange({ instructions: event.target.value })}
        />
      </div>
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        disabled={pending || onlyStage}
        aria-label={`Remove ${stage.name || `stage ${index + 1}`}`}
        onClick={onRemove}
      >
        <Trash2Icon aria-hidden />
      </Button>
    </li>
  );
}
