import { type FormEvent, useEffect, useId, useMemo, useState } from "react";
import {
  ArrowRightIcon,
  CheckIcon,
  FilePenLineIcon,
  MessageSquareMoreIcon,
  PencilIcon,
  XIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import {
  buildManualCorrectionProposal,
  staleFindingIdsForFacts,
  validateCorrectionProposal,
} from "@/lib/dpia/correctionProposal";
import type {
  CorrectionProposal,
  CorrectionProposalRecord,
  DpiaCaseSnapshot,
} from "@/lib/dpia/types";

export function CorrectionProposalCard({
  caseData,
  record,
  onApply,
  onEdit,
  onReject,
  onFollowUp,
}: {
  caseData: DpiaCaseSnapshot;
  record: CorrectionProposalRecord;
  onApply: () => void;
  onEdit: () => void;
  onReject: () => void;
  onFollowUp: () => void;
}) {
  const { proposal } = record;
  return (
    <article
      className="space-y-3 rounded-lg border border-border bg-card p-4"
      data-testid="correction-proposal-card"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="font-semibold">Correction proposal</h3>
          <p className="mt-1 text-sm text-muted-foreground">{proposal.instruction}</p>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant="outline">
            {record.source === "agent" ? "Agent draft" : "Manual draft"}
          </Badge>
          <Badge variant="outline">Officer approval required</Badge>
        </div>
      </div>

      <div className="space-y-2">
        {proposal.target_facts.map((target) => {
          const fact = caseData.processingModel.facts.find(({ id }) => id === target.fact_id);
          return (
            <div
              key={target.fact_id}
              className="grid gap-2 border-t border-border pt-3 md:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)]"
            >
              <div className="min-w-0">
                <p className="text-xs font-medium text-muted-foreground">
                  Current {fact?.label ?? target.fact_id}
                </p>
                <p className="mt-1 whitespace-pre-wrap text-sm">
                  {target.current_value || "Not recorded"}
                </p>
              </div>
              <ArrowRightIcon className="mt-5 hidden size-4 text-muted-foreground md:block" />
              <div className="min-w-0">
                <p className="text-xs font-medium text-muted-foreground">Proposed</p>
                <p className="mt-1 whitespace-pre-wrap text-sm font-medium">
                  {target.proposed_value}
                </p>
              </div>
            </div>
          );
        })}
      </div>

      <dl className="grid gap-3 border-t border-border pt-3 text-sm sm:grid-cols-3">
        <div>
          <dt className="text-muted-foreground">Evidence</dt>
          <dd className="mt-1">
            {proposal.new_evidence_refs.map(({ evidence_id }) => evidence_id).join(", ") ||
              "No new reference"}
          </dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Version impact</dt>
          <dd className="mt-1">
            v{proposal.expected_version_bump.from} to v{proposal.expected_version_bump.to}
          </dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Reassessment</dt>
          <dd className="mt-1">{proposal.role_to_reassess.replaceAll("_", " ")}</dd>
        </div>
      </dl>
      <p className="text-sm text-muted-foreground">
        {proposal.stale_finding_ids.length} finding
        {proposal.stale_finding_ids.length === 1 ? "" : "s"} will become stale. {proposal.rationale}
      </p>

      <div className="flex flex-wrap justify-end gap-2">
        <Button type="button" variant="ghost" onClick={onFollowUp}>
          <MessageSquareMoreIcon data-icon="inline-start" />
          Follow-up
        </Button>
        <Button type="button" variant="outline" onClick={onReject}>
          <XIcon data-icon="inline-start" />
          Reject
        </Button>
        <Button type="button" variant="outline" onClick={onEdit}>
          <PencilIcon data-icon="inline-start" />
          Edit
        </Button>
        <Button type="button" onClick={onApply}>
          <CheckIcon data-icon="inline-start" />
          Apply
        </Button>
      </div>
    </article>
  );
}

export function ManualCorrectionDialog({
  open,
  onOpenChange,
  caseData,
  initialProposal,
  onSubmit,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  caseData: DpiaCaseSnapshot;
  initialProposal?: CorrectionProposal;
  onSubmit: (proposal: CorrectionProposal) => void;
}) {
  const formId = useId();
  const eligibleFacts = useMemo(
    () =>
      caseData.processingModel.facts.filter(
        (fact) => fact.material && staleFindingIdsForFacts(caseData, new Set([fact.id])).length > 0,
      ),
    [caseData],
  );
  const defaultFact =
    eligibleFacts.find(({ id }) => id === "hosting") ??
    eligibleFacts.find(({ status }) => status === "missing") ??
    eligibleFacts[0];
  const defaultEvidence =
    caseData.evidence.find(({ id }) => id === "ev-response-security") ?? caseData.evidence[0];
  const [factId, setFactId] = useState(defaultFact?.id ?? "");
  const [targetValues, setTargetValues] = useState<Record<string, string>>({});
  const [evidenceId, setEvidenceId] = useState(defaultEvidence?.id ?? "");
  const [instruction, setInstruction] = useState("");
  const [rationale, setRationale] = useState("");
  const [role, setRole] = useState<CorrectionProposal["role_to_reassess"]>("privacy_assessor");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    const firstFact = initialProposal?.target_facts[0]?.fact_id ?? defaultFact?.id ?? "";
    setFactId(firstFact);
    setTargetValues(
      Object.fromEntries(
        initialProposal?.target_facts.map(({ fact_id, proposed_value }) => [
          fact_id,
          proposed_value,
        ]) ?? [[firstFact, ""]],
      ),
    );
    setEvidenceId(initialProposal?.new_evidence_refs[0]?.evidence_id ?? defaultEvidence?.id ?? "");
    const fact = caseData.processingModel.facts.find(({ id }) => id === firstFact);
    setInstruction(
      initialProposal?.instruction ??
        `Correct ${fact?.label ?? "the selected fact"} using reviewed synthetic evidence.`,
    );
    setRationale(initialProposal?.rationale ?? "");
    setRole(initialProposal?.role_to_reassess ?? "privacy_assessor");
    setError(null);
  }, [caseData.processingModel.facts, defaultEvidence?.id, defaultFact?.id, initialProposal, open]);

  function submit(event: FormEvent) {
    event.preventDefault();
    try {
      const proposal = initialProposal
        ? validateCorrectionProposal(caseData, {
            ...initialProposal,
            instruction,
            rationale,
            role_to_reassess: role,
            target_facts: initialProposal.target_facts.map((target) => ({
              ...target,
              proposed_value: targetValues[target.fact_id] ?? target.proposed_value,
            })),
          })
        : buildManualCorrectionProposal(caseData, {
            factId,
            proposedValue: targetValues[factId] ?? "",
            evidenceId,
            instruction,
            rationale,
            roleToReassess: role,
          });
      onSubmit(proposal);
      onOpenChange(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The correction proposal is invalid.");
    }
  }

  const targets = initialProposal?.target_facts ?? (factId ? [{ fact_id: factId }] : []);
  const canSubmit =
    targets.length > 0 &&
    targets.every(({ fact_id }) => (targetValues[fact_id] ?? "").trim().length > 0) &&
    instruction.trim().length > 0 &&
    rationale.trim().length >= 10 &&
    (initialProposal !== undefined || evidenceId.length > 0);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[88vh] max-w-2xl grid-rows-[auto_minmax(0,1fr)_auto] p-0">
        <DialogHeader className="border-b border-border px-6 pt-6 pb-4">
          <DialogTitle>
            {initialProposal ? "Edit correction proposal" : "Draft correction manually"}
          </DialogTitle>
          <DialogDescription>
            This creates a proposal only. The case changes after a Privacy Officer selects Apply.
          </DialogDescription>
        </DialogHeader>
        <form id={formId} onSubmit={submit} className="min-h-0 space-y-4 overflow-y-auto px-6 py-5">
          {!initialProposal && (
            <label className="block text-sm font-medium">
              Target fact
              <Select
                value={factId}
                onValueChange={(value) => {
                  setFactId(value);
                  setTargetValues((current) => ({ ...current, [value]: current[value] ?? "" }));
                }}
              >
                <SelectTrigger className="mt-1.5 w-full" aria-label="Target fact">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {eligibleFacts.map((fact) => (
                    <SelectItem key={fact.id} value={fact.id}>
                      {fact.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </label>
          )}

          {targets.map(({ fact_id }) => {
            const fact = caseData.processingModel.facts.find(({ id }) => id === fact_id);
            return (
              <div key={fact_id} className="space-y-2 rounded-lg border border-border p-3">
                <p className="text-sm font-medium">{fact?.label ?? fact_id}</p>
                <p className="text-sm text-muted-foreground">
                  Current: {fact?.value || "Not recorded"}
                </p>
                <label className="block text-sm font-medium">
                  Proposed value
                  <Textarea
                    aria-label={`Proposed value for ${fact?.label ?? fact_id}`}
                    value={targetValues[fact_id] ?? ""}
                    onChange={(event) =>
                      setTargetValues((current) => ({ ...current, [fact_id]: event.target.value }))
                    }
                    className="mt-1.5 min-h-20"
                  />
                </label>
              </div>
            );
          })}

          {!initialProposal && (
            <label className="block text-sm font-medium">
              Supporting evidence
              <Select value={evidenceId} onValueChange={setEvidenceId}>
                <SelectTrigger className="mt-1.5 w-full" aria-label="Supporting evidence">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {caseData.evidence.map((evidence) => (
                    <SelectItem key={evidence.id} value={evidence.id}>
                      {evidence.title}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </label>
          )}

          <label className="block text-sm font-medium">
            Officer instruction
            <Input
              aria-label="Officer instruction"
              value={instruction}
              onChange={(event) => setInstruction(event.target.value)}
              className="mt-1.5"
            />
          </label>
          <label className="block text-sm font-medium">
            Rationale
            <Textarea
              aria-label="Correction rationale"
              value={rationale}
              onChange={(event) => setRationale(event.target.value)}
              className="mt-1.5 min-h-20"
            />
          </label>
          <label className="block text-sm font-medium">
            Role to reassess
            <Select
              value={role}
              onValueChange={(value) => setRole(value as CorrectionProposal["role_to_reassess"])}
            >
              <SelectTrigger className="mt-1.5 w-full" aria-label="Role to reassess">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="process_investigator">Process Investigator</SelectItem>
                <SelectItem value="privacy_assessor">Privacy Assessor</SelectItem>
                <SelectItem value="independent_verifier">Independent Verifier</SelectItem>
              </SelectContent>
            </Select>
          </label>
          {error && (
            <p role="alert" className="text-sm text-status-red">
              {error}
            </p>
          )}
        </form>
        <DialogFooter className="m-0 rounded-none border-t px-6 py-4">
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button type="submit" form={formId} disabled={!canSubmit}>
            <FilePenLineIcon data-icon="inline-start" />
            {initialProposal ? "Save proposal" : "Create proposal"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
