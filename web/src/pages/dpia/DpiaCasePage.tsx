import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangleIcon,
  BotIcon,
  ChevronRightIcon,
  DownloadIcon,
  ExternalLinkIcon,
  FileCheck2Icon,
  FileTextIcon,
  LinkIcon,
  PrinterIcon,
  ShieldCheckIcon,
} from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { showToast } from "@/components/ui/toast";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { findOrCreateDpiaCaseSession } from "@/lib/dpia/caseSession";
import { decisionPackToMarkdown } from "@/lib/dpia/decisionPackMarkdown";
import { runLiveInvestigation } from "@/lib/dpia/liveInvestigation";
import { calculateReadiness } from "@/lib/dpia/readiness";
import { readinessDefinitions } from "@/lib/dpia/seed";
import { SynthesisRefusal, synthesizeDecisionPack } from "@/lib/dpia/synthesis";
import type {
  DecisionPack,
  Determination,
  EvidenceItem,
  StakeholderQuestion,
} from "@/lib/dpia/types";
import { Link, useParams, useSearchParams } from "@/lib/routing";
import { DpiaAudit } from "./DpiaAudit";
import { DpiaCaseChat } from "./DpiaCaseChat";
import {
  AgentActivityDialog,
  DecisionPackUnavailableDialog,
  DpiaIntakeDialog,
  EvidenceDialog,
  FindingProvenanceDialog,
  OfficerDecisionDialog,
  QuestionAnswerDialog,
  type LiveRunState,
  type OfficerAction,
} from "./DpiaDialogs";
import { DpiaEvidenceQuestions } from "./DpiaEvidenceQuestions";
import { DpiaFullAssessment } from "./DpiaFullAssessment";
import { DpiaOverview } from "./DpiaOverview";
import { DpiaPrintPack } from "./DpiaPrintPack";
import { DpiaProcessingMap } from "./DpiaProcessingMap";
import { DpiaScreening } from "./DpiaScreening";
import { DpiaStatus } from "./DpiaStatus";
import { StakeholderOutreachPanel } from "./StakeholderOutreachPanel";
import { useDpiaCase } from "./useDpiaCase";

type CaseTab = "overview" | "map" | "evidence" | "screening" | "full" | "audit";

export function DpiaCasePage() {
  const { caseId = "" } = useParams<{ caseId: string }>();
  if (caseId !== "student-success-alert") return <UnknownDpiaCase />;
  return <StudentSuccessAlertCase />;
}

function StudentSuccessAlertCase() {
  const controller = useDpiaCase("student-success-alert");
  const { caseData } = controller;
  const [searchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab");
  const initialTab: CaseTab = [
    "overview",
    "map",
    "evidence",
    "screening",
    "full",
    "audit",
  ].includes(requestedTab ?? "")
    ? (requestedTab as CaseTab)
    : "overview";
  const [activeTab, setActiveTab] = useState<CaseTab>(initialTab);
  const [bindingSession, setBindingSession] = useState(false);
  const [bindingError, setBindingError] = useState<string | null>(null);
  const [intakeOpen, setIntakeOpen] = useState(false);
  const [activityOpen, setActivityOpen] = useState(false);
  const [selectedEvidence, setSelectedEvidence] = useState<EvidenceItem | null>(null);
  const [selectedFinding, setSelectedFinding] = useState<Determination | null>(null);
  const [selectedQuestion, setSelectedQuestion] = useState<StakeholderQuestion | null>(null);
  const [officerAction, setOfficerAction] = useState<OfficerAction | null>(null);
  const [decisionPackError, setDecisionPackError] = useState<string | null>(null);
  const [printPack, setPrintPack] = useState<DecisionPack | null>(null);
  const [liveRun, setLiveRun] = useState<LiveRunState>(() =>
    caseData.liveRun?.status === "failed"
      ? { status: "failed", message: caseData.liveRun.message }
      : caseData.liveRun?.status === "completed"
        ? {
            status: "completed",
            message: caseData.liveRun.message,
            sessionId: caseData.liveRun.sessionId,
          }
        : {
            status: "idle",
            message:
              "No live run has been started. The cockpit is showing the validated demo snapshot.",
          },
  );
  const liveAbortRef = useRef<AbortController | null>(null);
  const dialogTriggerRef = useRef<HTMLElement | null>(null);
  const availableAgents = useAvailableAgents({ enabled: caseData.sessionId === undefined });
  useEffect(
    () => () => {
      liveAbortRef.current?.abort();
    },
    [],
  );
  useEffect(() => {
    if (
      requestedTab &&
      ["overview", "map", "evidence", "screening", "full", "audit"].includes(requestedTab)
    ) {
      setActiveTab(requestedTab as CaseTab);
    }
    const findingId = searchParams.get("finding");
    if (findingId) {
      setSelectedFinding(caseData.determinations.find(({ id }) => id === findingId) ?? null);
    }
    if (searchParams.get("agentActivity") === "1") setActivityOpen(true);
  }, [caseData.determinations, requestedTab, searchParams]);

  const readiness = useMemo(
    () =>
      calculateReadiness(
        caseData.processingModel,
        caseData.evidence,
        caseData.contradictions,
        readinessDefinitions,
        caseData.determinations,
      ),
    [caseData],
  );

  function rememberDialogTrigger() {
    dialogTriggerRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
  }

  function closeDialog(close: () => void) {
    const trigger = dialogTriggerRef.current;
    close();
    window.setTimeout(() => {
      if (trigger?.isConnected) trigger.focus();
    }, 0);
  }

  function openFindingById(findingId: string) {
    rememberDialogTrigger();
    setSelectedFinding(caseData.determinations.find(({ id }) => id === findingId) ?? null);
  }

  function showMutationError(error: unknown, fallback: string) {
    showToast(error instanceof Error ? error.message : fallback, { duration: 0 });
  }

  async function updateIntake(values: Record<string, string>) {
    try {
      const beforeVersion = caseData.processingModel.version;
      const updated = await controller.updateIntake(values);
      if (updated.processingModel.version > beforeVersion) {
        showToast(
          `Processing model v${updated.processingModel.version} saved. Dependent findings are stale until replayed.`,
        );
      } else {
        showToast("No material intake values changed.");
      }
    } catch (error) {
      showMutationError(error, "Could not save the intake changes.");
      throw error;
    }
  }

  async function connectCaseAgent() {
    const agent = availableAgents.data?.find(({ name }) => name === "dpia-investigation");
    if (!agent) {
      setBindingError(
        availableAgents.isLoading
          ? "The DPIA agent catalogue is still loading."
          : "The registered dpia-investigation agent is unavailable.",
      );
      return;
    }
    setBindingSession(true);
    setBindingError(null);
    try {
      const binding = await findOrCreateDpiaCaseSession(caseData.id, agent.id);
      await controller.bindSession(binding.sessionId);
      showToast(binding.created ? "Case agent session created." : "Existing case agent connected.");
    } catch (error) {
      setBindingError(error instanceof Error ? error.message : "Could not connect the case agent.");
    } finally {
      setBindingSession(false);
    }
  }

  async function answerQuestion(response: string, answeredBy: string) {
    if (!selectedQuestion) return;
    try {
      const updated = await controller.answerQuestion({
        questionId: selectedQuestion.id,
        response,
        answeredBy,
      });
      showToast(
        `Attributed answer recorded in processing model v${updated.processingModel.version}.`,
      );
    } catch (error) {
      showMutationError(error, "Could not record the stakeholder answer.");
      throw error;
    }
  }

  async function replaySnapshot() {
    try {
      const updated = await controller.replaySnapshot();
      setLiveRun({
        status: "idle",
        message: `Validated snapshot replayed against processing model v${updated.processingModel.version}. No live agent output was used.`,
      });
      showToast(
        `Validated investigation replayed against model v${updated.processingModel.version}.`,
      );
    } catch (error) {
      showMutationError(error, "Could not replay the validated investigation.");
    }
  }

  async function runLive() {
    liveAbortRef.current?.abort();
    const abortController = new AbortController();
    liveAbortRef.current = abortController;
    setLiveRun({ status: "running", message: "Starting the live investigation…" });
    try {
      const result = await runLiveInvestigation(
        caseData,
        (message) => setLiveRun({ status: "running", message }),
        abortController.signal,
      );
      const message =
        "The live root session completed. Its output remains separate from this validated snapshot until explicitly reviewed and imported.";
      setLiveRun({
        status: "completed",
        message,
        sessionId: result.sessionId,
      });
      await controller.recordLiveRun({
        status: "completed",
        message,
        sessionId: result.sessionId,
        updatedAt: new Date().toISOString(),
      });
    } catch (error) {
      if (abortController.signal.aborted) return;
      const message =
        error instanceof Error
          ? error.message
          : "The live investigation failed. The validated snapshot is unchanged.";
      setLiveRun({
        status: "failed",
        message,
      });
      try {
        await controller.recordLiveRun({
          status: "failed",
          message,
          updatedAt: new Date().toISOString(),
        });
      } catch (persistenceFailure) {
        showMutationError(persistenceFailure, "Could not save the live investigation status.");
      }
    }
  }

  async function resetCase() {
    try {
      await controller.reset();
      liveAbortRef.current?.abort();
      setLiveRun({
        status: "idle",
        message: "Returned to the reviewed Student Success Alert seed. No live output is applied.",
      });
      showToast("Case reset to the validated demo snapshot.");
    } catch (error) {
      showMutationError(error, "Could not reset the case.");
    }
  }

  function makeDecisionPack(): DecisionPack | null {
    try {
      const pack = synthesizeDecisionPack(caseData, new Date().toISOString());
      setDecisionPackError(null);
      setPrintPack(pack);
      return pack;
    } catch (error) {
      rememberDialogTrigger();
      setDecisionPackError(
        error instanceof SynthesisRefusal
          ? error.message
          : "The case could not be validated for export.",
      );
      return null;
    }
  }

  function printDecisionPack() {
    if (!makeDecisionPack()) return;
    window.setTimeout(() => window.print(), 0);
  }

  function downloadDecisionPack() {
    const pack = makeDecisionPack();
    if (!pack) return;
    const blob = new Blob([decisionPackToMarkdown(pack)], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `student-success-alert-dpia-decision-pack-v${pack.processingModelVersion}.md`;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  return (
    <>
      <PageScroll
        maxWidthClassName="max-w-[1480px]"
        contentClassName="px-4 md:px-7"
        className="dpia-case-surface"
        data-testid="dpia-case"
      >
        <header className="dpia-no-print mb-5 border-b border-border pb-5">
          <nav
            aria-label="Breadcrumb"
            className="mb-3 flex items-center gap-1 text-sm text-muted-foreground"
          >
            <Link to="/dpia" className="hover:text-foreground">
              DPIA assessments
            </Link>
            <ChevronRightIcon className="size-3.5" />
            <span aria-current="page">Student Success Alert</span>
          </nav>
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0 max-w-4xl">
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <Badge
                  variant="outline"
                  className="border-status-yellow/30 bg-status-yellow/10 text-status-yellow"
                >
                  Synthetic data
                </Badge>
                <Badge variant="outline">{caseData.snapshotLabel}</Badge>
                <Badge variant="outline">UK GDPR</Badge>
                <Badge variant="outline">
                  {liveRun.status === "running" || liveRun.status === "completed"
                    ? "Case agent live"
                    : caseData.sessionId
                      ? "Case agent bound"
                      : "Case agent unbound"}
                </Badge>
              </div>
              <h1 className="text-2xl font-semibold tracking-normal">{caseData.title}</h1>
              <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-muted-foreground">
                <span>{caseData.owner}</span>
                <span>Processing model v{caseData.processingModel.version}</span>
                <span>Policy {caseData.policyPack.version}</span>
                <span>{caseData.stage}</span>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2" aria-label="Case actions">
              {!caseData.sessionId && (
                <Button
                  type="button"
                  variant="outline"
                  disabled={bindingSession}
                  onClick={() => void connectCaseAgent()}
                >
                  <LinkIcon data-icon="inline-start" />
                  {bindingSession ? "Connecting…" : "Connect case agent"}
                </Button>
              )}
              {caseData.sessionId && (
                <Button asChild type="button" variant="outline">
                  <Link to={`/c/${caseData.sessionId}`}>
                    <ExternalLinkIcon data-icon="inline-start" />
                    Open full session
                  </Link>
                </Button>
              )}
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  rememberDialogTrigger();
                  setActivityOpen(true);
                }}
              >
                <BotIcon data-icon="inline-start" />
                Agent activity
              </Button>
              <Button
                type="button"
                variant="outline"
                size="icon"
                onClick={downloadDecisionPack}
                aria-label="Download decision pack"
                title="Download decision pack"
              >
                <DownloadIcon />
              </Button>
              <Button
                type="button"
                variant="outline"
                size="icon"
                onClick={printDecisionPack}
                aria-label="Print decision pack"
                title="Print decision pack"
              >
                <PrinterIcon />
              </Button>
            </div>
          </div>
          {bindingError && (
            <p role="alert" className="mt-3 text-sm text-status-red">
              {bindingError}
            </p>
          )}
        </header>

        {controller.recoveredInvalidState && (
          <div className="dpia-no-print mb-4 flex items-start gap-3 rounded-lg border border-border bg-muted/30 px-4 py-3">
            <AlertTriangleIcon className="mt-0.5 size-4 shrink-0 text-status-yellow" />
            <p className="text-ui">
              Invalid browser state was removed and the reviewed synthetic snapshot was restored.
            </p>
          </div>
        )}

        <section
          className="dpia-no-print mb-5 overflow-hidden rounded-lg border border-border bg-card"
          aria-labelledby="case-readiness-heading"
        >
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-4 py-3">
            <div className="flex items-center gap-2">
              <FileCheck2Icon className="size-4 text-status-blue" />
              <h2 id="case-readiness-heading" className="font-semibold">
                {readiness.answerable}/{readiness.total} determination areas answerable
              </h2>
            </div>
            <div className="flex items-center gap-2">
              <Badge
                variant="outline"
                className="border-status-red/25 bg-status-red/10 text-status-red"
              >
                Full DPIA likely
              </Badge>
              <span className="hidden text-sm text-muted-foreground sm:inline">
                Officer verification required
              </span>
            </div>
          </div>
          <div className="grid sm:grid-cols-2 lg:grid-cols-4 2xl:grid-cols-8">
            {readiness.dimensions.map((dimension) => {
              const finding = caseData.determinations.find(
                ({ dimensionId }) => dimensionId === dimension.id,
              );
              return (
                <button
                  key={dimension.id}
                  type="button"
                  onClick={() => {
                    if (!finding) return;
                    rememberDialogTrigger();
                    setSelectedFinding(finding);
                  }}
                  className="min-h-[82px] border-b border-r border-border px-3 py-2.5 text-left transition-colors hover:bg-muted/50"
                >
                  <span className="line-clamp-2 block min-h-9 text-sm font-medium leading-snug">
                    {dimension.label}
                  </span>
                  <DpiaStatus status={dimension.status} className="mt-2" />
                </button>
              );
            })}
          </div>
        </section>

        <div className="dpia-no-print mb-5 flex items-start gap-3 rounded-lg border border-border bg-muted/30 px-4 py-3 text-ui">
          <AlertTriangleIcon className="mt-0.5 size-4 shrink-0 text-status-yellow" />
          <p>
            Demo environment only. Do not enter or upload real student, disability, wellbeing,
            hardship, attainment, or intervention data.
          </p>
        </div>

        <Tabs
          value={activeTab}
          onValueChange={(value) => setActiveTab(value as CaseTab)}
          className="dpia-no-print gap-5"
        >
          <div className="overflow-x-auto border-b border-border">
            <TabsList
              variant="line"
              className="min-w-max px-1 pb-1"
              aria-label="DPIA case sections"
            >
              <TabsTrigger value="overview">Overview</TabsTrigger>
              <TabsTrigger value="map">Processing map</TabsTrigger>
              <TabsTrigger value="evidence">Evidence & questions</TabsTrigger>
              <TabsTrigger value="screening">Screening</TabsTrigger>
              <TabsTrigger value="full">Full DPIA</TabsTrigger>
              <TabsTrigger value="audit">Audit</TabsTrigger>
            </TabsList>
          </div>

          <TabsContent value="overview">
            <DpiaOverview
              caseData={caseData}
              readiness={readiness}
              onEditIntake={() => {
                rememberDialogTrigger();
                setIntakeOpen(true);
              }}
              onFinding={openFindingById}
              onAgentActivity={() => {
                rememberDialogTrigger();
                setActivityOpen(true);
              }}
              onNavigate={(tab) => setActiveTab(tab as CaseTab)}
            />
          </TabsContent>
          <TabsContent value="map">
            <DpiaProcessingMap caseData={caseData} />
          </TabsContent>
          <TabsContent value="evidence">
            <DpiaEvidenceQuestions
              caseData={caseData}
              onEvidence={(evidence) => {
                rememberDialogTrigger();
                setSelectedEvidence(evidence);
              }}
              onQuestion={(question) => {
                rememberDialogTrigger();
                setSelectedQuestion(question);
              }}
            />
          </TabsContent>
          <TabsContent value="screening">
            <DpiaScreening
              caseData={caseData}
              onFinding={(finding) => {
                rememberDialogTrigger();
                setSelectedFinding(finding);
              }}
              onOfficerAction={(action) => {
                rememberDialogTrigger();
                setOfficerAction(action);
              }}
              onContinue={() => setActiveTab("full")}
              onAgentActivity={() => {
                rememberDialogTrigger();
                setActivityOpen(true);
              }}
            />
          </TabsContent>
          <TabsContent value="full">
            <DpiaFullAssessment caseData={caseData} />
          </TabsContent>
          <TabsContent value="audit">
            <DpiaAudit caseData={caseData} />
          </TabsContent>
        </Tabs>

        <StakeholderOutreachPanel
          caseData={caseData}
          onAcceptAnswer={async (input) => {
            try {
              const updated = await controller.answerQuestion(input);
              showToast(
                `Attributed answer recorded in processing model v${updated.processingModel.version}.`,
              );
            } catch (error) {
              showMutationError(error, "Could not record the stakeholder answer.");
            }
          }}
        />

        <DpiaCaseChat
          caseData={caseData}
          sessionId={caseData.sessionId}
          connecting={bindingSession}
          bindingError={bindingError}
          onConnect={() => void connectCaseAgent()}
          onStageProposal={async (proposal, source) => {
            try {
              await controller.stageCorrection(proposal, source);
              showToast(
                source === "agent"
                  ? "Agent correction proposal is ready for officer review."
                  : "Manual correction proposal drafted.",
              );
            } catch (error) {
              showMutationError(error, "Could not save the correction proposal.");
            }
          }}
          onEditProposal={async (proposalId, proposal) => {
            try {
              await controller.editCorrection(proposalId, proposal);
              showToast("Correction proposal updated.");
            } catch (error) {
              showMutationError(error, "Could not update the correction proposal.");
            }
          }}
          onApplyProposal={async (proposal) => {
            try {
              const updated = await controller.applyCorrection(proposal);
              showToast(
                `Correction applied in processing model v${updated.processingModel.version}. Dependent findings are stale.`,
              );
            } catch (error) {
              showMutationError(error, "Could not apply the correction proposal.");
            }
          }}
          onRejectProposal={async (proposalId) => {
            try {
              await controller.rejectCorrection(proposalId);
              showToast("Correction proposal rejected. The case was not changed.");
            } catch (error) {
              showMutationError(error, "Could not reject the correction proposal.");
            }
          }}
        />
      </PageScroll>

      <DpiaPrintPack pack={printPack} />
      <DpiaIntakeDialog
        open={intakeOpen}
        onOpenChange={(open) => {
          if (!open) closeDialog(() => setIntakeOpen(false));
        }}
        caseData={caseData}
        onSave={updateIntake}
        busy={controller.isSaving}
      />
      <EvidenceDialog
        evidence={selectedEvidence}
        caseData={caseData}
        onOpenChange={(open) => {
          if (!open) closeDialog(() => setSelectedEvidence(null));
        }}
      />
      <FindingProvenanceDialog
        finding={selectedFinding}
        caseData={caseData}
        onEvidence={(evidence) => {
          setSelectedFinding(null);
          setSelectedEvidence(evidence);
        }}
        onOpenChange={(open) => {
          if (!open) closeDialog(() => setSelectedFinding(null));
        }}
      />
      <QuestionAnswerDialog
        question={selectedQuestion}
        onOpenChange={(open) => {
          if (!open) closeDialog(() => setSelectedQuestion(null));
        }}
        onSubmit={answerQuestion}
        busy={controller.isSaving}
      />
      <OfficerDecisionDialog
        action={officerAction}
        caseData={caseData}
        onOpenChange={(open) => {
          if (!open) closeDialog(() => setOfficerAction(null));
        }}
        onSubmit={async (decision) => {
          try {
            const updated = await controller.decide(decision);
            showToast(
              `Privacy Officer decision recorded: ${updated.officerDecision?.outcome.replaceAll("-", " ")}.`,
            );
          } catch (error) {
            showMutationError(error, "Could not record the Privacy Officer decision.");
            throw error;
          }
        }}
        busy={controller.isSaving}
      />
      <AgentActivityDialog
        open={activityOpen}
        onOpenChange={(open) => {
          if (!open) closeDialog(() => setActivityOpen(false));
        }}
        activity={caseData.agentActivity}
        liveRun={liveRun}
        onRunLive={() => void runLive()}
        onReplaySnapshot={() => void replaySnapshot()}
        onReset={() => void resetCase()}
      />
      <DecisionPackUnavailableDialog
        message={decisionPackError}
        onOpenChange={(open) => {
          if (!open) closeDialog(() => setDecisionPackError(null));
        }}
      />
    </>
  );
}

function UnknownDpiaCase() {
  return (
    <PageScroll maxWidthClassName="max-w-xl" contentClassName="px-5 text-center">
      <div className="flex flex-col items-center py-16">
        <span className="mb-4 flex size-12 items-center justify-center rounded-lg bg-muted text-muted-foreground">
          <FileTextIcon className="size-6" />
        </span>
        <h1 className="text-xl font-semibold">DPIA case not found</h1>
        <p className="mt-2 text-ui text-muted-foreground">
          This demonstration includes the synthetic Student Success Alert case only.
        </p>
        <Button asChild className="mt-5">
          <Link to="/dpia">
            <ShieldCheckIcon data-icon="inline-start" />
            Return to assessments
          </Link>
        </Button>
      </div>
    </PageScroll>
  );
}
