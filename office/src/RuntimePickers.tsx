import { useEffect, useState } from "react";
import { type HarnessModels, modelEfforts } from "../shared/harnessModels.ts";
import { type HarnessReadiness, READINESS_LABELS, harnessReadiness } from "../shared/troubleshoot.ts";
import { officeApi } from "./api.ts";
import { useRuntimeOptions } from "./capabilities.ts";

const CUSTOM_MODEL = "\u0000custom";
const modelLists = new Map<string, Promise<HarnessModels>>();

function loadModels(hostId: string, harness: string): Promise<HarnessModels> {
  const key = `${hostId}\n${harness}`;
  let pending = modelLists.get(key);
  if (!pending) {
    pending = officeApi
      .harnessModels(hostId, harness)
      .catch((error: Error): HarnessModels => ({ listed: false, models: [], efforts: [], source: null, effortNote: null, error: error.message }));
    modelLists.set(key, pending);
    // The service caches the host probe; dropping ours lets a reopened form see a fresh list.
    void pending.finally(() => window.setTimeout(() => modelLists.delete(key), 30_000));
  }
  return pending;
}

/** The host's model list for a harness; null while loading or without a host. */
export function useHarnessModels(hostId: string | undefined, harness: string): HarnessModels | null {
  const [models, setModels] = useState<HarnessModels | null>(null);
  useEffect(() => {
    setModels(null);
    if (!hostId || !harness) return;
    let cancelled = false;
    void loadModels(hostId, harness).then((result) => !cancelled && setModels(result));
    return () => {
      cancelled = true;
    };
  }, [hostId, harness]);
  return models;
}

/** Readiness of each harness on a host, from the host's own report. */
export function useHarnessReadiness(hostId: string | undefined): (harness: string) => HarnessReadiness {
  const report = (useRuntimeOptions()?.hosts.hosts ?? []).find((host) => host.host_id === hostId)?.configured_harnesses;
  return (harness) => (hostId ? harnessReadiness(harness, report) : "unknown");
}

interface HarnessSelectProps {
  id?: string;
  value: string;
  hostId: string | undefined;
  onChange: (harness: string) => void;
  /** Shown for an empty value, e.g. "Role default (codex)". */
  emptyLabel?: string;
  disabled?: boolean;
  /** Offer only these harness ids instead of Omnigent's harness list. */
  only?: readonly string[];
}

/** Harness picker that shows each harness's readiness on the host. */
export function HarnessSelect({ id, value, hostId, onChange, emptyLabel, disabled, only }: HarnessSelectProps) {
  const listed = useRuntimeOptions()?.harnesses.data ?? [];
  const harnesses = only ? only.map((harness) => listed.find((entry) => entry.id === harness) ?? { id: harness, label: harness }) : listed;
  const readiness = useHarnessReadiness(hostId);
  const label = (harness: string, name?: string) => {
    const state = readiness(harness);
    return state === "unknown" ? (name ?? harness) : `${name ?? harness} · ${READINESS_LABELS[state]}`;
  };
  const known = harnesses.some((entry) => entry.id === value);
  return (
    <select id={id} value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)}>
      {emptyLabel !== undefined ? <option value="">{emptyLabel}</option> : null}
      {value && !known ? <option value={value}>{label(value)}</option> : null}
      {harnesses.map((entry) => (
        <option key={entry.id} value={entry.id}>
          {label(entry.id, entry.label)}
        </option>
      ))}
    </select>
  );
}

interface ModelSelectProps {
  id?: string;
  hostId: string | undefined;
  harness: string;
  value: string;
  onChange: (model: string) => void;
  disabled?: boolean;
}

/** Model picker listing what the harness offers on the host, with a typed id as the fallback. */
export function ModelSelect({ id, hostId, harness, value, onChange, disabled }: ModelSelectProps) {
  const list = useHarnessModels(hostId, harness);
  const listed = list?.listed && list.models.length > 0;
  const [typing, setTyping] = useState(false);
  const known = Boolean(list?.models.some((model) => model.id === value));
  const custom = typing || Boolean(value && list && !known);
  const defaultModel = list?.models.find((model) => model.isDefault);
  const sourceHint = list?.source === "local_cli" ? "Listed by the harness CLI on this machine" : null;

  if (!listed) {
    const hint = !hostId
      ? "Choose a host to list models"
      : list === null
        ? "Loading models…"
        : list.error
          ? `Models unavailable (${list.error}); type an id or leave blank`
          : "This harness does not list its models; type an id or leave blank";
    return (
      <span className="model-select">
        <input id={id} value={value} disabled={disabled} placeholder="Harness default" onChange={(event) => onChange(event.target.value)} />
        <span className="muted small">{hint}</span>
      </span>
    );
  }

  return (
    <span className="model-select">
      <select
        id={id}
        disabled={disabled}
        value={custom ? CUSTOM_MODEL : value}
        onChange={(event) => {
          const next = event.target.value;
          setTyping(next === CUSTOM_MODEL);
          if (next !== CUSTOM_MODEL) onChange(next);
        }}
      >
        <option value="">Harness default{defaultModel ? ` (${defaultModel.label})` : ""}</option>
        {list.models.map((model) => (
          <option key={model.id} value={model.id}>
            {model.label}
          </option>
        ))}
        <option value={CUSTOM_MODEL}>Other model id…</option>
      </select>
      {custom ? (
        <input aria-label="Model id" value={value} disabled={disabled} placeholder="Exact model id" onChange={(event) => onChange(event.target.value)} />
      ) : null}
      {sourceHint ? <span className="muted small">{sourceHint}</span> : null}
    </span>
  );
}

interface EffortSelectProps {
  id?: string;
  hostId: string | undefined;
  harness: string;
  model: string;
  value: string;
  onChange: (effort: string) => void;
  disabled?: boolean;
}

/** Reasoning effort the harness accepts, narrowed to what the chosen model supports. */
export function EffortSelect({ id, hostId, harness, model, value, onChange, disabled }: EffortSelectProps) {
  const list = useHarnessModels(hostId, harness);
  const chosen = list?.models.find((entry) => entry.id === model) ?? (model ? undefined : list?.models.find((entry) => entry.isDefault));
  const levels = list ? modelEfforts(list.efforts, chosen) : [];
  const offered = levels.includes(value);

  // A model or harness change can leave a level the new choice does not take.
  useEffect(() => {
    if (list && value && !offered) onChange("");
  }, [list, value, offered, onChange]);

  if (!list) return <span className="muted small">{hostId ? "Loading…" : "—"}</span>;
  if (levels.length === 0) return <span className="muted small">{list.effortNote ?? "Not adjustable on this harness"}</span>;
  return (
    <select id={id} value={offered ? value : ""} disabled={disabled} onChange={(event) => onChange(event.target.value)}>
      <option value="">Default{chosen?.defaultEffort ? ` (${chosen.defaultEffort})` : ""}</option>
      {levels.map((level) => (
        <option key={level} value={level}>
          {level}
        </option>
      ))}
    </select>
  );
}
