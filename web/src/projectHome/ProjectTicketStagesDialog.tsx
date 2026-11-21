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
  const [initialStageId, setInitialStageId] = useState(configuration.initial_stage_id);
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
      : !stages.some((stage) => stage.id === initialStageId && !stage.is_complete)
        ? "Choose an unfinished starting stage."
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
          stages: stages.map((stage) => ({
            ...stage,
            name: stage.name.trim(),
            instructions: stage.instructions.trim(),
          })),
          initial_stage_id: initialStageId,
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
                initial={stage.id === initialStageId}
                onlyStage={stages.length === 1}
                onChange={(fields) => change(stage.id, fields)}
                onStart={() => setInitialStageId(stage.id)}
                onRemove={() => {
                  const remaining = stages.filter((current) => current.id !== stage.id);
                  setStages(remaining);
                  if (stage.id === initialStageId)
                    setInitialStageId(remaining.find((current) => !current.is_complete)?.id ?? "");
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
            <select
              aria-label={`Move tickets from ${stage.name} to`}
              value={stages.some((target) => target.id === moves[stage.id]) ? moves[stage.id] : ""}
              disabled={pending}
              className="h-9 rounded-md border bg-background px-2"
              onChange={(event) => setMoves({ ...moves, [stage.id]: event.target.value })}
            >
              <option value="">
                {occupied.has(stage.id)
                  ? "Choose a stage…"
                  : "No tickets currently — choose a fallback (optional)"}
              </option>
              {stages.map((target) => (
                <option key={target.id} value={target.id}>
                  {target.name || "Unnamed stage"}
                </option>
              ))}
            </select>
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
  initial,
  onlyStage,
  pending,
  onChange,
  onStart,
  onRemove,
}: {
  stage: TicketStage;
  index: number;
  initial: boolean;
  onlyStage: boolean;
  pending: boolean;
  onChange: (fields: Partial<TicketStage>) => void;
  onStart: () => void;
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
      className="space-y-2 rounded-lg border border-border/60 bg-background p-3"
      style={{
        transform: transform ? `translate3d(${transform.x}px, ${transform.y}px, 0)` : undefined,
        transition,
        opacity: isDragging ? 0.5 : 1,
      }}
    >
      <div className="flex items-center gap-2">
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
        <Input
          aria-label={`Stage name ${index + 1}`}
          required
          maxLength={60}
          value={stage.name}
          disabled={pending}
          placeholder="Stage name"
          onChange={(event) => onChange({ name: event.target.value })}
        />
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
      </div>
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 pl-1 text-sm">
        <label className="flex items-center gap-2">
          <input
            type="radio"
            name="initial-ticket-stage"
            checked={initial}
            disabled={pending || stage.is_complete}
            onChange={onStart}
            aria-label={`Start in ${stage.name || `stage ${index + 1}`}`}
          />
          Starting stage
        </label>
        <label
          className="flex items-center gap-2"
          title={initial ? "Choose another starting stage first" : undefined}
        >
          <input
            type="checkbox"
            checked={stage.is_complete}
            disabled={pending || initial}
            aria-label={`${stage.name || `Stage ${index + 1}`} means work is complete`}
            onChange={(event) => onChange({ is_complete: event.target.checked })}
          />
          Work is complete
        </label>
      </div>
      <Input
        aria-label={`Instructions for ${stage.name || `stage ${index + 1}`}`}
        value={stage.instructions}
        maxLength={500}
        disabled={pending}
        placeholder="Optional instruction for this stage…"
        onChange={(event) => onChange({ instructions: event.target.value })}
      />
    </li>
  );
}
