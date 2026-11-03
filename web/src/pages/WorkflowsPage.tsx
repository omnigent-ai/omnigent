import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CopyIcon, DownloadIcon, FileCodeIcon, PlayIcon, WorkflowIcon } from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { triggerBrowserDownload } from "@/hooks/useFileContent";
import { copyText } from "@/lib/clipboard";
import {
  workflowRequest,
  type WorkflowBackend,
  type WorkflowOptions,
  type WorkflowRun,
} from "@/lib/workflowsApi";
import {
  canConnect,
  executionDefinition,
  removeTask,
  starterWorkflow,
  type WorkflowDesign,
} from "./workflows/workflowDesign";
import { WorkflowGraph } from "./workflows/WorkflowGraph";
import { CreateTaskDialog } from "./workflows/CreateTaskDialog";
import { TaskEditor } from "./workflows/TaskEditor";
import { Field, selectClass } from "./workflows/fields";
import { WorkflowTest } from "./workflows/WorkflowTest";

export function WorkflowsPage() {
  const queryClient = useQueryClient();
  const [workflow, setWorkflow] = useState(starterWorkflow);
  const [selectedId, setSelectedId] = useState<string | null>("investigate");
  const [testLive, setTestLive] = useState(false);
  const [runId, setRunId] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [exported, setExported] = useState<string | null>(null);
  const [copyMessage, setCopyMessage] = useState<string | null>(null);
  const options = useQuery({
    queryKey: ["workflows", "options"],
    queryFn: () => workflowRequest<WorkflowOptions>("/options"),
  });
  const backend: WorkflowBackend = testLive ? "arclet" : "simulation";
  const testEnabled = options.data?.backends.some((item) => item.id === backend && item.enabled);
  const unavailable = options.data?.backends.filter((item) => item.id === backend && !item.enabled);
  const selected = workflow.nodes.find((node) => node.id === selectedId) ?? workflow.nodes[0];
  const lastTest = useQuery({
    queryKey: ["workflows", "run", runId],
    queryFn: () => workflowRequest<WorkflowRun>("/" + runId),
    enabled: runId !== null && !testing,
    refetchInterval: 2000,
  });
  const compiling = useMutation({
    mutationFn: async ({
      action,
      design,
      testBackend,
    }: {
      action: "export" | "test";
      design: WorkflowDesign;
      testBackend: WorkflowBackend;
    }) => {
      const result = await workflowRequest<{ definition: string }>("/definition", {
        backend: "arclet",
        workflow: executionDefinition(design),
      });
      if (action === "export") return { definition: result.definition };
      const run = await workflowRequest<WorkflowRun>("", {
        backend: testBackend,
        definition: result.definition,
      });
      return { run };
    },
    onSuccess: (result) => {
      if (result.run) {
        queryClient.setQueryData(["workflows", "run", result.run.run_id], result.run);
        setRunId(result.run.run_id);
        setTesting(true);
      } else {
        setExported(result.definition ?? null);
        setCopyMessage(null);
      }
    },
  });
  const submit = (action: "export" | "test") =>
    compiling.mutate({ action, design: workflow, testBackend: backend });
  const connect = (source: string, target: string) => {
    if (!canConnect(workflow.nodes, source, target)) return;
    setWorkflow({
      ...workflow,
      nodes: workflow.nodes.map((node) =>
        node.id === target ? { ...node, depends_on: [...node.depends_on, source] } : node,
      ),
    });
  };
  return (
    <PageScroll maxWidthClassName="max-w-[1440px]" extraTop="1rem">
      <div className="space-y-5 p-4 sm:p-6">
        {testing && runId ? (
          <WorkflowTest runId={runId} onBack={() => setTesting(false)} />
        ) : (
          <>
            <header className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <h1 className="flex items-center gap-2 text-xl font-semibold">
                  <WorkflowIcon className="size-5" />
                  Create workflow
                </h1>
                <p className="mt-2 max-w-xl text-sm text-muted-foreground">
                  Define tasks, connect their prerequisites, and decide when work can proceed.
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {runId && (
                  <Button variant="ghost" onClick={() => setTesting(true)}>
                    View test
                  </Button>
                )}
                <Button
                  variant="outline"
                  disabled={compiling.isPending}
                  onClick={() => submit("export")}
                >
                  <FileCodeIcon className="size-4" />
                  Export YAML
                </Button>
                <Field label="Test using">
                  <select
                    className={selectClass}
                    value={testLive ? "live" : "simulation"}
                    onChange={(event) => setTestLive(event.target.value === "live")}
                  >
                    <option value="simulation">Simulation</option>
                    <option value="live">Task agents</option>
                  </select>
                </Field>
                <Button
                  disabled={compiling.isPending || !testEnabled || Boolean(lastTest.data?.active)}
                  onClick={() => submit("test")}
                >
                  <PlayIcon className="size-4" />
                  Test workflow
                </Button>
              </div>
            </header>
            <p className="text-xs text-muted-foreground">
              {testLive
                ? "Tests use the runtime and agents configured in each task."
                : "Simulation exercises the graph without launching agents."}
            </p>
            {unavailable?.map((item) => (
              <p key={item.id} role="status" className="text-sm text-muted-foreground">
                {item.reason}
              </p>
            ))}
            {(compiling.error || options.error) && (
              <p
                role="alert"
                className="whitespace-pre-wrap rounded-lg border border-destructive p-3 text-sm text-destructive"
              >
                {compiling.error?.message ?? options.error?.message}
              </p>
            )}
            {compiling.isPending && (
              <p role="status" className="text-sm text-muted-foreground">
                Validating the design…
              </p>
            )}
            <section aria-label="Workflow settings" className="rounded-xl border bg-card p-4">
              <div className="space-y-3">
                <Field label="Workflow name">
                  <Input
                    value={workflow.name}
                    onChange={(event) => setWorkflow({ ...workflow, name: event.target.value })}
                  />
                </Field>
                <Field label="Workflow goal">
                  <Textarea
                    className="min-h-16"
                    value={workflow.goal}
                    onChange={(event) => setWorkflow({ ...workflow, goal: event.target.value })}
                  />
                </Field>
              </div>
            </section>
            <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
              <section className="min-w-0 space-y-3" aria-label="Workflow design">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <h2 className="font-medium">Task graph</h2>
                    <p className="mt-1 text-xs text-muted-foreground">
                      Arrows point from a prerequisite to the task that waits for it.
                    </p>
                  </div>
                  <CreateTaskDialog
                    tasks={workflow.nodes}
                    selectedTask={selected}
                    onCreate={(task) => {
                      setWorkflow((current) => ({ ...current, nodes: [...current.nodes, task] }));
                      setSelectedId(task.id);
                    }}
                  />
                </div>
                <WorkflowGraph
                  tasks={workflow.nodes}
                  selectedId={selected?.id ?? null}
                  onSelect={setSelectedId}
                  onConnect={connect}
                  onDisconnect={(source, target) =>
                    setWorkflow((current) => ({
                      ...current,
                      nodes: current.nodes.map((node) =>
                        node.id === target
                          ? { ...node, depends_on: node.depends_on.filter((id) => id !== source) }
                          : node,
                      ),
                    }))
                  }
                />
                <p className="text-xs text-muted-foreground">
                  Drag between connection points, or select a task and choose “Wait for”. To remove
                  an arrow, click or tap it, then choose “Remove arrow”. Export YAML to save your
                  design.
                </p>
              </section>
              {selected && (
                <TaskEditor
                  key={selected.id}
                  tasks={workflow.nodes}
                  task={selected}
                  onChange={(updated) =>
                    setWorkflow((current) => ({
                      ...current,
                      nodes: current.nodes.map((task) => (task.id === updated.id ? updated : task)),
                    }))
                  }
                  onDelete={() => {
                    setWorkflow(removeTask(workflow, selected.id));
                    setSelectedId(null);
                  }}
                />
              )}
            </div>
          </>
        )}
      </div>
      <Dialog
        open={exported !== null}
        onOpenChange={(open) => {
          if (!open) setExported(null);
        }}
      >
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Workflow YAML</DialogTitle>
            <DialogDescription>
              Generated from your design and validated against the workflow API. Use this file with
              the CLI or submit its contents as the API’s definition.
            </DialogDescription>
          </DialogHeader>
          <Textarea
            aria-label="Generated YAML"
            readOnly
            value={exported ?? ""}
            className="h-80 font-mono text-xs [field-sizing:fixed]"
          />
          <div className="flex gap-2">
            <Button
              variant="outline"
              onClick={() => {
                void copyText(exported ?? "").then(
                  () => setCopyMessage("Copied YAML"),
                  () => setCopyMessage("Copy unavailable. Select the YAML or download the file."),
                );
              }}
            >
              <CopyIcon className="size-4" />
              Copy YAML
            </Button>
            <Button
              onClick={() =>
                triggerBrowserDownload(
                  new Blob([exported ?? ""], { type: "application/yaml" }),
                  "workflow.yaml",
                )
              }
            >
              <DownloadIcon className="size-4" />
              Download YAML
            </Button>
          </div>
          {copyMessage && (
            <p role="status" className="text-sm text-muted-foreground">
              {copyMessage}
            </p>
          )}
        </DialogContent>
      </Dialog>
    </PageScroll>
  );
}
