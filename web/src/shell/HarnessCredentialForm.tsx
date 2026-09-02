/**
 * Inline "Add a credential" form for the harness setup dialog (Claude/Codex/Pi).
 *
 * Presents every applicable auth path as an equally-weighted, labeled option
 * separated by "or", so it reads as a set of alternatives rather than one
 * primary field with the rest demoted:
 *   - adopt a credential omnigent detected on the host (only when one is found),
 *   - sign in with the harness's subscription (a copy-command — the UI can't
 *     drive browser OAuth; omitted for Pi, which has no CLI login),
 *   - use an API key,
 *   - connect a gateway (base URL + key).
 * Writing goes through {@link useStoreCredential}; on success the host's
 * readiness flips and the dialog's row turns green.
 */

import { Fragment, useState } from "react";
import { KeyRound, UserRound, Waypoints } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { showToast } from "@/components/ui/toast";
import { copyText } from "@/lib/clipboard";
import {
  useDetectedCredentials,
  useStoreCredential,
  type DetectedCredential,
  type Host,
  type StoreCredentialInput,
} from "@/hooks/useHosts";
import { harnessCredentialAdoptFamilies } from "@/lib/harnessSetup";

export function HarnessCredentialForm({
  harness,
  label,
  host,
  command,
  onDone,
}: {
  harness: string;
  /** Friendly display name for user-facing toasts (e.g. "Codex"); the raw
   *  `harness` id (e.g. "codex-native") is still used for the readiness lookup
   *  and the API call. Falls back to `harness` when not supplied. */
  label?: string;
  host: Host | undefined | null;
  /** The server's subscription-login command (e.g. `codex login`), shown as a
   *  copy signpost — the UI can't drive browser OAuth. `null` for Pi. */
  command: string | null;
  onDone: () => void;
}) {
  // What the user sees in toasts — the friendly name, not the canonical id.
  const display = label ?? harness;
  const store = useStoreCredential(host?.host_id ?? "");
  // Detection is only meaningful for a live host and this open form.
  const { data: detected } = useDetectedCredentials(host?.host_id, !!host);
  // The detect endpoint is host-wide (both families), so scope the adopt
  // affordance to the families THIS harness can adopt — otherwise we could offer
  // (and persist) a cross-family key, e.g. an Anthropic key for Codex. Pi
  // consumes both families, so it can adopt an openai OR an anthropic key.
  const adoptFamilies = harnessCredentialAdoptFamilies(harness);
  const adoptable = (detected ?? []).find((d) => d.env_var && adoptFamilies.includes(d.family));

  const [apiKey, setApiKey] = useState("");
  const [gatewayUrl, setGatewayUrl] = useState("");
  const [gatewayKey, setGatewayKey] = useState("");

  const busy = store.isPending;

  const submit = (input: StoreCredentialInput) => {
    // The form only mounts for an online host (gated by harnessAuthableOnHost),
    // so a host id is always present — but guard explicitly rather than POST to
    // a malformed `/v1/hosts//…` URL if that invariant ever changes.
    if (!host?.host_id) return;
    store.mutate(input, {
      onSuccess: (result) => {
        // Key the toast on the harness's REFRESHED readiness, not the mere fact
        // that the write succeeded: a credential can be stored yet leave the
        // harness not-ready (e.g. the daemon couldn't route it), and claiming
        // "ready" then would contradict the still-yellow dot. Only close the
        // form when the harness actually turned ready, so a not-ready result
        // keeps the options visible to try another.
        const ready = result.configured_harnesses[harness] === true;
        // Clear the secret fields so a saved value doesn't linger in the input
        // (the form stays open on a not-ready result to try another option).
        setApiKey("");
        setGatewayUrl("");
        setGatewayKey("");
        if (ready) {
          showToast(`${display} is ready on ${host.name}.`);
          onDone();
        } else {
          showToast(
            `Saved, but ${display} still needs setup on ${host.name}. Try another option.`,
            { duration: 0 },
          );
        }
      },
      onError: (err) => showToast(`Couldn't save the credential: ${err.message}`, { duration: 0 }),
    });
  };

  // Build the applicable options in a fixed order; each renders as an
  // equally-weighted labeled block, joined by "or" dividers below. Adopt and
  // subscription are conditional; API key and gateway always apply. Each carries
  // a stable id key: the async adopt row prepends once detection resolves, so an
  // array-index key would shift and remount the rows below it.
  const options: { id: string; node: React.ReactNode }[] = [];
  if (adoptable) {
    options.push({
      id: "adopt",
      node: (
        <CredentialOption icon={<KeyRound />} label="Use a credential found on this host">
          <AdoptRow
            detected={adoptable}
            busy={busy}
            onUse={() =>
              submit({ harness, kind: "adopt", env_var: adoptable.env_var ?? undefined })
            }
          />
        </CredentialOption>
      ),
    });
  }
  if (command) {
    options.push({
      id: "subscription",
      node: (
        <CredentialOption icon={<UserRound />} label="Sign in with your subscription">
          <p className="text-sm text-muted-foreground">
            Use your existing plan — run{" "}
            <button
              type="button"
              className="rounded bg-muted px-1 py-0.5 font-mono hover:text-foreground"
              data-testid="harness-credential-login-copy"
              onClick={() => {
                void copyText(command)
                  .then(() => showToast("Copied to clipboard."))
                  .catch(() =>
                    showToast("Couldn't copy — select and copy manually.", { duration: 0 }),
                  );
              }}
            >
              {command}
            </button>{" "}
            on {host?.name}.
          </p>
        </CredentialOption>
      ),
    });
  }
  options.push({
    id: "key",
    node: (
      <CredentialOption icon={<KeyRound />} label="Use an API key">
        <form
          className="flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            // Guard on !busy too: the Save button is disabled while a write is
            // in flight, but Enter in the field would otherwise re-fire the POST.
            if (!busy && apiKey.trim()) submit({ harness, kind: "key", secret: apiKey.trim() });
          }}
        >
          <Input
            type="password"
            autoComplete="off"
            placeholder="Paste an API key"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            data-testid="harness-credential-key"
            className="h-8 font-mono text-sm"
          />
          <SaveButton busy={busy} disabled={!apiKey.trim()} testId="harness-credential-save" />
        </form>
      </CredentialOption>
    ),
  });
  options.push({
    id: "gateway",
    node: (
      <CredentialOption icon={<Waypoints />} label="Connect a gateway">
        <p className="text-sm text-muted-foreground">
          Route through an OpenAI-compatible proxy (e.g. OpenRouter).
        </p>
        <form
          className="flex flex-col gap-2"
          data-testid="harness-credential-gateway"
          onSubmit={(e) => {
            e.preventDefault();
            // See the API-key form: gate on !busy so Enter can't re-POST mid-save.
            if (!busy && gatewayUrl.trim() && gatewayKey.trim()) {
              submit({
                harness,
                kind: "gateway",
                base_url: gatewayUrl.trim(),
                secret: gatewayKey.trim(),
              });
            }
          }}
        >
          <Input
            type="text"
            placeholder="Gateway base URL (e.g. https://openrouter.ai/api/v1)"
            value={gatewayUrl}
            onChange={(e) => setGatewayUrl(e.target.value)}
            className="h-8 text-sm"
          />
          <div className="flex items-center gap-2">
            <Input
              type="password"
              autoComplete="off"
              placeholder="Gateway API key"
              value={gatewayKey}
              onChange={(e) => setGatewayKey(e.target.value)}
              className="h-8 font-mono text-sm"
            />
            <SaveButton busy={busy} disabled={!gatewayUrl.trim() || !gatewayKey.trim()} />
          </div>
        </form>
      </CredentialOption>
    ),
  });

  return (
    <div className="flex flex-col gap-3 py-1" data-testid="harness-credential-form">
      {options.map((option, i) => (
        <Fragment key={option.id}>
          {i > 0 && <OrDivider />}
          {option.node}
        </Fragment>
      ))}
    </div>
  );
}

/** A labeled, equally-weighted auth choice block, with a leading glyph so the
 *  paths are scannable at a glance. */
function CredentialOption({
  icon,
  label,
  children,
}: {
  icon: React.ReactNode;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="flex items-center gap-1.5 text-sm font-medium">
        <span className="text-muted-foreground [&_svg]:size-3.5">{icon}</span>
        {label}
      </span>
      {children}
    </div>
  );
}

/** Save button whose disabled state stays a light, clearly-inert outline
 *  (rather than a heavy dark fill that reads as an error). */
function SaveButton({
  busy,
  disabled,
  testId,
}: {
  busy: boolean;
  disabled: boolean;
  testId?: string;
}) {
  return (
    <Button
      type="submit"
      size="sm"
      loading={busy}
      disabled={disabled}
      variant={disabled ? "outline" : "default"}
      data-testid={testId}
      className="shrink-0"
    >
      Save
    </Button>
  );
}

/** Short, centered "or" separator between two equally-weighted options — a
 *  quiet divider, not a section header. */
function OrDivider() {
  return (
    <div
      className="flex items-center justify-center gap-2 text-[10px] uppercase tracking-wide text-muted-foreground"
      aria-hidden
    >
      <span className="h-px w-6 bg-border" />
      or
      <span className="h-px w-6 bg-border" />
    </div>
  );
}

function AdoptRow({
  detected,
  busy,
  onUse,
}: {
  detected: DetectedCredential;
  busy: boolean;
  onUse: () => void;
}) {
  return (
    <div
      className="flex items-center gap-2 rounded bg-emerald-50 px-2 py-1.5 dark:bg-emerald-500/10"
      data-testid="harness-credential-adopt"
    >
      <span className="flex-1 text-sm">
        Found <code className="font-mono">{detected.source}</code> on this host.
      </span>
      <Button type="button" size="sm" loading={busy} onClick={onUse}>
        Use it
      </Button>
    </div>
  );
}
