import { getSession, postEvent } from "@/lib/sessionsApi";
import type { DpiaCaseSnapshot } from "./types";
import { buildDpiaProcessingModelArtifact } from "./agentContext";
import { findDpiaCaseSession } from "./caseSession";

export interface LiveInvestigationResult {
  sessionId: string;
  completedAt: string;
}

export class LiveInvestigationUnavailable extends Error {
  constructor(message: string) {
    super(message);
    this.name = "LiveInvestigationUnavailable";
  }
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, milliseconds);
  });
}

async function waitForLiveCompletion(
  sessionId: string,
  startedAt: number,
  observedActive: boolean,
  onProgress: (message: string) => void,
  signal?: AbortSignal,
): Promise<LiveInvestigationResult> {
  if (signal?.aborted) throw new DOMException("The live run was cancelled.", "AbortError");
  if (Date.now() - startedAt >= 120_000) {
    throw new Error(
      "The live investigation did not complete within two minutes. It may still be running in the live session; the validated snapshot is unchanged.",
    );
  }

  await delay(1_500);
  const session = await getSession(sessionId);
  if (session.status === "failed") {
    throw new Error(
      session.lastTaskError?.message ??
        "The live investigation session failed. The validated snapshot is unchanged.",
    );
  }

  const isActive = ["launching", "running", "waiting"].includes(session.status);
  const hasBeenActive = observedActive || isActive;
  if (isActive) {
    onProgress(
      session.status === "waiting"
        ? "Live run is waiting for an explicit approval"
        : "Professional-role sessions are working",
    );
  }
  if (hasBeenActive && session.status === "idle") {
    return { sessionId, completedAt: new Date().toISOString() };
  }
  if (!hasBeenActive && Date.now() - startedAt > 15_000 && session.status === "idle") {
    throw new Error(
      "The session accepted the request but did not start within 15 seconds. Retry the live run or inspect the session; the validated snapshot is unchanged.",
    );
  }
  return waitForLiveCompletion(sessionId, startedAt, hasBeenActive, onProgress, signal);
}

export async function runLiveInvestigation(
  caseData: DpiaCaseSnapshot,
  onProgress: (message: string) => void,
  signal?: AbortSignal,
): Promise<LiveInvestigationResult> {
  onProgress("Looking for a configured DPIA investigation session");
  let root: Awaited<ReturnType<typeof findDpiaCaseSession>>;
  try {
    root = await findDpiaCaseSession(caseData.id, signal);
  } catch (error) {
    throw new LiveInvestigationUnavailable(
      `${error instanceof Error ? error.message : "Could not inspect sessions."} The validated snapshot is unchanged.`,
    );
  }
  if (!root) {
    throw new LiveInvestigationUnavailable(
      "No labelled DPIA root session is configured. Install the professional-role bundle and create a session labelled omnigent.product=dpia-investigation and omnigent.case_id=student-success-alert.",
    );
  }

  onProgress("Submitting processing model to the live root session");
  await postEvent(root.sessionId, {
    type: "message",
    data: {
      role: "user",
      content: [
        {
          type: "input_text",
          text: [
            "Run the DPIA investigation workflow for the synthetic Student Success Alert case.",
            `Processing model version: ${caseData.processingModel.version}`,
            `Policy pack version: ${caseData.policyPack.version}`,
            "Current processing model JSON follows. Treat it as authoritative for current facts and version; use the bundled evidence pack only for cited provenance:",
            JSON.stringify(buildDpiaProcessingModelArtifact(caseData)),
            "Keep Process Investigator, Privacy Assessor, and Independent Verifier outputs schema-bound. Blind the verifier until its initial evidence review is complete. Do not contact real systems or people.",
          ].join("\n"),
        },
      ],
    },
  });

  onProgress("Live run accepted; waiting for the professional-role workflow");
  return waitForLiveCompletion(root.sessionId, Date.now(), false, onProgress, signal);
}
