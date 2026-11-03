import { Trash2Icon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { canConnect, DEFAULT_TASK_OPTIONS, type TaskDesign } from "./workflowDesign";
import { Field } from "./fields";
import { ExecutionSettings } from "./ExecutionSettings";

export function TaskEditor({
  tasks,
  task,
  onChange,
  onDelete,
  creating = false,
}: {
  tasks: TaskDesign[];
  task: TaskDesign;
  onChange: (task: TaskDesign) => void;
  onDelete?: () => void;
  creating?: boolean;
}) {
  const updateTask = (patch: Partial<TaskDesign>) => onChange({ ...task, ...patch });
  return (
    <section
      aria-label="Task settings"
      className={cn("min-w-0 space-y-4", !creating && "rounded-xl border bg-card p-4")}
    >
      {!creating && (
        <div className="flex items-center justify-between gap-2">
          <h2 className="font-medium">Task settings</h2>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Delete task"
            disabled={tasks.length === 1}
            onClick={onDelete}
          >
            <Trash2Icon className="size-4" />
          </Button>
        </div>
      )}
      <Field label="Task name">
        <Input
          required={creating}
          placeholder="e.g. Verify acceptance criteria"
          value={task.name ?? task.id}
          onChange={(event) => updateTask({ name: event.target.value })}
        />
      </Field>
      <Field label="Task goal">
        <Textarea
          required={creating}
          className={creating ? "min-h-24" : undefined}
          value={task.goal}
          onChange={(event) => updateTask({ goal: event.target.value })}
          placeholder="What should this task accomplish?"
        />
      </Field>
      <fieldset className="space-y-2">
        <legend className="mb-2 text-sm font-medium">Wait for</legend>
        <p className="text-xs text-muted-foreground">
          All selected tasks must succeed first. Independent branches can run together.
        </p>
        {tasks
          .filter((node) => node.id !== task.id)
          .map((node) => {
            const checked = task.depends_on.includes(node.id);
            const allowed = checked || canConnect(tasks, node.id, task.id);
            return (
              <label
                key={node.id}
                className="flex items-center gap-2 text-sm"
                title={!allowed ? "This dependency would create a cycle" : undefined}
              >
                <input
                  type="checkbox"
                  className="size-4 accent-primary"
                  checked={checked}
                  disabled={!allowed}
                  onChange={() =>
                    updateTask({
                      depends_on: checked
                        ? task.depends_on.filter((id) => id !== node.id)
                        : [...task.depends_on, node.id],
                    })
                  }
                />
                <span className={!allowed ? "text-muted-foreground" : undefined}>
                  {node.name || node.id}
                </span>
              </label>
            );
          })}
        {tasks.length === 1 && (
          <p className="text-xs text-muted-foreground">Add another task to connect it.</p>
        )}
      </fieldset>
      <label className="flex items-start gap-2 text-sm">
        <input
          type="checkbox"
          className="mt-0.5 size-4 accent-primary"
          checked={task.gates.length > 0}
          onChange={(event) =>
            updateTask({
              gates: event.target.checked
                ? [{ id: "review", kind: "external", artifacts: ["result"] }]
                : [],
            })
          }
        />
        Require review before continuing
      </label>
      {task.gates.length > 0 && (
        <p className="text-xs text-muted-foreground">
          Review the result before dependent work starts. A failed review requests a revision.
        </p>
      )}
      <ExecutionSettings
        execution={task.execution}
        onChange={(execution) => updateTask({ execution })}
      />
      <details className="space-y-3 border-t pt-3">
        <summary className="cursor-pointer text-sm font-medium">Task options</summary>
        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            className="mt-0.5 size-4 accent-primary"
            checked={task.allow_expansion ?? DEFAULT_TASK_OPTIONS.allow_expansion}
            onChange={(event) => updateTask({ allow_expansion: event.target.checked })}
          />
          Allow follow-ups
        </label>
        <p className="text-xs text-muted-foreground">
          Allow this task to add follow-up work to the DAG.
        </p>
        <Field label="Retries">
          <Input
            type="number"
            min={0}
            max={99}
            value={(task.max_rounds ?? DEFAULT_TASK_OPTIONS.max_rounds) - 1}
            onChange={(event) => updateTask({ max_rounds: Number(event.target.value) + 1 })}
          />
        </Field>
        <p className="text-xs text-muted-foreground">
          Extra attempts for feedback, specialist input, or failed review, after the first attempt.
        </p>
      </details>
    </section>
  );
}
