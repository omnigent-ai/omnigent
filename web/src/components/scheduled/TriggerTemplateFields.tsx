// Trigger-template fields shown in place of the scheduled-task prompt.

import { useState } from "react";
import { InfoIcon, PlusIcon, TriangleAlertIcon, XIcon } from "lucide-react";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import {
  MAX_INSTRUCTIONS_CHARS,
  MAX_SESSIONS_PER_RUN,
  OMNIGENT_ATTRIBUTION,
  singleValueScope,
  stripScopePrefix,
  TRIGGER_SCOPE_OPERATOR_LABELS,
  TRIGGER_TEMPLATES,
  triggerConditionFields,
  triggerConditionFilledIn,
  triggerEvent,
  triggerEventDescription,
  triggerOperatorLabel,
  triggerScope,
  triggerScopeEntries,
  triggerScopeOperators,
  triggerTargetValid,
  triggerServiceName,
  VALUELESS_OPERATORS,
  withTriggerEvent,
  type TriggerCondition,
  type TriggerFields,
  type TriggerOperator,
  type TriggerScopeOperator,
  type TriggerSource,
  type TriggerTaskSessionMode,
} from "@/components/scheduled/triggerTemplates";
import { cn } from "@/lib/utils";

/** Tinted, compact styling shared by the builder's selects, inputs and fixed words. */
const CHIP = "h-7 rounded-md border-0 bg-primary/10 px-2 text-ui text-primary shadow-none";

function WarningTag({
  label,
  message,
  testId,
  warning = false,
}: {
  label: string;
  message: string;
  testId: string;
  warning?: boolean;
}) {
  return (
    <TooltipProvider delayDuration={0}>
      <Tooltip>
        <TooltipTrigger asChild>
          <Badge
            variant="outline"
            tabIndex={0}
            aria-label={`${label}: ${message}`}
            className="h-5 rounded-md border-border bg-muted px-2 text-muted-foreground"
            data-testid={testId}
          >
            {warning ? (
              <TriangleAlertIcon data-icon="inline-start" aria-hidden="true" />
            ) : (
              <InfoIcon data-icon="inline-start" aria-hidden="true" />
            )}
            {label}
          </Badge>
        </TooltipTrigger>
        <TooltipContent className="max-w-sm">{message}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export function TriggerTemplateFields({
  source,
  fields,
  onChange,
  onSelectOpenChange,
  toolsProvided = false,
  taskSessionMode = "new",
}: {
  source: TriggerSource;
  fields: TriggerFields;
  onChange: (next: TriggerFields) => void;
  /** "continue" sends every match to one task session, which these instructions then direct. */
  taskSessionMode?: TriggerTaskSessionMode;
  /** Forwarded to each Select so the dialog's dismiss guard knows one is open. */
  onSelectOpenChange: (open: boolean) => void;
  /** Where the task runs already gives the agent the service's tools, so skip that warning. */
  toolsProvided?: boolean;
}) {
  const template = TRIGGER_TEMPLATES[source];
  const Icon = template.icon;
  const { authorAction, target } = triggerEvent(source, fields.event);
  const targetInvalid = !triggerTargetValid(fields, source);
  // Reviewers added after the pull request opens never match the opened event.
  const reviewerOnOpen =
    source === "github" &&
    fields.event === "pr" &&
    fields.conditions.some((c) => c.field === "reviewer");
  const scope = triggerScope(source, fields.event);
  const conditionFields = triggerConditionFields(source, fields.event);
  const scopeEntries = triggerScopeEntries(source, fields);
  const instructionsTooLong = fields.instructions.trim().length > MAX_INSTRUCTIONS_CHARS;
  const instructionsTooLongMessage = `Keep instructions to ${MAX_INSTRUCTIONS_CHARS.toLocaleString("en-US")} characters or fewer so each ${
    taskSessionMode === "continue" ? "message to the task session" : "session's first message"
  } has room for the item.`;
  const [activeSection, setActiveSection] = useState<"trigger" | "instructions">("trigger");
  const fullTriggerDescription = triggerEventDescription(source, fields.event);
  const firstSentenceEnd = fullTriggerDescription.indexOf(". ");
  const triggerSummary =
    firstSentenceEnd === -1
      ? fullTriggerDescription
      : fullTriggerDescription.slice(0, firstSentenceEnd + 1);
  const triggerDetails =
    firstSentenceEnd === -1 ? "" : fullTriggerDescription.slice(firstSentenceEnd + 2);
  const toolsWarning = `This only works if the agent has ${template.toolsExample} where this task runs. Without one, each run stops and says so in its reply.`;
  const sourceWarning = `Anyone who can ${authorAction} may direct the agent using your tools and permissions. Only use trusted sources.${fields.linkBack ? ` Links are posted with your ${triggerServiceName(source)} account.` : ""}`;

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
    <div className="flex flex-col gap-3" data-testid="task-trigger-fields">
      <Tabs
        value={activeSection}
        onValueChange={(value) => setActiveSection(value as "trigger" | "instructions")}
        className="gap-2"
        componentId="tasks.scheduled.trigger_setup_tabs"
        data-testid="task-trigger-tabs"
      >
        <TabsList variant="pill" className="gap-1" aria-label="Trigger setup">
          <TabsTrigger value="trigger" className="h-7 flex-none px-3">
            Trigger
          </TabsTrigger>
          <TabsTrigger
            value="instructions"
            aria-label="Instructions"
            className="h-7 flex-none px-3"
          >
            Instructions
            {fields.instructions.trim() === "" && (
              <span aria-hidden="true" data-testid="task-trigger-instructions-required">
                *
              </span>
            )}
            {instructionsTooLong && (
              <TriangleAlertIcon
                aria-hidden="true"
                className="size-3.5 text-destructive"
                data-testid="task-trigger-instructions-invalid"
              />
            )}
          </TabsTrigger>
        </TabsList>
        {/* The length error lives in the hidden Instructions panel, so surface it here too. */}
        {instructionsTooLong && activeSection !== "instructions" && (
          <div
            className="flex items-center justify-between gap-3 text-sm text-destructive"
            role="alert"
            data-testid="task-trigger-instructions-too-long-notice"
          >
            <p>{instructionsTooLongMessage}</p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="shrink-0"
              onClick={() => setActiveSection("instructions")}
            >
              Edit instructions
            </Button>
          </div>
        )}
        <TabsContent value="trigger" forceMount hidden={activeSection !== "trigger"}>
          <div
            className="flex min-h-[240px] flex-col gap-2 rounded-lg border border-border p-3"
            data-testid="task-trigger-card"
          >
            <div className="mb-1 flex items-center gap-3">
              <Avatar
                className={cn("rounded-lg bg-current/10 after:hidden", template.iconClassName)}
              >
                <AvatarFallback className="rounded-lg bg-transparent text-current">
                  <Icon className="size-4" />
                </AvatarFallback>
              </Avatar>
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <div className="flex min-w-0 items-center gap-1">
                  <p
                    className="min-w-0 text-ui leading-5 text-muted-foreground"
                    data-testid="task-trigger-description"
                  >
                    {triggerSummary}
                  </p>
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <button
                          type="button"
                          aria-label="More trigger details"
                          className="inline-flex size-5 shrink-0 items-center justify-center rounded-sm text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                        >
                          <InfoIcon className="size-3.5" aria-hidden="true" />
                        </button>
                      </TooltipTrigger>
                      <TooltipContent className="block max-w-sm space-y-1">
                        {triggerDetails !== "" && <p>{triggerDetails}</p>}
                        <p data-testid="task-trigger-attribution-note">
                          Up to {MAX_SESSIONS_PER_RUN} matches per run; remaining matches wait for
                          the next run · Posts include &quot;
                          {OMNIGENT_ATTRIBUTION}&quot;.
                        </p>
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {!toolsProvided && (
                    <WarningTag
                      label="MCP required"
                      message={toolsWarning}
                      testId="task-trigger-tools-note"
                    />
                  )}
                  <WarningTag
                    label="Untrusted source"
                    message={sourceWarning}
                    testId="task-trigger-untrusted-note"
                    warning
                  />
                </div>
              </div>
            </div>
            <div
              className="flex flex-col gap-2 border-t border-border pt-3"
              data-testid="task-trigger-builder"
            >
              <div className="flex flex-wrap items-center gap-1.5 pl-11 text-ui">
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
                        <SelectItem
                          key={e.id}
                          value={e.id}
                          data-testid={`task-trigger-event-${e.id}`}
                        >
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
                {target && (
                  <>
                    <span className="text-muted-foreground">{target.word}</span>
                    <Input
                      value={fields.target ?? ""}
                      placeholder={target.placeholder}
                      aria-label={target.label}
                      data-testid="task-trigger-target"
                      className={cn(CHIP, "min-w-16 placeholder:text-primary/50")}
                      // Fit the placeholder or value so neither is cut off.
                      style={{
                        width: `calc(${Math.max((fields.target ?? "").length, target.placeholder.length) + 2}ch + 1rem)`,
                      }}
                      onChange={(e) => onChange({ ...fields, target: e.target.value })}
                    />
                  </>
                )}
                <span className="text-muted-foreground">where {scope.label}</span>
                {!scope.isGroup && (
                  <Select
                    value={fields.scopeOperator ?? "in"}
                    onValueChange={(operator) =>
                      onChange({ ...fields, scopeOperator: operator as TriggerScopeOperator })
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
                      {triggerScopeOperators(source).map((operator) => (
                        <SelectItem key={operator} value={operator}>
                          {TRIGGER_SCOPE_OPERATOR_LABELS[operator]}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
                {/* The prefix wraps with its input, so it never ends a line on its own. */}
                <span className="flex min-w-36 flex-1 items-center gap-1">
                  {scope.prefix !== "" && (
                    <span
                      className="text-ui font-medium text-primary"
                      aria-hidden="true"
                      data-testid="task-trigger-scope-prefix"
                    >
                      {scope.prefix}
                    </span>
                  )}
                  <Input
                    value={fields.scope}
                    placeholder={
                      singleValueScope(fields) && !scope.isGroup
                        ? scope.placeholder.split(",")[0]
                        : scope.placeholder
                    }
                    aria-label={scope.label}
                    data-testid="task-trigger-scope"
                    className="h-7 min-w-0 flex-1"
                    onChange={(e) =>
                      onChange({ ...fields, scope: stripScopePrefix(e.target.value, scope.prefix) })
                    }
                  />
                </span>
                <span className="size-6 shrink-0" aria-hidden="true" />
              </div>

              {targetInvalid && target?.hint && (
                <p
                  className="pl-6 text-ui text-muted-foreground"
                  data-testid="task-trigger-target-hint"
                >
                  {target.hint}
                </p>
              )}

              {scope.isGroup && scopeEntries.length > 0 && scopeEntries.length < 2 && (
                <p
                  className="pl-6 text-ui text-muted-foreground"
                  data-testid="task-trigger-group-hint"
                >
                  List at least two people. For one person, choose Direct message received.
                </p>
              )}

              {!scope.isGroup && singleValueScope(fields) && fields.scope.includes(",") && (
                <p className="pl-6 text-ui text-destructive" role="alert">
                  {fields.scopeOperator === "contains"
                    ? "Contains matches one piece of text."
                    : "Equals matches one value."}{" "}
                  Choose in to match a comma-separated list.
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
                    className="flex flex-wrap items-center gap-1.5 text-ui"
                    data-testid={`task-trigger-condition-${index}`}
                  >
                    <span className="mr-1.5 w-8 shrink-0 text-center text-muted-foreground">
                      and
                    </span>
                    <Select
                      value={field.id}
                      onValueChange={(id) => {
                        const next = conditionFields.find((f) => f.id === id) ?? field;
                        setCondition(index, {
                          field: next.id,
                          operator: next.operators[0],
                          value: "",
                        });
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
                    {field.namePlaceholder !== undefined && (
                      <Input
                        value={condition.name ?? ""}
                        placeholder={field.namePlaceholder}
                        aria-label={`${field.label} name`}
                        data-testid={`task-trigger-condition-${index}-name`}
                        className={cn(CHIP, "w-36 placeholder:text-primary/50")}
                        onChange={(e) =>
                          setCondition(index, { ...condition, name: e.target.value })
                        }
                      />
                    )}
                    {field.operators.length > 1 ? (
                      <Select
                        value={condition.operator}
                        onValueChange={(operator) =>
                          setCondition(index, {
                            ...condition,
                            operator: operator as TriggerOperator,
                          })
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
                        className="h-7 min-w-24 flex-1"
                        onChange={(e) =>
                          setCondition(index, { ...condition, value: e.target.value })
                        }
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

              {reviewerOnOpen && (
                <p
                  className="pl-6 text-ui text-muted-foreground"
                  data-testid="task-trigger-reviewer-hint"
                >
                  Only matches reviewers requested before the next check after the pull request
                  opens. To catch later requests and re-requests, choose Review requested.
                </p>
              )}

              <div className="pl-11">
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-7 px-2 text-ui text-muted-foreground"
                  data-testid="task-trigger-add-condition"
                  componentId="tasks.scheduled.trigger_add_condition"
                  onClick={addCondition}
                >
                  <PlusIcon className="size-3.5" />
                  Add condition
                </Button>
              </div>
            </div>
            {!fields.conditions.every(triggerConditionFilledIn) && (
              <p
                className="text-ui text-muted-foreground"
                data-testid="task-trigger-empty-condition"
              >
                Fill in or remove each condition.
              </p>
            )}
            <div className="mt-auto flex flex-wrap items-center gap-2 pt-2">
              <div className="flex items-center gap-2">
                <Checkbox
                  id="task-trigger-link-back"
                  checked={fields.linkBack}
                  onCheckedChange={(checked) =>
                    onChange({ ...fields, linkBack: checked === true, linkBackTouched: true })
                  }
                  data-testid="task-trigger-link-back"
                  componentId="tasks.scheduled.trigger_link_back"
                />
                <label htmlFor="task-trigger-link-back" className="cursor-pointer text-sm">
                  {template.linkBackLabel}
                </label>
              </div>
            </div>
          </div>
        </TabsContent>

        <TabsContent
          value="instructions"
          forceMount
          hidden={activeSection !== "instructions"}
          className="flex flex-col"
        >
          <div
            className="flex min-h-[240px] flex-col gap-2"
            data-testid="task-trigger-instructions-panel"
          >
            <div className="flex flex-col gap-1.5">
              <Textarea
                id="task-trigger-instructions"
                aria-label={
                  taskSessionMode === "continue"
                    ? "Instructions for the task session"
                    : "Instructions for each session"
                }
                value={fields.instructions}
                rows={3}
                placeholder={template.instructionsPlaceholder}
                data-testid="task-trigger-instructions"
                componentId="tasks.scheduled.trigger_instructions"
                className="min-h-[240px] resize-none text-ui"
                onChange={(e) => onChange({ ...fields, instructions: e.target.value })}
              />
              {instructionsTooLong && (
                <p
                  className="text-sm text-destructive"
                  role="alert"
                  data-testid="task-trigger-too-long"
                >
                  {instructionsTooLongMessage}
                </p>
              )}
            </div>
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}
