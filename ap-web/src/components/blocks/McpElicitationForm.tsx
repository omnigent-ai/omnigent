// Generic JSON-Schema form for MCP-server elicitations surfaced in
// claude-native (the `Elicitation` hook → `/hooks/elicitation` →
// `response.elicitation_request` with `policy_name ===
// "claude_native_mcp_elicitation"`).
//
// MCP elicitation schemas are a restricted subset of JSON Schema: a
// flat object whose properties are primitives (string / number /
// integer / boolean) or a string/number enum — no nested objects or
// arrays of objects. This renders one input per property:
//
//   - string            → text input
//   - number / integer  → number input
//   - boolean           → switch
//   - enum              → select
//
// Submit gathers the typed values into a flat `{[prop]: value}` map
// (MCP's `ElicitResult.content` shape) with correct value types, gated
// on every `required` field being filled and valid. Decline and Cancel
// map to the MCP `decline` / `cancel` actions respectively.
//
// `schemaIsRenderable` lets the caller (ApprovalCard) fall back to the
// binary approve/reject card when a schema contains a property type
// this form can't render — guaranteeing the prompt is always answerable.

import { CheckIcon, XIcon } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";

type FieldType = "string" | "number" | "integer" | "boolean" | "enum";

interface FieldSpec {
  key: string;
  title: string;
  description: string | null;
  type: FieldType;
  required: boolean;
  /** Stringified enum options for the select (enum fields only). */
  enumOptions: string[];
  /** Original enum values, so the submitted content keeps its type. */
  enumRaw: (string | number)[];
  /** JSON-Schema default, or undefined. */
  default: unknown;
}

/**
 * Classify a JSON-Schema property into a renderable field type, or
 * ``null`` when it is a shape this form can't render (nested object,
 * array, untyped, etc.).
 */
function fieldType(prop: Record<string, unknown>): FieldType | null {
  if (Array.isArray(prop.enum)) return "enum";
  switch (prop.type) {
    case "string":
      return "string";
    case "number":
      return "number";
    case "integer":
      return "integer";
    case "boolean":
      return "boolean";
    default:
      return null;
  }
}

/**
 * Parse a ``requestedSchema`` into an ordered list of field specs, or
 * ``null`` when the schema is empty or contains any unrenderable
 * property — the signal for ApprovalCard to degrade to a binary card.
 */
function parseFields(schema: Record<string, unknown>): FieldSpec[] | null {
  const properties = schema.properties;
  if (!properties || typeof properties !== "object" || Array.isArray(properties)) return null;
  const props = properties as Record<string, unknown>;
  const keys = Object.keys(props);
  if (keys.length === 0) return null;
  const required = Array.isArray(schema.required)
    ? (schema.required.filter((r): r is string => typeof r === "string") as string[])
    : [];
  const fields: FieldSpec[] = [];
  for (const key of keys) {
    const prop = props[key];
    if (!prop || typeof prop !== "object" || Array.isArray(prop)) return null;
    const p = prop as Record<string, unknown>;
    const type = fieldType(p);
    if (type === null) return null;
    let enumRaw: (string | number)[] = [];
    if (type === "enum") {
      enumRaw = (p.enum as unknown[]).filter(
        (v): v is string | number => typeof v === "string" || typeof v === "number",
      );
      // An enum with no usable (string/number) options can't render.
      if (enumRaw.length === 0) return null;
    }
    fields.push({
      key,
      title: typeof p.title === "string" && p.title ? p.title : key,
      description: typeof p.description === "string" && p.description ? p.description : null,
      type,
      required: required.includes(key),
      enumOptions: enumRaw.map(String),
      enumRaw,
      default: p.default,
    });
  }
  return fields;
}

/**
 * Whether ApprovalCard should render the generic form for this schema.
 * False for empty schemas or any unrenderable property type → caller
 * falls back to the binary approve/reject card.
 */
export function schemaIsRenderable(schema: Record<string, unknown>): boolean {
  return parseFields(schema) !== null;
}

/** Form value state: booleans store a bool, everything else a string. */
type FormValues = Record<string, string | boolean>;

function initialValues(fields: FieldSpec[]): FormValues {
  const init: FormValues = {};
  for (const f of fields) {
    if (f.type === "boolean") {
      init[f.key] = f.default === true;
    } else if (f.default !== undefined && f.default !== null) {
      init[f.key] = String(f.default);
    } else {
      init[f.key] = "";
    }
  }
  return init;
}

/**
 * Validate one field's current value. Required fields must be filled
 * and (for numbers) numeric; an optional field is valid empty but, when
 * filled, must still parse.
 */
function fieldValid(f: FieldSpec, v: string | boolean): boolean {
  if (f.type === "boolean") return true;
  const s = typeof v === "string" ? v.trim() : "";
  if (s === "") return !f.required;
  if (f.type === "number" || f.type === "integer") {
    const n = Number(s);
    if (!Number.isFinite(n)) return false;
    if (f.type === "integer" && !Number.isInteger(n)) return false;
  }
  if (f.type === "enum") return f.enumOptions.includes(s);
  return true;
}

interface McpElicitationFormProps {
  requestedSchema: Record<string, unknown>;
  onSubmit: (content: Record<string, unknown>) => void;
  onDecline: () => void;
  onCancel: () => void;
}

export function McpElicitationForm({
  requestedSchema,
  onSubmit,
  onDecline,
  onCancel,
}: McpElicitationFormProps) {
  // ApprovalCard only mounts this when ``schemaIsRenderable`` is true,
  // so ``parseFields`` is non-null here; guard defensively regardless.
  const fields = parseFields(requestedSchema);
  const [values, setValues] = useState<FormValues>(() => initialValues(fields ?? []));
  if (fields === null) return null;

  const allValid = fields.every((f) => fieldValid(f, values[f.key]));

  const handleSubmit = () => {
    const content: Record<string, unknown> = {};
    for (const f of fields) {
      const v = values[f.key];
      if (f.type === "boolean") {
        content[f.key] = Boolean(v);
        continue;
      }
      const s = typeof v === "string" ? v.trim() : "";
      if (s === "") continue; // omit empty optional fields
      if (f.type === "number" || f.type === "integer") {
        content[f.key] = Number(s);
      } else if (f.type === "enum") {
        const raw = f.enumRaw.find((e) => String(e) === s);
        content[f.key] = raw !== undefined ? raw : s;
      } else {
        content[f.key] = s;
      }
    }
    onSubmit(content);
  };

  return (
    <div className="flex flex-col gap-3 text-foreground" data-testid="mcp-elicitation-form">
      {fields.map((f) => {
        const inputId = `mcp-elicit-${f.key}`;
        const v = values[f.key];
        return (
          <div key={f.key} className="flex flex-col gap-1">
            <label htmlFor={inputId} className="flex items-center gap-1 text-sm font-medium">
              {f.title}
              {f.required && <span className="text-destructive">*</span>}
            </label>
            {f.description && (
              <span className="text-muted-foreground text-xs">{f.description}</span>
            )}
            {f.type === "boolean" ? (
              <Switch
                id={inputId}
                checked={Boolean(v)}
                onCheckedChange={(checked) =>
                  setValues((prev) => ({ ...prev, [f.key]: checked }))
                }
                data-testid={`mcp-elicit-field-${f.key}`}
              />
            ) : f.type === "enum" ? (
              <Select
                value={typeof v === "string" ? v : ""}
                onValueChange={(val) => setValues((prev) => ({ ...prev, [f.key]: val }))}
              >
                <SelectTrigger id={inputId} data-testid={`mcp-elicit-field-${f.key}`}>
                  <SelectValue placeholder="Select…" />
                </SelectTrigger>
                <SelectContent>
                  {f.enumOptions.map((opt) => (
                    <SelectItem key={opt} value={opt}>
                      {opt}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : (
              <Input
                id={inputId}
                type={f.type === "string" ? "text" : "number"}
                value={typeof v === "string" ? v : String(v ?? "")}
                onChange={(e) => setValues((prev) => ({ ...prev, [f.key]: e.target.value }))}
                data-testid={`mcp-elicit-field-${f.key}`}
              />
            )}
          </div>
        );
      })}
      <div className="flex flex-wrap items-center gap-2 pt-1">
        <Button
          size="sm"
          onClick={handleSubmit}
          disabled={!allValid}
          data-testid="mcp-elicitation-submit"
        >
          <CheckIcon className="mr-1 size-3.5" />
          Submit
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={onDecline}
          data-testid="mcp-elicitation-decline"
        >
          <XIcon className="mr-1 size-3.5" />
          Decline
        </Button>
        <Button
          size="sm"
          variant="ghost"
          onClick={onCancel}
          className="ml-auto"
          data-testid="mcp-elicitation-cancel"
        >
          Cancel
        </Button>
      </div>
    </div>
  );
}
