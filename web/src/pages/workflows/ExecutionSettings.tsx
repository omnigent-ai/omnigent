import { PlusIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Field, selectClass } from "./fields";
import { newMember, nextIdentifier, type TaskExecution } from "./workflowDesign";

export function ExecutionSettings({
  execution,
  onChange,
}: {
  execution: TaskExecution;
  onChange: (execution: TaskExecution) => void;
}) {
  return (
    <details className="rounded-xl border bg-card p-4">
      <summary className="cursor-pointer text-sm font-medium">
        <span>Execution settings</span>
        <span className="ml-2 text-xs font-normal text-muted-foreground">Managed sandbox</span>
      </summary>
      <div className="mt-4 space-y-4">
        <p className="text-xs text-muted-foreground">
          Choose how this task runs. Each agent uses a sandbox provider configured on your server.
        </p>
        <Field label="Execution mode">
          <select
            className={selectClass}
            value={execution.strategy}
            onChange={(event) =>
              onChange({ ...execution, strategy: event.target.value as TaskExecution["strategy"] })
            }
          >
            <option value="owner">Owner with specialists on request</option>
            <option value="parallel">Everyone contributes, then the lead summarizes</option>
          </select>
        </Field>
        <Field label="Lead agent">
          <select
            className={selectClass}
            value={execution.lead}
            onChange={(event) => onChange({ ...execution, lead: event.target.value })}
          >
            {execution.members.map((member) => (
              <option key={member.id} value={member.id}>
                {member.id}
              </option>
            ))}
          </select>
        </Field>
        {execution.members.map((member) => {
          const updateMember = (patch: Partial<typeof member>) =>
            onChange({
              ...execution,
              members: execution.members.map((item) =>
                item.id === member.id ? { ...item, ...patch } : item,
              ),
            });
          return (
            <fieldset className="space-y-3 rounded-lg border p-3" key={member.id}>
              <legend className="px-1 text-xs font-medium">{member.id}</legend>
              <Field label={`${member.id} agent ID`}>
                <Input
                  value={member.agent_id}
                  onChange={(event) => updateMember({ agent_id: event.target.value })}
                />
              </Field>
              <Field label={`${member.id} role`}>
                <Textarea
                  value={member.role}
                  onChange={(event) => updateMember({ role: event.target.value })}
                />
              </Field>
              <Field label={`${member.id} sandbox provider`}>
                <Input
                  value={member.placement.provider}
                  onChange={(event) =>
                    updateMember({
                      placement: { ...member.placement, provider: event.target.value },
                    })
                  }
                />
              </Field>
              <Field label={`${member.id} repository (optional)`}>
                <Input
                  value={member.placement.workspace ?? ""}
                  onChange={(event) =>
                    updateMember({
                      placement: {
                        ...member.placement,
                        workspace: event.target.value || undefined,
                      },
                    })
                  }
                />
              </Field>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                disabled={member.id === execution.lead}
                onClick={() =>
                  onChange({
                    ...execution,
                    members: execution.members.filter((item) => item.id !== member.id),
                  })
                }
              >
                Remove {member.id}
              </Button>
            </fieldset>
          );
        })}
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={execution.members.length >= 16}
          onClick={() => {
            const member = newMember(
              nextIdentifier(
                "specialist",
                execution.members.map((item) => item.id),
              ),
            );
            onChange({
              ...execution,
              members: [
                ...execution.members,
                {
                  ...member,
                  role: "Provide specialist evidence when asked by the owner.",
                },
              ],
            });
          }}
        >
          <PlusIcon className="size-3" />
          Add agent
        </Button>
      </div>
    </details>
  );
}
