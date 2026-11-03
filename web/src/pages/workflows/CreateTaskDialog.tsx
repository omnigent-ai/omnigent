import { useState } from "react";
import { PlusIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { TaskEditor } from "./TaskEditor";
import {
  DEFAULT_TASK_OPTIONS,
  newExecution,
  nextIdentifier,
  type TaskDesign,
} from "./workflowDesign";

export function CreateTaskDialog({
  tasks,
  selectedTask,
  onCreate,
}: {
  tasks: TaskDesign[];
  selectedTask?: TaskDesign;
  onCreate: (task: TaskDesign) => void;
}) {
  const [task, setTask] = useState<TaskDesign | null>(null);
  const ready = Boolean(task?.name?.trim() && task.goal.trim());
  return (
    <Dialog
      open={task !== null}
      onOpenChange={(open) =>
        setTask(
          open
            ? {
                ...DEFAULT_TASK_OPTIONS,
                execution: newExecution(),
                id: nextIdentifier(
                  "task",
                  tasks.map((node) => node.id),
                ),
                name: "",
                goal: "",
                depends_on: selectedTask ? [selectedTask.id] : [],
                gates: [],
              }
            : null,
        )
      }
    >
      <DialogTrigger asChild>
        <Button variant="outline" size="sm" disabled={tasks.length >= 64}>
          <PlusIcon className="size-4" />
          Add task
        </Button>
      </DialogTrigger>
      {task && (
        <DialogContent className="max-h-[85dvh] grid-rows-[auto_minmax(0,1fr)_auto] sm:max-w-xl">
          <form
            className="contents"
            onSubmit={(event) => {
              event.preventDefault();
              if (!ready) return;
              onCreate({ ...task, name: task.name!.trim(), goal: task.goal.trim() });
              setTask(null);
            }}
          >
            <DialogHeader>
              <DialogTitle>Add task</DialogTitle>
              <DialogDescription>
                Define what this task should accomplish and what needs to happen first. A name and
                goal are required.
              </DialogDescription>
            </DialogHeader>
            <div className="min-h-0 overflow-y-auto pr-1">
              <TaskEditor creating tasks={[...tasks, task]} task={task} onChange={setTask} />
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setTask(null)}>
                Cancel
              </Button>
              <Button type="submit" disabled={!ready}>
                Create task
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      )}
    </Dialog>
  );
}
