// Form for an MCP elicitation's ``requestedSchema``.
//
// ``elicitation/create`` is how an MCP server asks for something it cannot
// decide alone, and the shape it wants back is declared in the request:
// a flat object of primitive properties, per MCP's elicitation spec. The
// card rendered a bare Approve / Reject for all of them, so a server asking
// "which branch?" or "name the release" got a yes with no fields — the
// answer it declared it needed was unanswerable.
//
// Each property becomes one control, chosen by what the schema says:
//   - ``enum``            → a select, with the offered values
//   - ``type: boolean``   → a checkbox
//   - ``type: number`` /
//     ``integer``         → a number input
//   - anything else       → a text input
//
// ``default`` prefills. ``title`` labels the field and ``description``
// explains it, both per the spec's presentation hints, falling back to the
// property name. Submit is gated on every ``required`` property having a
// value, so a server that declared a field cannot be sent an accept without
// it.

import { CheckIcon, XIcon } from "lucide-react";
import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/** One value MCP allows in an ``ElicitResult.content`` map. */
export type ElicitValue = string | number | boolean;

/** The answers gathered from the form, keyed by property name. */
export type ElicitationAnswers = Record<string, ElicitValue>;

/** Form state, where a field may not have been answered yet. */
export type ElicitationDraft = Record<string, ElicitValue | undefined>;

/** A single property of a ``requestedSchema``, as far as this form reads it. */
interface SchemaProperty {
  type?: string;
  enum?: unknown[];
  enumNames?: unknown[];
  default?: unknown;
  title?: string;
  description?: string;
}

/** A property paired with the name it is keyed under. */
export interface SchemaField {
  name: string;
  prop: SchemaProperty;
  required: boolean;
}

/**
 * Read a ``requestedSchema`` into an ordered field list.
 *
 * Returns an empty array for a schema with no properties at all — a bare
 * consent prompt, where Approve / Reject is the right control. Property order
 * follows the schema's own key order, which is the order the server wrote
 * them in.
 *
 * A lone ``answer`` enum *does* come back as one field. Excluding it is the
 * caller's job: the card checks the option-button branch first, and the
 * approve hotkey wants that shape counted so it stops firing a bare accept at
 * a question that needs a choice.
 */
export function schemaFields(schema: Record<string, unknown> | null | undefined): SchemaField[] {
  // The schema comes from an outside server and is optional on the wire, so
  // an absent one is ordinary rather than exceptional.
  const properties = schema?.properties;
  if (!properties || typeof properties !== "object") return [];
  const entries = Object.entries(properties as Record<string, unknown>);
  if (entries.length === 0) return [];
  const requiredRaw = schema.required;
  const required = new Set(
    Array.isArray(requiredRaw) ? requiredRaw.filter((r): r is string => typeof r === "string") : [],
  );
  return entries
    .filter(([, prop]) => prop !== null && typeof prop === "object")
    .map(([name, prop]) => ({
      name,
      prop: prop as SchemaProperty,
      required: required.has(name),
    }));
}

/**
 * The value a field starts at: its ``default``, or unset.
 *
 * A boolean with no default starts ``undefined`` rather than ``false``. They
 * are not the same answer — ``false`` tells the server to turn something off,
 * and an untouched checkbox has told it nothing.
 */
function initialValue(field: SchemaField): ElicitValue | undefined {
  const { prop } = field;
  if (prop.default !== undefined && isElicitValue(prop.default)) return prop.default;
  if (prop.type === "boolean") return undefined;
  return "";
}

/**
 * A schema-supplied string, or ``""`` when the server sent something else.
 *
 * ``title`` and ``description`` are typed as strings but arrive from an
 * outside server; handing React a non-string here crashes the whole card.
 */
function asText(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** True when a value is one of the primitives MCP allows in ``content``. */
function isElicitValue(value: unknown): value is ElicitValue {
  return typeof value === "string" || typeof value === "number" || typeof value === "boolean";
}

/** The enum members of a property, as strings, or an empty array. */
function enumValues(prop: SchemaProperty): string[] {
  if (!Array.isArray(prop.enum)) return [];
  return prop.enum
    .filter((v): v is string => typeof v === "string" || typeof v === "number")
    .map(String);
}

/**
 * The label to show for an enum member.
 *
 * The spec lets a server send ``enumNames`` alongside ``enum`` — display text
 * for values that are ids. Showing the id when a name was supplied puts the
 * server's internal spelling in front of a person.
 */
export function enumLabel(prop: SchemaProperty, index: number, value: string): string {
  const names = prop.enumNames;
  if (!Array.isArray(names)) return value;
  const name = names[index];
  return typeof name === "string" && name.length > 0 ? name : value;
}

/**
 * Whether every required field has an answer.
 *
 * A boolean is always answered — false is a real answer to "should I force
 * push?" — so only text-shaped fields can be blank.
 */
export function isComplete(fields: SchemaField[], answers: ElicitationDraft): boolean {
  return fields.every((field) => {
    const value = answers[field.name];
    if (typeof value === "boolean") return true;
    const blank = value === undefined || String(value).trim().length === 0;
    // An optional field may be left alone, but not filled in wrongly: dropping
    // what someone typed while reporting success is the worse of the two.
    if (blank) return !field.required;
    return numberOrNull(field, value) !== null;
  });
}

/**
 * A numeric field's value as a finite number, or ``null`` when it is not one.
 *
 * ``Number("1e999")`` is ``Infinity``, which ``JSON.stringify`` writes as
 * ``null`` — the server would receive a null where it declared a number. A
 * non-integer in an ``integer`` field is type-invalid in the same way, and an
 * accept carrying either is worse than a decline: the server has already
 * committed to the accept path by the time it hits the error.
 */
function numberOrNull(field: SchemaField, value: ElicitValue): number | null {
  const { type } = field.prop;
  if (type !== "number" && type !== "integer") return 0;
  const parsed = Number(String(value));
  if (!Number.isFinite(parsed)) return null;
  if (type === "integer" && !Number.isInteger(parsed)) return null;
  return parsed;
}

/**
 * Drop blank optional fields and coerce numbers before submitting.
 *
 * An untouched optional text field would otherwise send ``""``, which reads
 * to the server as "answered, with nothing" rather than "not answered".
 */
export function toContent(fields: SchemaField[], answers: ElicitationDraft): ElicitationAnswers {
  const content: ElicitationAnswers = {};
  for (const field of fields) {
    const value = answers[field.name];
    if (value === undefined) continue;
    if (typeof value === "boolean") {
      content[field.name] = value;
      continue;
    }
    const text = String(value);
    if (text.trim().length === 0) continue;
    const numeric = field.prop.type === "number" || field.prop.type === "integer";
    if (numeric) {
      const parsed = numberOrNull(field, value);
      // Unparseable in a numeric field: omit rather than send the server a
      // value its own schema rejects.
      if (parsed !== null) content[field.name] = parsed;
      continue;
    }
    content[field.name] = text;
  }
  return content;
}

interface ElicitationSchemaFormProps {
  fields: SchemaField[];
  onSubmit: (content: ElicitationAnswers) => void;
  onReject: () => void;
}

/**
 * Render one control per schema property and gather the answers.
 *
 * :param fields: Parsed properties, from :func:`schemaFields`.
 * :param onSubmit: Receives the ``ElicitResult.content`` map.
 * :param onReject: Called when the person declines instead.
 */
export function ElicitationSchemaForm({ fields, onSubmit, onReject }: ElicitationSchemaFormProps) {
  const [answers, setAnswers] = useState<ElicitationDraft>(() =>
    Object.fromEntries(fields.map((field) => [field.name, initialValue(field)])),
  );
  const set = (name: string, value: ElicitValue) =>
    setAnswers((prev) => ({ ...prev, [name]: value }));
  // Two pending cards can both ask for "branch"; a bare field name would give
  // them the same DOM id and point every label at the first card's input.
  const scope = useId();
  const complete = isComplete(fields, answers);

  return (
    <div className="flex flex-col gap-3" data-testid="elicitation-schema-form">
      {fields.map((field) => {
        const { name, prop, required } = field;
        const label = asText(prop.title) || name;
        const description = asText(prop.description);
        const options = enumValues(prop);
        const value = answers[name];
        const controlId = `${scope}-${name}`;
        const describedBy = asText(prop.description) ? `${controlId}-description` : undefined;
        return (
          <div key={name} className="flex flex-col gap-1" data-testid={`elicit-field-${name}`}>
            <label htmlFor={controlId} className="text-ui text-foreground">
              {label}
              {required && <span className="ml-1 text-muted-foreground">*</span>}
            </label>
            {description && (
              <span id={describedBy} className="text-sm text-muted-foreground">
                {description}
              </span>
            )}
            {options.length > 0 ? (
              <select
                id={controlId}
                className="rounded border bg-background px-2 py-1 text-ui text-foreground"
                required={required}
                aria-required={required}
                aria-describedby={describedBy}
                value={String(value ?? "")}
                onChange={(e) => set(name, e.target.value)}
              >
                <option value="">Choose…</option>
                {options.map((option, index) => (
                  <option key={option} value={option}>
                    {enumLabel(prop, index, option)}
                  </option>
                ))}
              </select>
            ) : prop.type === "boolean" ? (
              <input
                id={controlId}
                type="checkbox"
                className="size-4 self-start"
                aria-required={required}
                aria-describedby={describedBy}
                checked={value === true}
                onChange={(e) => set(name, e.target.checked)}
              />
            ) : (
              <Input
                id={controlId}
                type={prop.type === "number" || prop.type === "integer" ? "number" : "text"}
                required={required}
                aria-required={required}
                aria-describedby={describedBy}
                value={String(value ?? "")}
                onChange={(e) => set(name, e.target.value)}
              />
            )}
          </div>
        );
      })}
      <div className="flex flex-wrap gap-2 pt-1">
        <Button
          size="sm"
          disabled={!complete}
          data-testid="elicitation-schema-submit"
          onClick={() => onSubmit(toContent(fields, answers))}
        >
          <CheckIcon className="mr-1 size-3.5" />
          Submit
        </Button>
        <Button size="sm" variant="outline" onClick={onReject}>
          <XIcon className="mr-1 size-3.5" />
          Reject
        </Button>
      </div>
    </div>
  );
}
