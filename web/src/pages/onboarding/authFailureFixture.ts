// Developer-only fixture for the sign-in failure and recovery screens.
//
// Drives the real ServerSelectorV2 / SetupTerminalStep through scripted
// runner results so the failure line, the neutral next-step line, Retry and
// "Copy diagnostics" can be inspected without a live SSO or Arca failure. The
// failure copy mirrors what the Electron shell sends (see electron/src/arca.js
// and server_manager.js). Honored only in a development build and only with
// `?mock=1`; production bundles drop it. Everything it shows is simulated.
//
// Query params:
//   authFailure=laptop            laptop sign-in never completes
//   authFailure=arca-auth         Arca sign-in error surfaced instead of a connect timeout
//   authFailure=arca-timeout      Arca connect timed out with no sign-in error
//   authFailure=laptop-recovery   laptop fails once, the next Retry succeeds
//   authFailure=arca-recovery     Arca fails once, the next Retry succeeds
//   clipboard=deny                make the browser clipboard write fail, to show "Couldn't copy"

import type { Runner } from "./RunnerStep";
import type { ServerSelectorV2Setup, SetupResult } from "./ServerSelectorV2";

export const AUTH_FAILURE_SCENARIOS = [
  "laptop",
  "arca-auth",
  "arca-timeout",
  "laptop-recovery",
  "arca-recovery",
] as const;
export type AuthFailureScenario = (typeof AUTH_FAILURE_SCENARIOS)[number];

export const SIMULATED_BANNER = "Simulated failure fixture. Developer preview, not a live sign-in.";

const FALLBACK_SERVER = "https://team.example.test";

interface ScenarioDefinition {
  runner: Runner;
  /** Log lines streamed before the run settles. */
  logs: (url: string) => string[];
  failure: (
    url: string,
  ) => Required<Pick<SetupResult, "error" | "errorMessage" | "errorAction" | "errorKind">>;
  /** The attempt (1-based) that succeeds; absent → every attempt fails. */
  succeedsOnAttempt?: number;
}

function combined(message: string, action: string) {
  return { error: `${message} ${action}`, errorMessage: message, errorAction: action };
}

const LAPTOP_FAILURE = (url: string) => ({
  ...combined(
    `Sign-in to ${url} didn't complete. A browser window should have opened.`,
    `Finish signing in and try again (or run \`omnigent login ${url}\` in a terminal).`,
  ),
  errorKind: "laptop-sign-in",
});
const LAPTOP_LOGS = (url: string) => [
  `$ omnigent host --server ${url}`,
  "Signing in to the server if needed…",
  "If a browser window opens, finish signing in there.",
];
const ARCA_LOGS = (url: string) => [
  `$ arca ssh isaac omni host --server ${url} --background --non-interactive`,
  "If sign-in is needed, a browser opens on your laptop. Finish signing in to continue.",
  "OMNIGENT_AUTH_REQUIRED: the Arca instance is waiting for a browser sign-in",
];

const SCENARIOS: Record<AuthFailureScenario, ScenarioDefinition> = {
  laptop: { runner: "local", logs: LAPTOP_LOGS, failure: LAPTOP_FAILURE },
  "laptop-recovery": {
    runner: "local",
    logs: LAPTOP_LOGS,
    failure: LAPTOP_FAILURE,
    succeedsOnAttempt: 2,
  },
  "arca-auth": {
    runner: "remote",
    logs: ARCA_LOGS,
    failure: () => ({
      ...combined(
        "The Arca instance isn't signed in to this server.",
        "Run `arca ssh` and sign in with `isaac omni login <server-url>`, then try again.",
      ),
      errorKind: "omni-auth",
    }),
  },
  "arca-recovery": {
    runner: "remote",
    logs: ARCA_LOGS,
    failure: () => ({
      ...combined(
        "The Arca instance isn't signed in to this server.",
        "Run `arca ssh` and sign in with `isaac omni login <server-url>`, then try again.",
      ),
      errorKind: "omni-auth",
    }),
    succeedsOnAttempt: 2,
  },
  "arca-timeout": {
    runner: "remote",
    logs: (url) => [
      `$ arca ssh isaac omni host --server ${url} --background --non-interactive`,
      "Waiting for the Arca instance to start…",
    ],
    failure: () => ({
      ...combined(
        "Connecting to Arca timed out. The instance may still be starting.",
        "Check `arca status` and try again.",
      ),
      errorKind: "timeout",
    }),
  },
};

export function parseAuthFailureScenario(params: URLSearchParams): AuthFailureScenario | null {
  // Dev builds only: the production bundle never honors the param.
  if (!import.meta.env.DEV) return null;
  const value = params.get("authFailure");
  return (AUTH_FAILURE_SCENARIOS as readonly string[]).includes(value ?? "")
    ? (value as AuthFailureScenario)
    : null;
}

/** Real browser clipboard write, or a scripted refusal (`clipboard=deny`). */
export function copyToBrowserClipboard(text: string, denied: boolean): Promise<void> {
  if (denied) return Promise.reject(new Error("Clipboard write denied by fixture."));
  if (!navigator.clipboard?.writeText) {
    return Promise.reject(new Error("Clipboard API unavailable."));
  }
  return navigator.clipboard.writeText(text);
}

/**
 * Setup fields that make the wizard open on the scripted run. Spread over a
 * mock setup; `onRunnerLog` / `onConnectRunner` replace the mock's.
 */
export function authFailureFixture(
  scenario: AuthFailureScenario,
  serverUrl: string | undefined,
): Pick<
  ServerSelectorV2Setup,
  "initialTerminal" | "simulatedFailures" | "onConnectRunner" | "onRunnerLog"
> {
  const definition = SCENARIOS[scenario];
  const url = serverUrl ?? FALLBACK_SERVER;
  let attempt = 0;
  let sink: ((line: string) => void) | null = null;
  return {
    initialTerminal: { url, runner: definition.runner },
    simulatedFailures: true,
    onRunnerLog: (cb) => {
      sink = cb;
      return () => {
        if (sink === cb) sink = null;
      };
    },
    onConnectRunner: (runUrl) => {
      attempt += 1;
      const succeeds =
        definition.succeedsOnAttempt !== undefined && attempt >= definition.succeedsOnAttempt;
      const lines = [
        "[simulated] no real sign-in is attempted",
        ...(succeeds ? ["Signed in. Connecting…", "Host is running"] : definition.logs(runUrl)),
      ];
      return new Promise<SetupResult>((resolve) => {
        let i = 0;
        const timer = setInterval(() => {
          if (i < lines.length) {
            sink?.(lines[i++]);
            return;
          }
          clearInterval(timer);
          if (succeeds) {
            resolve({ ok: true });
          } else {
            resolve({ ok: false, ...definition.failure(runUrl) });
          }
        }, 250);
      });
    },
  };
}
