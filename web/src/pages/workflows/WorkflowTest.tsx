import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeftIcon, RotateCcwIcon, SquareIcon } from "lucide-react";
import { Link } from "@/lib/routing";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { workflowRequest, type WorkflowRun } from "@/lib/workflowsApi";
import { WorkflowGraph, statusColor } from "./WorkflowGraph";

const terminal = new Set(["succeeded", "failed", "blocked"]);

export function WorkflowTest({ runId, onBack }: { runId: string; onBack: () => void }) {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const run = useQuery({
    queryKey: ["workflows", "run", runId],
    queryFn: () => workflowRequest<WorkflowRun>("/" + runId),
    refetchInterval: 1000,
  });
  const control = useMutation({
    mutationFn: (action: "resume" | "stop") =>
      workflowRequest<WorkflowRun>("/" + runId + "/" + action, {}),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
    },
  });
  const current = run.data;
  const selected =
    current?.workflow.nodes.find((node) => node.id === selectedId) ?? current?.workflow.nodes[0];
  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <Button variant="ghost" size="sm" onClick={onBack}>
            <ArrowLeftIcon className="size-4" />
            Back to designer
          </Button>
          <h1 className="mt-3 text-xl font-semibold">Test workflow</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {current?.workflow.name} · {current?.binding.mode} · {current?.status}
          </p>
        </div>
        {current && (
          <div className="flex flex-wrap items-center gap-2">
            {current.root_session_id && (
              <Link
                to={"/c/" + current.root_session_id}
                target="_blank"
                className="text-sm underline"
              >
                Open root session
              </Link>
            )}
            <Button
              variant="outline"
              disabled={current.active || terminal.has(current.status) || control.isPending}
              onClick={() => control.mutate("resume")}
            >
              <RotateCcwIcon className="size-4" />
              Resume
            </Button>
            <Button
              variant="outline"
              disabled={!current.active || control.isPending}
              onClick={() => control.mutate("stop")}
            >
              <SquareIcon className="size-4" />
              Stop controller
            </Button>
          </div>
        )}
      </header>
      {(run.error || control.error || current?.controller_error) && (
        <p role="alert" className="text-sm text-destructive">
          {run.error?.message ?? control.error?.message ?? current?.controller_error}
        </p>
      )}
      {run.isLoading && <p role="status">Loading test…</p>}
      {current && (
        <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_340px]">
          <WorkflowGraph
            tasks={current.workflow.nodes}
            selectedId={selected?.id ?? null}
            onSelect={setSelectedId}
            states={current.nodes}
          />
          {selected && <NodeDetails key={selected.id} run={current} node={selected} />}
        </div>
      )}
    </div>
  );
}

export function NodeDetails({
  run,
  node,
}: {
  run: WorkflowRun;
  node: WorkflowRun["workflow"]["nodes"][number];
}) {
  const queryClient = useQueryClient();
  const [message, setMessage] = useState("");
  const [rawEvent, setRawEvent] = useState("");
  const [parseError, setParseError] = useState<string | null>(null);
  const state = run.nodes[node.id];
  const event = useMutation({
    mutationFn: async (payload: Record<string, unknown>) => {
      await workflowRequest("/" + run.run_id + "/events", {
        ...payload,
        id: crypto.randomUUID(),
        node_id: node.id,
      });
      if (!run.active) {
        try {
          await workflowRequest("/" + run.run_id + "/resume", {});
        } catch (error) {
          throw new Error(`Event queued. Resume the workflow when ready: ${String(error)}`, {
            cause: error,
          });
        }
      }
    },
    onSuccess: () => {
      setMessage("");
      setRawEvent("");
      setParseError(null);
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["workflows"] });
    },
  });
  const liveNode = !terminal.has(state.status);
  const outcome = state.status === "succeeded" ? state : state.candidate;
  return (
    <section className="space-y-4 rounded-xl border p-4" aria-label={"Task " + node.id}>
      <div>
        <h3 className="font-medium">{node.name || node.id}</h3>
        <p className="mt-1 text-sm text-muted-foreground">{node.goal}</p>
      </div>
      {state.error && (
        <p role="alert" className="text-sm text-destructive">
          {state.error}
        </p>
      )}
      {outcome && (
        <div className="rounded-lg bg-muted p-3 text-sm">
          <p className="mb-1 text-xs text-muted-foreground">
            {state.status === "succeeded" ? "Accepted outcome" : `Candidate ${state.candidate_id}`}
          </p>
          <p className="whitespace-pre-wrap">{outcome.summary}</p>
          {Object.keys(outcome.outputs ?? {}).length > 0 && (
            <div className="mt-3">
              <p className="text-xs text-muted-foreground">Outputs for dependent tasks</p>
              <pre className="mt-1 whitespace-pre-wrap break-all text-xs">
                {JSON.stringify(outcome.outputs, null, 2)}
              </pre>
            </div>
          )}
          {outcome.artifacts.map((artifact) => (
            <p key={artifact.id} className="mt-2 break-all font-mono text-xs">
              {artifact.id} · {artifact.revision}
              <br />
              {artifact.uri}
            </p>
          ))}
        </div>
      )}
      {state.gate_results.map((gate) => (
        <p key={gate.candidate_id + gate.gate_id} className="text-sm">
          <span className={gate.outcome.passed ? statusColor.succeeded : statusColor.failed}>
            {gate.outcome.passed ? "Passed" : "Failed"}
          </span>
          {" · " + gate.gate_id + " · " + gate.candidate_id + ": " + gate.outcome.detail}
        </p>
      ))}
      {Object.entries(state.members).map(([id, member]) => (
        <div key={id} className="text-xs text-muted-foreground">
          <p className="break-all">
            {id} · Session: {member.session_id ?? "Not created"}
          </p>
          {member.cleanup_error && <p className="mt-1 text-amber-600">{member.cleanup_error}</p>}
          {Object.entries(member.turns)
            .filter(([, turn]) => turn.status === "uncertain")
            .map(([turnId, turn]) => (
              <p key={turnId} className="mt-1 break-all">
                Needs reconciliation: {turnId}
                {turn.remote_id && " · " + turn.remote_id}
              </p>
            ))}
        </div>
      ))}
      {liveNode && (
        <div className="space-y-2">
          <label className="text-sm" htmlFor="workflow-feedback">
            Feedback or validation evidence
          </label>
          <Textarea
            id="workflow-feedback"
            value={message}
            onChange={(change) => setMessage(change.target.value)}
            placeholder="Describe what passed, what failed, or what should change."
          />
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              disabled={!message.trim() || event.isPending}
              onClick={() => event.mutate({ kind: "feedback", message })}
            >
              Send feedback
            </Button>
            {state.candidate?.status === "ready" &&
              node.gates
                .filter(
                  (gate) =>
                    !state.gate_results.some(
                      (result) =>
                        result.candidate_id === state.candidate_id && result.gate_id === gate.id,
                    ),
                )
                .map((gate) => (
                  <div className="flex gap-2" key={gate.id}>
                    {[true, false].map((passed) => (
                      <Button
                        key={String(passed)}
                        variant="outline"
                        disabled={!message.trim() || event.isPending}
                        onClick={() =>
                          event.mutate({
                            kind: "gate_result",
                            candidate_id: state.candidate_id,
                            gate_id: gate.id,
                            outcome: { passed, detail: message },
                          })
                        }
                      >
                        {passed ? "Pass " : "Fail "}
                        {gate.id}
                      </Button>
                    ))}
                  </div>
                ))}
          </div>
        </div>
      )}
      {event.error && (
        <p role="alert" className="text-sm text-destructive">
          {event.error.message}
        </p>
      )}
      {event.isSuccess && (
        <p role="status" className="text-sm text-muted-foreground">
          Event submitted. The controller applies it at the next safe boundary.
        </p>
      )}
      {run.events
        .filter((item) => item.status === "rejected")
        .map((item) => (
          <p key={item.event.id} className="text-xs text-amber-600">
            Event rejected: {item.error}
          </p>
        ))}
      <details className="text-sm">
        <summary className="cursor-pointer text-muted-foreground">Candidate history</summary>
        <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all text-xs">
          {JSON.stringify(state.candidates, null, 2)}
        </pre>
      </details>
      {Object.values(state.members).some((member) =>
        Object.values(member.turns).some((turn) => turn.status === "uncertain"),
      ) && (
        <details className="space-y-2 text-sm">
          <summary className="cursor-pointer">Reconcile an uncertain turn</summary>
          <p className="text-xs text-muted-foreground">
            Inspect the remote session first, then submit a resolve_turn event with member_id,
            turn_id, session_id, and its actual output.
          </p>
          <Textarea
            aria-label="Reconciliation event JSON"
            value={rawEvent}
            onChange={(change) => setRawEvent(change.target.value)}
          />
          <Button
            disabled={!rawEvent.trim() || event.isPending}
            onClick={() => {
              try {
                const payload: unknown = JSON.parse(rawEvent);
                if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
                  throw new Error("Expected a JSON object");
                }
                setParseError(null);
                event.mutate(payload as Record<string, unknown>);
              } catch {
                setParseError("Enter a valid JSON object. Your input has been preserved.");
              }
            }}
          >
            Submit reconciliation
          </Button>
          {parseError && (
            <p role="alert" className="text-sm text-destructive">
              {parseError}
            </p>
          )}
        </details>
      )}
    </section>
  );
}
