/**
 * Coerce raw policy factory-parameter form values into the typed shape the
 * server expects.
 *
 * The add-policy dialogs in ``PoliciesPage`` (global defaults) and
 * ``AgentInfo`` (per-session) both capture every field as a string and must
 * turn ``"20"`` into ``20``, ``"a, b"`` into ``["a", "b"]``, and a typed-in
 * JSON object literal into a real object before sending ``factory_params``.
 *
 * ``object``-typed fields (e.g. the risk-score policy's ``tool_points`` /
 * ``sensitive_labels``) were previously passed through as raw strings because
 * the coercion had no ``object`` branch — the server then rejected the
 * non-dict value and the create silently failed. This helper is the single
 * source of truth for that conversion so the two dialogs can't drift.
 */

export interface PolicyParamProp {
  type?: string;
}

export type PolicyParamsResult =
  | { ok: true; params: Record<string, unknown> }
  | { ok: false; error: string };

/**
 * Convert string-valued form inputs to typed factory params, driven by each
 * parameter's JSON-schema ``type``. Empty / unset fields are omitted. Returns
 * a discriminated result so callers can surface a precise error (e.g. invalid
 * JSON in an ``object`` field) instead of submitting a value the server will
 * reject.
 */
export function coercePolicyParams(
  paramKeys: string[],
  properties: Record<string, PolicyParamProp | undefined>,
  rawValues: Record<string, string>,
): PolicyParamsResult {
  const params: Record<string, unknown> = {};
  for (const key of paramKeys) {
    const raw = rawValues[key];
    if (raw === undefined || raw === "") continue;
    const type = properties[key]?.type;
    if (type === "integer") {
      params[key] = parseInt(raw, 10);
    } else if (type === "number") {
      params[key] = parseFloat(raw);
    } else if (type === "boolean") {
      params[key] = raw === "true";
    } else if (type === "array") {
      params[key] = raw
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
    } else if (type === "object") {
      let parsed: unknown;
      try {
        parsed = JSON.parse(raw);
      } catch {
        return {
          ok: false,
          error: `${key} must be valid JSON, e.g. {"web_search": 10}`,
        };
      }
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        return {
          ok: false,
          error: `${key} must be a JSON object, e.g. {"web_search": 10}`,
        };
      }
      params[key] = parsed;
    } else {
      // "string" and any unmapped schema type pass through verbatim — the
      // server validates the concrete value.
      params[key] = raw;
    }
  }
  return { ok: true, params };
}
