// Trigger-template fields shown in place of the scheduled-task prompt.

import { useState } from "react";
import { PlusIcon, TriangleAlertIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { Label } from "@/components/scheduled/Label";
import {
  MAX_INSTRUCTIONS_CHARS,
  MAX_SESSIONS_PER_RUN,
  OMNIGENT_ATTRIBUTION,
  stripScopePrefix,
  TRIGGER_TEMPLATES,
  triggerConditionFields,
  triggerEvent,
  triggerOperatorLabel,
  triggerScope,
  triggerServiceName,
  VALUELESS_OPERATORS,
  withTriggerEvent,
  type TriggerCondition,
  type TriggerFields,
  type TriggerOperator,
  type TriggerSource,
} from "@/components/scheduled/triggerTemplates";
import { cn } from "@/lib/utils";

/** Tinted, compact styling shared by the builder's selects, inputs and fixed words. */
const CHIP = "h-7 rounded-md border-0 bg-primary/10 px-2 text-sm text-primary shadow-none";

export function TriggerTemplateFields({
  source,
  fields,
  onChange,
  renderedPrompt,
  onEditAsPrompt,
  onSelectOpenChange,
  toolsProvided = false,
}: {
  source: TriggerSource;
  fields: TriggerFields;
  onChange: (next: TriggerFields) => void;
  /** The prompt the task will save; "" until the required fields are filled. */
  renderedPrompt: string;
  /** Leave the template and edit the rendered prompt as a plain prompt. */
  onEditAsPrompt: () => void;
  /** Forwarded to each Select so the dialog's dismiss guard knows one is open. */
  onSelectOpenChange: (open: boolean) => void;
  /** Where the task runs already gives the agent the service's tools, so skip that warning. */
  toolsProvided?: boolean;
}) {
  const [warningsOpen, setWarningsOpen] = useState(false);
  const template = TRIGGER_TEMPLATES[source];
  const Icon = template.icon;
  const { authorAction } = triggerEvent(source, fields.event);
  const scope = triggerScope(source, fields.event);
  const conditionFields = triggerConditionFields(source, fields.event);
  const instructionsTooLong = fields.instructions.trim().length > MAX_INSTRUCTIONS_CHARS;

  function setCondition(index: number, next: TriggerCondition) {
    onChange({
      ...fields,
      conditions: fields.conditions.map((c, i) => (i === index ? next : c)),
    });
  }

  function addCondition() {
    const field = conditionFields[0];
    onChange({
      ...fields,
      conditions: [
        ...fields.conditions,
        { field: field.id, operator: field.operators[0], value: "" },
      ],
    });
  }

  return (
    <div className="flex flex-col gap-4" data-testid="task-trigger-fields">
      <div className="flex flex-col gap-1.5">
        <Label>Trigger</Label>
        <p className="text-sm text-muted-foreground">
          Runs your automation on all new {template.itemPlural} that match these conditions. Older{" "}
          {template.itemPlural} are skipped, even if they are edited to match later.
        </p>
        <div
          className="flex flex-col gap-2 rounded-lg border border-border p-3"
          data-testid="task-trigger-builder"
        >
          <div className="flex flex-wrap items-center gap-1.5 text-sm">
            <Icon className={cn("size-4 shrink-0", template.iconClassName)} />
            {template.events.length > 1 ? (
              <Select
                value={fields.event}
                onValueChange={(event) => onChange(withTriggerEvent(source, fields, event))}
                onOpenChange={onSelectOpenChange}
              >
                <SelectTrigger
                  size="sm"
                  className={CHIP}
                  aria-label="Event"
                  data-testid="task-trigger-event"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent position="popper" align="start">
                  {template.events.map((e) => (
                    <SelectItem key={e.id} value={e.id} data-testid={`task-trigger-event-${e.id}`}>
                      {e.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : (
              <span className={cn(CHIP, "flex items-center")} data-testid="task-trigger-event">
                {template.events[0].label}
              </span>
            )}
            <span className="text-muted-foreground">where {scope.label}</span>
            {!scope.isGroup && (
              <Select
                value={fields.scopeOperator ?? "in"}
                onValueChange={(operator) =>
                  onChange({ ...fields, scopeOperator: operator as "is" | "in" })
                }
                onOpenChange={onSelectOpenChange}
              >
                <SelectTrigger
                  size="sm"
                  className={CHIP}
                  aria-label="Scope operator"
                  data-testid="task-trigger-scope-operator"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent position="popper" align="start">
                  <SelectItem value="is">equals</SelectItem>
                  <SelectItem value="in">in</SelectItem>
                </SelectContent>
              </Select>
            )}
            {/* The prefix wraps with its input, so it never ends a line on its own. */}
            <span className="flex min-w-36 flex-1 items-center gap-1">
              {scope.prefix !== "" && (
                <span
                  className="text-sm font-medium text-primary"
                  aria-hidden="true"
                  data-testid="task-trigger-scope-prefix"
                >
                  {scope.prefix}
                </span>
              )}
              <Input
                value={fields.scope}
                placeholder={
                  fields.scopeOperator === "is" && !scope.isGroup
                    ? scope.placeholder.split(",")[0]
                    : scope.placeholder
                }
                aria-label={scope.label}
                data-testid="task-trigger-scope"
                className={cn(CHIP, "min-w-0 flex-1 placeholder:text-primary/50")}
                onChange={(e) =>
                  onChange({ ...fields, scope: stripScopePrefix(e.target.value, scope.prefix) })
                }
              />
            </span>
          </div>

          {scope.isGroup && fields.scope.trim() !== "" && !fields.scope.includes(",") && (
            <p className="pl-6 text-sm text-muted-foreground" data-testid="task-trigger-group-hint">
              List at least two people. For one person, choose Direct message received.
            </p>
          )}

          {!scope.isGroup && fields.scopeOperator === "is" && fields.scope.includes(",") && (
            <p className="pl-6 text-sm text-destructive" role="alert">
              Equals matches one value. Choose in to match a comma-separated list.
            </p>
          )}

          {fields.conditions.map((condition, index) => {
            const field =
              conditionFields.find((f) => f.id === condition.field) ?? conditionFields[0];
            return (
              <div
                // Rows have no identity beyond their position.
                // eslint-disable-next-line react/no-array-index-key
                key={index}
                className="flex flex-wrap items-center gap-1.5 pl-6 text-sm"
                data-testid={`task-trigger-condition-${index}`}
              >
                <span className="text-muted-foreground">and</span>
                <Select
                  value={field.id}
                  onValueChange={(id) => {
                    const next = conditionFields.find((f) => f.id === id) ?? field;
                    setCondition(index, { field: next.id, operator: next.operators[0], value: "" });
                  }}
                  onOpenChange={onSelectOpenChange}
                >
                  <SelectTrigger
                    size="sm"
                    className={CHIP}
                    aria-label="Field"
                    data-testid={`task-trigger-condition-${index}-field`}
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent position="popper" align="start">
                    {conditionFields.map((f) => (
                      <SelectItem
                        key={f.id}
                        value={f.id}
                        data-testid={`task-trigger-condition-${index}-field-${f.id}`}
                      >
                        {f.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                {field.operators.length > 1 ? (
                  <Select
                    value={condition.operator}
                    onValueChange={(operator) =>
                      setCondition(index, { ...condition, operator: operator as TriggerOperator })
                    }
                    onOpenChange={onSelectOpenChange}
                  >
                    <SelectTrigger
                      size="sm"
                      className={CHIP}
                      aria-label="Operator"
                      data-testid={`task-trigger-condition-${index}-operator`}
                    >
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent position="popper" align="start">
                      {field.operators.map((op) => (
                        <SelectItem
                          key={op}
                          value={op}
                          data-testid={`task-trigger-condition-${index}-operator-${op}`}
                        >
                          {triggerOperatorLabel(field, op)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                ) : (
                  <span className={cn(CHIP, "flex items-center")}>
                    {triggerOperatorLabel(field, field.operators[0])}
                  </span>
                )}
                {VALUELESS_OPERATORS.has(condition.operator) ? (
                  <span className="flex-1" />
                ) : (
                  <Input
                    value={condition.value}
                    placeholder={field.placeholder}
                    aria-label={`${field.label} value`}
                    data-testid={`task-trigger-condition-${index}-value`}
                    className={cn(CHIP, "min-w-24 flex-1 placeholder:text-primary/50")}
                    onChange={(e) => setCondition(index, { ...condition, value: e.target.value })}
                  />
                )}
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  aria-label="Remove condition"
                  data-testid={`task-trigger-condition-${index}-remove`}
                  componentId="tasks.scheduled.trigger_remove_condition"
                  onClick={() =>
                    onChange({
                      ...fields,
                      conditions: fields.conditions.filter((_, i) => i !== index),
                    })
                  }
                >
                  <XIcon className="size-3.5" />
                </Button>
              </div>
            );
          })}

          <div className="pl-6">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-7 px-2 text-muted-foreground"
              data-testid="task-trigger-add-condition"
              componentId="tasks.scheduled.trigger_add_condition"
              onClick={addCondition}
            >
              <PlusIcon className="size-3.5" />
              Add condition
            </Button>
          </div>
        </div>
        {fields.conditions.some(
          (c) => !VALUELESS_OPERATORS.has(c.operator) && c.value.trim() === "",
        ) && (
          <p className="text-sm text-muted-foreground" data-testid="task-trigger-empty-condition">
            Fill in or remove each condition.
          </p>
        )}
        <div className="mt-1 flex items-center gap-2">
          <Checkbox
            id="task-trigger-link-back"
            checked={fields.linkBack}
            onCheckedChange={(checked) => onChange({ ...fields, linkBack: checked === true })}
            data-testid="task-trigger-link-back"
            componentId="tasks.scheduled.trigger_link_back"
          />
          <label htmlFor="task-trigger-link-back" className="cursor-pointer text-sm">
            {template.linkBackLabel}
          </label>
        </div>
      </div>

      <div className="flex flex-col gap-1.5">
        <div className="flex items-center gap-1.5">
          <Label htmlFor="task-trigger-instructions">Instructions for each session</Label>
          {!toolsProvided && (
            <TooltipProvider>
              <Tooltip open={warningsOpen} onOpenChange={setWarningsOpen}>
                <TooltipTrigger asChild>
                  <button
                    type="button"
                    aria-label="Session instructions warnings"
                    className="inline-flex size-4 shrink-0 cursor-pointer items-center justify-center rounded-sm text-warning focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    onClick={(event) => {
                      event.preventDefault();
                      setWarningsOpen(true);
                    }}
                  >
                    <TriangleAlertIcon className="size-3.5" aria-hidden="true" />
                  </button>
                </TooltipTrigger>
                <TooltipContent side="top" className="block max-w-xs space-y-2 text-warning">
                  <p data-testid="task-trigger-tools-note">
                    This only works if the agent has {template.toolsExample} where this task runs.
                    Without one, each run stops and says so in its reply.
                  </p>
                </TooltipContent>
              </Tooltip>
            </TooltipProvider>
          )}
        </div>
        <Textarea
          id="task-trigger-instructions"
          value={fields.instructions}
          rows={3}
          placeholder={template.instructionsPlaceholder}
          data-testid="task-trigger-instructions"
          componentId="tasks.scheduled.trigger_instructions"
          className="resize-none text-ui"
          onChange={(e) => onChange({ ...fields, instructions: e.target.value })}
        />
        {instructionsTooLong && (
          <p className="text-sm text-destructive" role="alert" data-testid="task-trigger-too-long">
            Keep instructions to {MAX_INSTRUCTIONS_CHARS.toLocaleString("en-US")} characters or
            fewer so each session&apos;s first message has room for the item.
          </p>
        )}
        <p className="text-sm text-muted-foreground" data-testid="task-trigger-attribution-note">
          Up to {MAX_SESSIONS_PER_RUN} matches per run; remaining matches wait for the next run ·
          Posts include &quot;
          {OMNIGENT_ATTRIBUTION}&quot;.
        </p>
      </div>

      <p
        className="flex items-start gap-1.5 text-sm text-warning"
        data-testid="task-trigger-untrusted-note"
      >
        <TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
        <span>
          Anyone who can {authorAction} may direct the agent using your tools and permissions. Only
          use trusted sources.
          {fields.linkBack && ` Links are posted with your ${triggerServiceName(source)} account.`}
        </span>
      </p>

      {renderedPrompt !== "" && (
        <details className="group" data-testid="task-trigger-preview">
          <summary className="cursor-pointer text-sm text-muted-foreground">Preview prompt</summary>
          <pre className="mt-2 max-h-56 overflow-y-auto rounded-md border border-border bg-muted/40 p-2 font-sans text-sm whitespace-pre-wrap">
            {renderedPrompt}
          </pre>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="mt-2"
            onClick={onEditAsPrompt}
            data-testid="task-trigger-edit-as-prompt"
            componentId="tasks.scheduled.trigger_edit_as_prompt"
          >
            Edit as prompt
          </Button>
        </details>
      )}
    </div>
  );
}
