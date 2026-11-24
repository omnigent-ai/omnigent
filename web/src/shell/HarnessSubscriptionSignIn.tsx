// The "Sign in with your subscription" option of the harness setup dialog's credential form: the
// harness's own login command, which the UI can't run, with a click that copies it. Settings'
// fix dialog never offers it, so it lives with the setup dialog rather than the shared form.

import { showToast } from "@/components/ui/toast";
import { copyText } from "@/lib/clipboard";

export function HarnessSubscriptionSignIn({
  command,
  hostName,
}: {
  /** The server's subscription-login command, such as `codex login`. */
  command: string;
  hostName: string | undefined;
}) {
  return (
    <p className="text-sm text-muted-foreground">
      Use your existing plan — run{" "}
      <button
        type="button"
        className="rounded bg-muted px-1 py-0.5 font-mono hover:text-foreground"
        data-testid="harness-credential-login-copy"
        onClick={() => {
          void copyText(command)
            .then(() => showToast("Copied to clipboard."))
            .catch(() => showToast("Couldn't copy — select and copy manually.", { duration: 0 }));
        }}
      >
        {command}
      </button>{" "}
      on {hostName}.
    </p>
  );
}
