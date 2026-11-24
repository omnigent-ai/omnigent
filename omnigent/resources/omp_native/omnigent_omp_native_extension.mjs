import installBridge from "./omnigent_pi_native_extension.cjs";

let nativeFormAdapter;
async function formDialog(html, opts, park) {
  nativeFormAdapter ??= Promise.all([
    import("@oh-my-pi/pi-tui/html-form/parse"),
    import("@oh-my-pi/pi-tui/html-form/data"),
  ]).then(([parse, data]) => createOmpFormDialog({ ...parse, ...data }));
  return (await nativeFormAdapter)(html, opts, park);
}

function formContext(form, spansText) {
  const text = [];
  for (const section of form.sections) {
    const title = spansText(section.title);
    if (title) text.push(title);
    for (const block of section.blocks) {
      if (block.kind === "text") text.push(spansText(block.spans));
      else if (block.kind === "list") text.push(...block.items.map(spansText));
      else if (block.kind === "pre") text.push(block.text);
      else if (block.kind === "table") text.push(...block.rows.map((row) => row.map(spansText).join(" | ")));
      else if (block.kind === "image" || block.kind === "svg") text.push(block.alt || "Image");
    }
  }
  return text.filter(Boolean).join("\n\n") || "OMP needs your input";
}

function formSchema(form, api, state, errors) {
  const properties = {};
  const required = [];
  const questions = api.fieldQuestions(form);
  for (const [index, field] of form.fields.entries()) {
    if (!field.name) continue;
    const key = `f${index}`;
    const title = questions.get(field) || field.name;
    const description = errors.get(field) || field.placeholder || "";
    if (field.kind === "text") {
      const numeric = field.type === "number" || field.type === "range";
      properties[key] = {
        type: numeric ? "number" : "string", title, description,
        default: Object.hasOwn(state, key) ? state[key] : api.textValue(field, field.value),
        "x-preserve-empty": true,
        "x-multiline": field.multiline,
        "x-input-type": field.type === "range" ? "number" : field.type,
        ...(field.min !== undefined ? { minimum: field.min } : {}),
        ...(field.max !== undefined ? { maximum: field.max } : {}),
        ...(field.minLength !== undefined ? { minLength: field.minLength } : {}),
        ...(field.maxLength !== undefined ? { maxLength: field.maxLength } : {}),
        ...(field.pattern ? { pattern: field.pattern } : {}),
      };
      if (field.required) required.push(key);
      continue;
    }
    const available = field.options.map((option, optionIndex) => ({ option, optionIndex }));
    if (field.multi) {
      for (const { option, optionIndex } of available) {
        const optionKey = `${key}_${optionIndex}`;
        properties[optionKey] = {
          type: "boolean", title: `${title}: ${api.spansText(option.label) || option.value}`,
          description, default: Object.hasOwn(state, optionKey) ? state[optionKey] : option.checked,
          "x-disabled": option.disabled,
        };
      }
    } else {
      const selected = available.find(({ option }) => option.checked);
      properties[key] = {
        type: "string", title, description,
        enum: available.map(({ optionIndex }) => String(optionIndex)),
        enumNames: available.map(({ option }) => api.spansText(option.label) || option.value),
        "x-disabled-enum": available.filter(({ option }) => option.disabled).map(({ optionIndex }) => String(optionIndex)),
        default: Object.hasOwn(state, key) ? state[key] : selected ? String(selected.optionIndex) : "",
      };
      if (field.required && !field.other) required.push(key);
    }
    if (field.other) {
      properties[`${key}_other`] = {
        type: "string", title: `${title}: Other`,
        description: field.multi ? "Optional additional answer" : "Optional alternative (replaces the selected option)",
        default: state[`${key}_other`] || "", "x-preserve-empty": true,
      };
    }
  }
  return { type: "object", properties, required };
}

function readFormAnswers(form, api, state) {
  const data = { ...api.defaultFormData(form) };
  const errors = new Map();
  for (const [index, field] of form.fields.entries()) {
    if (!field.name) continue;
    const key = `f${index}`;
    let value;
    if (field.kind === "text") {
      const raw = Object.hasOwn(state, key) ? state[key] : field.value;
      if (typeof raw !== "string" && typeof raw !== "number") {
        errors.set(field, "Enter text or a number");
        continue;
      }
      value = api.textValue(field, String(raw));
    } else {
      const checked = new Set();
      for (const [optionIndex, option] of field.options.entries()) {
        if (option.disabled) continue;
        if (field.multi ? state[`${key}_${optionIndex}`] === true : state[key] === String(optionIndex)) checked.add(optionIndex);
      }
      if (!field.multi && state[key] && !field.options.some((_option, optionIndex) => state[key] === String(optionIndex))) errors.set(field, "Select a supplied option");
      value = api.choiceValue(field, checked, state[`${key}_other`]);
    }
    if (value === undefined) delete data[field.name];
    else data[field.name] = value;
    const error = api.fieldError(field, value);
    if (error) errors.set(field, error);
  }
  return { data, errors };
}

/** Translate with OMP's parser and validators, never transport or render HTML. */
export function createOmpFormDialog(api) {
  return async (html, opts = {}, park) => {
    if (opts.signal?.aborted) return undefined;
    const form = api.parseHtmlForm(html);
    let data = api.defaultFormData(form);
    let errors = new Map();
    let state = {};
    const duration = Number.isFinite(opts.timeout) && opts.timeout > 0 ? opts.timeout : undefined;
    const deadline = duration === undefined ? undefined : Date.now() + duration;
    opts.onTimeoutStart?.();
    for (let round = 0; round < 4; round += 1) {
      if (opts.signal?.aborted) return undefined;
      const remaining = deadline === undefined ? undefined : deadline - Date.now();
      if (remaining !== undefined && remaining <= 0) {
        opts.onTimeout?.();
        return { data, timedOut: true };
      }
      const schema = formSchema(form, api, state, errors);
      const controller = new AbortController();
      let timedOut = false;
      let timer;
      const expired = new Promise((resolve) => {
        if (remaining !== undefined) timer = setTimeout(() => {
          timedOut = true;
          controller.abort();
          resolve(null);
        }, remaining);
      });
      const signal = opts.signal ? AbortSignal.any([opts.signal, controller.signal]) : controller.signal;
      let onCancel;
      const cancelled = new Promise((resolve) => {
        onCancel = () => resolve({ cancelled: true });
        opts.signal?.addEventListener("abort", onCancel, { once: true });
      });
      let response;
      try {
        response = await Promise.race([
          park({ method: "formDialog", title: formContext(form, api.spansText), requestedSchema: schema }, {
            ...opts, timeout: remaining, signal,
          }),
          expired,
          cancelled,
        ]);
      } finally {
        clearTimeout(timer);
        opts.signal?.removeEventListener("abort", onCancel);
        controller.abort();
      }
      if (opts.signal?.aborted) return undefined;
      // Explicit cancellation always wins over a coincident deadline.
      if (response?.cancelled && !response.timedOut) return undefined;
      if (timedOut || (response?.timedOut && deadline !== undefined && Date.now() >= deadline)) {
        opts.onTimeout?.();
        return { data, timedOut: true };
      }
      if (!response || response.cancelled || !response.value || typeof response.value !== "object") return undefined;
      state = { ...state, ...Object.fromEntries(Object.entries(response.value).filter(([key]) => Object.hasOwn(schema.properties, key))) };
      ({ data, errors } = readFormAnswers(form, api, state));
      if (!errors.size) return { data };
    }
    throw new Error("OMP form answers repeatedly failed native field validation");
  };
}

const ompOptions = {
  harness: "omp-native",
  displayName: "OMP",
  configEnv: "OMNIGENT_OMP_NATIVE_CONFIG",
  pidEnv: "OMNIGENT_OMP_NATIVE_PID",
  authDirEnv: "OMNIGENT_OMP_NATIVE_AUTH_DIR",
  inferenceBoundEnv: "OMNIGENT_OMP_INFERENCE_BOUND",
  omitIdleDeliveryMode: true,
  asyncCompaction: true,
  isolateChildren: true,
  strictPolicyRequired: true,
  enforceResultPolicy: true,
  formDialog,
};

export default function installOmpBridge(host) {
  const instances = new Map();
  const subscriptions = new Set();
  const callOwners = new Map();
  function bind(key, register = false) {
    const handlers = new Map();
    const tools = new Map();
    const local = Object.create(null);
    const api = new Proxy(local, {
      get(target, property) {
        if (property in target) return target[property];
        const value = host[property];
        return typeof value === "function" ? value.bind(host) : value;
      },
    });
    local.on = (name, handler) => {
      handlers.set(name, handler);
      if (subscriptions.has(name)) return;
      subscriptions.add(name);
      host.on(name, async (event, ctx) => {
        if (name === "session_shutdown") {
          const results = await Promise.all([...instances.values()].map((instance) => instance.handlers.get(name)?.(event, ctx)));
          instances.clear();
          callOwners.clear();
          return results[0];
        }
        // Advisors can issue tools on their parent's runner. They need their own
        // bridge just like factory-rebound task/eval children, including depth 0.
        const identity = ctx?.agent;
        const instanceKey = identity?.kind === "sub"
          ? `${ctx.sessionManager?.getSessionId?.() || ""}:${identity.id}` : "main";
        const instance = instances.get(instanceKey) || bind(instanceKey);
        if (name === "tool_call" && event?.toolCallId) {
          callOwners.set(event.toolCallId, instance);
          while (callOwners.size > 4096) callOwners.delete(callOwners.keys().next().value);
        }
        return instance.handlers.get(name)?.(event, ctx);
      });
    };
    local.registerCommand = register ? host.registerCommand.bind(host) : () => {};
    local.registerTool = (tool) => {
      tools.set(tool.name, tool);
      if (!register) return;
      host.registerTool({
        ...tool,
        execute(callId, ...args) {
          const owner = callOwners.get(callId) || instances.get("main");
          const implementation = owner?.tools.get(tool.name);
          if (!implementation) throw new Error("OMP tool has no isolated Omnigent session");
          return implementation.execute(callId, ...args);
        },
      });
    };
    const instance = { handlers, tools };
    instances.set(key, instance);
    installBridge(api, ompOptions);
    return instance;
  }
  bind("main", true);
}
