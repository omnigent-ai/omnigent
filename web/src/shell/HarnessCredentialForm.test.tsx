// Tests for HarnessCredentialForm — the inline "Add a credential" form for the
// harness setup dialog. Covers the equally-weighted options (adopt a detected
// key, sign in with a subscription, paste an API key, connect a gateway) and
// that they're presented as alternatives ("or" dividers), not one primary field
// with the rest demoted. The hooks are mocked so the form is exercised without a
// live host.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Host } from "@/hooks/useHosts";
import { HarnessCredentialForm } from "./HarnessCredentialForm";

const mutateMock = vi.fn();
const { copyTextMock, showToastMock } = vi.hoisted(() => ({
  copyTextMock: vi.fn(() => Promise.resolve()),
  showToastMock: vi.fn(),
}));
vi.mock("@/lib/clipboard", () => ({ copyText: copyTextMock }));
vi.mock("@/components/ui/toast", () => ({ showToast: showToastMock }));

let detected: { family: string; source: string; env_var: string | null }[] = [];
let storePending = false;
vi.mock("@/hooks/useHosts", () => ({
  useStoreCredential: () => ({ mutate: mutateMock, isPending: storePending }),
  useDetectedCredentials: () => ({ data: detected }),
}));

const HOST = { host_id: "host_1", name: "corey-laptop", status: "online" } as Host;

afterEach(() => {
  cleanup();
  mutateMock.mockReset();
  copyTextMock.mockClear();
  showToastMock.mockReset();
  detected = [];
  storePending = false;
});

describe("HarnessCredentialForm", () => {
  it("saves a pasted API key", () => {
    render(
      <HarnessCredentialForm harness="codex" host={HOST} command="codex login" onDone={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("harness-credential-key"), {
      target: { value: "sk-ant-XYZ" },
    });
    fireEvent.click(screen.getByTestId("harness-credential-save"));
    expect(mutateMock).toHaveBeenCalledWith(
      { harness: "codex", kind: "key", secret: "sk-ant-XYZ" },
      expect.anything(),
    );
  });

  it("does not submit an empty key (Save disabled)", () => {
    render(<HarnessCredentialForm harness="codex" host={HOST} command={null} onDone={vi.fn()} />);
    expect((screen.getByTestId("harness-credential-save") as HTMLButtonElement).disabled).toBe(
      true,
    );
  });

  it("submits a gateway", () => {
    render(<HarnessCredentialForm harness="codex" host={HOST} command={null} onDone={vi.fn()} />);
    // Gateway is an equally-weighted option, always shown (no toggle).
    const gateway = screen.getByTestId("harness-credential-gateway");
    const [urlInput, keyInput] = gateway.querySelectorAll("input");
    fireEvent.change(urlInput, { target: { value: "https://openrouter.ai/api/v1" } });
    fireEvent.change(keyInput, { target: { value: "or-KEY" } });
    fireEvent.submit(gateway);
    expect(mutateMock).toHaveBeenCalledWith(
      {
        harness: "codex",
        kind: "gateway",
        base_url: "https://openrouter.ai/api/v1",
        secret: "or-KEY",
      },
      expect.anything(),
    );
  });

  it("offers a detected credential for one-click adopt", () => {
    detected = [{ family: "openai", source: "$OPENAI_API_KEY", env_var: "OPENAI_API_KEY" }];
    render(<HarnessCredentialForm harness="codex" host={HOST} command={null} onDone={vi.fn()} />);
    const adopt = screen.getByTestId("harness-credential-adopt");
    expect(adopt.textContent).toContain("$OPENAI_API_KEY");
    fireEvent.click(adopt.querySelector("button")!);
    expect(mutateMock).toHaveBeenCalledWith(
      { harness: "codex", kind: "adopt", env_var: "OPENAI_API_KEY" },
      expect.anything(),
    );
  });

  it("does not offer a cross-family credential for adopt", () => {
    // The detect endpoint is host-wide: an Anthropic key is present but the
    // harness is Codex (openai family). The adopt row must NOT appear — offering
    // "Use $ANTHROPIC_API_KEY" for Codex would persist an openai provider that
    // points at an Anthropic key.
    detected = [
      { family: "anthropic", source: "$ANTHROPIC_API_KEY", env_var: "ANTHROPIC_API_KEY" },
    ];
    render(<HarnessCredentialForm harness="codex" host={HOST} command={null} onDone={vi.fn()} />);
    expect(screen.queryByTestId("harness-credential-adopt")).toBeNull();
  });

  it("offers a Pi (anthropic-family) credential to adopt", () => {
    // Pi resolves to the anthropic family, so an ANTHROPIC_API_KEY is adoptable.
    detected = [
      { family: "anthropic", source: "$ANTHROPIC_API_KEY", env_var: "ANTHROPIC_API_KEY" },
    ];
    render(<HarnessCredentialForm harness="pi" host={HOST} command={null} onDone={vi.fn()} />);
    expect(screen.getByTestId("harness-credential-adopt").textContent).toContain(
      "$ANTHROPIC_API_KEY",
    );
  });

  it("offers Pi an openai-family credential too (Pi consumes both families)", () => {
    // Pi has no CLI login and routes through anthropic OR openai; the daemon
    // adopts under the detected family, so a host with only $OPENAI_API_KEY can
    // still back Pi. The adopt filter must not narrow Pi to anthropic-only.
    detected = [{ family: "openai", source: "$OPENAI_API_KEY", env_var: "OPENAI_API_KEY" }];
    render(<HarnessCredentialForm harness="pi" host={HOST} command={null} onDone={vi.fn()} />);
    const adopt = screen.getByTestId("harness-credential-adopt");
    expect(adopt.textContent).toContain("$OPENAI_API_KEY");
    fireEvent.click(adopt.querySelector("button")!);
    expect(mutateMock).toHaveBeenCalledWith(
      { harness: "pi", kind: "adopt", env_var: "OPENAI_API_KEY" },
      expect.anything(),
    );
  });

  it("shows no adopt row when nothing is detected", () => {
    detected = [];
    render(<HarnessCredentialForm harness="codex" host={HOST} command={null} onDone={vi.fn()} />);
    expect(screen.queryByTestId("harness-credential-adopt")).toBeNull();
  });

  it("copies the subscription login command (never a form field)", async () => {
    render(
      <HarnessCredentialForm
        harness="claude"
        host={HOST}
        command="claude auth login"
        onDone={vi.fn()}
      />,
    );
    const loginCopy = screen.getByTestId("harness-credential-login-copy");
    expect(loginCopy.textContent).toContain("claude auth login");
    fireEvent.click(loginCopy);
    await waitFor(() => expect(copyTextMock).toHaveBeenCalledWith("claude auth login"));
  });

  it("omits the subscription signpost when there is no login command (Pi)", () => {
    render(<HarnessCredentialForm harness="pi" host={HOST} command={null} onDone={vi.fn()} />);
    expect(screen.queryByTestId("harness-credential-login-copy")).toBeNull();
  });

  it("presents the paths as equal alternatives joined by 'or'", () => {
    // Codex with a detected key: adopt + subscription + API key + gateway = four
    // options, so three "or" dividers separate them. Each is a labeled block
    // (no field is the primary with the rest demoted).
    detected = [{ family: "openai", source: "$OPENAI_API_KEY", env_var: "OPENAI_API_KEY" }];
    const { container } = render(
      <HarnessCredentialForm harness="codex" host={HOST} command="codex login" onDone={vi.fn()} />,
    );
    // Each path carries its own equal-weight label.
    for (const label of [
      "Use a credential found on this host",
      "Sign in with your subscription",
      "Use an API key",
      "Connect a gateway",
    ]) {
      expect(screen.getByText(label)).toBeTruthy();
    }
    // Four options → three "or" separators between them.
    expect(countOrDividers(container)).toBe(3);
  });

  it("drops the subscription option for Pi, leaving key + gateway (one 'or')", () => {
    // Pi has no CLI login and nothing detected → just API key and gateway, so a
    // single "or" divides them; the subscription block is absent.
    const { container } = render(
      <HarnessCredentialForm harness="pi" host={HOST} command={null} onDone={vi.fn()} />,
    );
    expect(screen.getByText("Use an API key")).toBeTruthy();
    expect(screen.getByText("Connect a gateway")).toBeTruthy();
    expect(screen.queryByText("Sign in with your subscription")).toBeNull();
    expect(countOrDividers(container)).toBe(1);
  });

  it("confirms ready and closes only when the write turns the harness ready", () => {
    // The write succeeded AND readiness flipped to true → toast says ready and
    // the form closes (onDone).
    mutateMock.mockImplementation((_input, opts?: { onSuccess?: (r: unknown) => void }) =>
      opts?.onSuccess?.({
        object: "harness_credential",
        harness: "pi",
        configured_harnesses: { pi: true },
      }),
    );
    const onDone = vi.fn();
    render(<HarnessCredentialForm harness="pi" host={HOST} command={null} onDone={onDone} />);
    fireEvent.change(screen.getByTestId("harness-credential-key"), { target: { value: "sk-x" } });
    fireEvent.click(screen.getByTestId("harness-credential-save"));
    expect(showToastMock).toHaveBeenCalledWith(expect.stringContaining("is ready on"));
    expect(onDone).toHaveBeenCalled();
  });

  it("does NOT claim ready when the write leaves the harness not-ready", () => {
    // Regression: the toast used to say "ready" unconditionally, contradicting a
    // still-yellow dot. When readiness stays not-true, say so and keep the form
    // open (don't call onDone) so the user can try another option.
    mutateMock.mockImplementation((_input, opts?: { onSuccess?: (r: unknown) => void }) =>
      opts?.onSuccess?.({
        object: "harness_credential",
        harness: "pi",
        configured_harnesses: { pi: "needs-auth" },
      }),
    );
    const onDone = vi.fn();
    render(<HarnessCredentialForm harness="pi" host={HOST} command={null} onDone={onDone} />);
    const keyInput = screen.getByTestId("harness-credential-key") as HTMLInputElement;
    fireEvent.change(keyInput, { target: { value: "sk-x" } });
    fireEvent.click(screen.getByTestId("harness-credential-save"));
    expect(showToastMock).toHaveBeenCalledWith(
      expect.stringContaining("still needs setup"),
      expect.anything(),
    );
    expect(showToastMock).not.toHaveBeenCalledWith(expect.stringContaining("is ready on"));
    expect(onDone).not.toHaveBeenCalled();
    // The form stays open on a not-ready result; the entered key must not linger
    // in the field while the user tries another option.
    expect(keyInput.value).toBe("");
  });

  it("surfaces a sticky error toast when the write is rejected", () => {
    // A backend-rejected write (e.g. a malformed gateway base_url the host
    // bounces as a 502) rejects the mutation. The form must report the failure
    // via the onError toast — kept sticky (duration 0) so it isn't missed — and
    // must NOT claim ready or close.
    mutateMock.mockImplementation((_input, opts?: { onError?: (e: Error) => void }) =>
      opts?.onError?.(
        new Error(
          "host credential write failed: a gateway base_url must start with http:// or https://",
        ),
      ),
    );
    const onDone = vi.fn();
    render(<HarnessCredentialForm harness="codex" host={HOST} command={null} onDone={onDone} />);
    const gateway = screen.getByTestId("harness-credential-gateway");
    const [urlInput, keyInput] = gateway.querySelectorAll("input");
    fireEvent.change(urlInput, { target: { value: "ftp://nope" } });
    fireEvent.change(keyInput, { target: { value: "tok" } });
    fireEvent.submit(gateway);
    expect(showToastMock).toHaveBeenCalledWith(
      expect.stringContaining("Couldn't save the credential"),
      expect.objectContaining({ duration: 0 }),
    );
    expect(showToastMock).not.toHaveBeenCalledWith(expect.stringContaining("is ready on"));
    expect(onDone).not.toHaveBeenCalled();
  });

  it("does not re-submit on Enter while a write is in flight", () => {
    // The Save button is disabled during a write, but the input isn't — pressing
    // Enter in the field (paste-then-hammer-Enter) would re-fire the POST and
    // send the secret twice. The onSubmit is gated on !busy, so a submit while
    // pending is a no-op.
    storePending = true;
    render(<HarnessCredentialForm harness="codex" host={HOST} command={null} onDone={vi.fn()} />);
    const keyInput = screen.getByTestId("harness-credential-key");
    fireEvent.change(keyInput, { target: { value: "sk-x" } });
    fireEvent.submit(keyInput.closest("form")!);
    fireEvent.submit(keyInput.closest("form")!);
    expect(mutateMock).not.toHaveBeenCalled();
  });

  it("uses the friendly label (not the raw harness id) in the ready toast", () => {
    // The dialog passes a display name ("Codex") while the harness id is the
    // canonical "codex-native" (what readiness is keyed on). The toast must read
    // "Codex is ready", never "codex-native is ready" — the id is plumbing.
    mutateMock.mockImplementation((_input, opts?: { onSuccess?: (r: unknown) => void }) =>
      opts?.onSuccess?.({
        object: "harness_credential",
        harness: "codex-native",
        configured_harnesses: { "codex-native": true },
      }),
    );
    render(
      <HarnessCredentialForm
        harness="codex-native"
        label="Codex"
        host={HOST}
        command={null}
        onDone={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByTestId("harness-credential-key"), { target: { value: "sk-x" } });
    fireEvent.click(screen.getByTestId("harness-credential-save"));
    expect(showToastMock).toHaveBeenCalledWith("Codex is ready on corey-laptop.");
    expect(showToastMock).not.toHaveBeenCalledWith(expect.stringContaining("codex-native"));
  });

  it("keeps a light, non-error disabled Save until input is entered", () => {
    // A disabled Save must read as "waiting for input" (outline), not a heavy
    // dark fill that looks like an error/unavailable state. Typing a key flips
    // it to the solid primary.
    render(<HarnessCredentialForm harness="pi" host={HOST} command={null} onDone={vi.fn()} />);
    const save = screen.getByTestId("harness-credential-save") as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    expect(save.getAttribute("data-variant")).toBe("outline");
    fireEvent.change(screen.getByTestId("harness-credential-key"), { target: { value: "sk-x" } });
    expect((screen.getByTestId("harness-credential-save") as HTMLButtonElement).disabled).toBe(
      false,
    );
    expect(screen.getByTestId("harness-credential-save").getAttribute("data-variant")).toBe(
      "default",
    );
  });
});

/** Count the "or" divider blocks (a div whose visible text is exactly "or",
 *  its two flanking rules being empty spans). */
function countOrDividers(container: HTMLElement): number {
  return Array.from(container.querySelectorAll("div")).filter((d) => d.textContent === "or").length;
}
