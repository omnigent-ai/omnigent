import { type FormEvent, useEffect, useId, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { connectSshHost, getDesktopFeatures, supportsSshConnect } from "@/lib/nativeBridge";

/**
 * Whether to offer the SSH connect form: the desktop shell can connect over SSH,
 * and this window isn't one where Run on Arca (Databricks-internal) replaces it.
 * False until the shell answers, so the form never flashes in and out.
 */
export function useSshConnectAvailable(): boolean {
  const [available, setAvailable] = useState(false);
  useEffect(() => {
    if (!supportsSshConnect()) return;
    let active = true;
    void getDesktopFeatures().then((features) => {
      if (active) setAvailable(features?.databricksInternalFeatures !== true);
    });
    return () => {
      active = false;
    };
  }, []);
  return available;
}

/**
 * Connect a machine the user reaches over SSH as a host, through the desktop
 * shell. Consent, ssh prompts and the browser sign-in happen in the shell's
 * own window; this form only names the machine and reports gate failures.
 */
export function SshConnectForm({
  onConnected,
}: {
  /** Called with the new host's id, once it is known. */
  onConnected?: (hostId: string) => void;
}) {
  const queryClient = useQueryClient();
  const inputId = useId();
  const [destination, setDestination] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [message, setMessage] = useState<{ kind: "error" | "info"; text: string } | null>(null);

  async function connect(event: FormEvent) {
    event.preventDefault();
    const target = destination.trim();
    if (!target || connecting) return;
    setConnecting(true);
    setMessage(null);
    try {
      const res = await connectSshHost(target);
      if (!res.ok) {
        // The shell's window already showed its own outcome or the user closed it.
        if (!res.canceled && !res.shownInConsole) {
          setMessage({ kind: "error", text: res.error ?? `Couldn't connect to ${target}.` });
        }
        return;
      }
      await queryClient.invalidateQueries({ queryKey: ["hosts"] });
      if (res.identity) {
        onConnected?.(res.identity.hostId);
        return;
      }
      // Without a confirmed id, stay open and let the user pick: closing would keep the
      // previous host selected, and guessing could pick another machine that came online.
      setMessage({
        kind: "info",
        text: res.alreadyRunning
          ? `${target} was already connected to this server. Pick it from the host list.`
          : `${target} is connected. Pick it from the host list.`,
      });
    } finally {
      setConnecting(false);
    }
  }

  return (
    <form
      onSubmit={(event) => void connect(event)}
      className="flex flex-col gap-2"
      data-testid="ssh-connect-form"
    >
      <label htmlFor={inputId} className="text-sm text-muted-foreground">
        Or connect a machine you reach over SSH. Sign-in, and any SSH password or key prompt, happen
        in the app.
      </label>
      <div className="flex gap-2">
        <Input
          id={inputId}
          value={destination}
          onChange={(event) => setDestination(event.target.value)}
          placeholder="host alias or user@host"
          autoComplete="off"
          spellCheck={false}
          componentId="new_chat.ssh_connect.destination"
          data-testid="ssh-connect-destination"
        />
        <Button
          type="submit"
          variant="outline"
          disabled={connecting || !destination.trim()}
          componentId="new_chat.ssh_connect.submit"
          data-testid="ssh-connect-submit"
        >
          {connecting ? "Connecting..." : "Connect"}
        </Button>
      </div>
      {message && (
        <p
          className={
            message.kind === "error" ? "text-sm text-destructive" : "text-sm text-muted-foreground"
          }
          data-testid="ssh-connect-message"
        >
          {message.text}
        </p>
      )}
    </form>
  );
}
