import { ArrowUpRightIcon, FileTextIcon, MessageSquareTextIcon, UserRoundIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { DpiaCaseSnapshot, EvidenceItem, StakeholderQuestion } from "@/lib/dpia/types";
import { DpiaStatus } from "./DpiaStatus";

export function DpiaEvidenceQuestions({
  caseData,
  onEvidence,
  onQuestion,
}: {
  caseData: DpiaCaseSnapshot;
  onEvidence: (evidence: EvidenceItem) => void;
  onQuestion: (question: StakeholderQuestion) => void;
}) {
  const openQuestions = caseData.questions.filter(({ status }) => status !== "answered").length;

  return (
    <Tabs defaultValue="evidence" className="gap-5">
      <div className="flex flex-wrap items-end justify-between gap-3 border-b border-border pb-3">
        <div>
          <h2 className="font-semibold">Evidence and targeted inquiry</h2>
          <p className="mt-0.5 text-sm text-muted-foreground">
            Every material finding links to a source excerpt or remains explicitly unsupported.
          </p>
        </div>
        <TabsList variant="pill" aria-label="Evidence workspace view">
          <TabsTrigger value="evidence">
            <FileTextIcon />
            Evidence ledger
            <Badge variant="secondary">{caseData.evidence.length}</Badge>
          </TabsTrigger>
          <TabsTrigger value="questions">
            <MessageSquareTextIcon />
            Questions
            <Badge variant="secondary">{openQuestions} open</Badge>
          </TabsTrigger>
        </TabsList>
      </div>

      <TabsContent value="evidence">
        <div className="grid gap-3 lg:grid-cols-2 xl:hidden">
          {caseData.evidence.map((evidence) => (
            <article key={evidence.id} className="rounded-lg border border-border bg-card p-4">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <h3 className="font-medium leading-snug">{evidence.title}</h3>
                  <p className="mt-0.5 text-sm text-muted-foreground">{evidence.type}</p>
                </div>
                <DpiaStatus status={evidence.status} />
              </div>
              <dl className="mt-3 grid grid-cols-2 gap-3 border-y border-border py-3 text-sm">
                <div>
                  <dt className="text-muted-foreground">Source</dt>
                  <dd className="mt-0.5 font-medium">{evidence.source}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">Owner</dt>
                  <dd className="mt-0.5 font-medium">{evidence.owner}</dd>
                </div>
                <div className="col-span-2">
                  <dt className="text-muted-foreground">Collected</dt>
                  <dd className="mt-0.5">{formatDate(evidence.collectedAt)}</dd>
                </div>
              </dl>
              <blockquote className="mt-3 text-ui leading-relaxed text-muted-foreground">
                “{evidence.excerpt}”
              </blockquote>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="mt-4 w-full"
                onClick={() => onEvidence(evidence)}
              >
                Inspect provenance
                <ArrowUpRightIcon data-icon="inline-end" />
              </Button>
            </article>
          ))}
        </div>
        <div className="hidden overflow-x-auto rounded-lg border border-border bg-card xl:block">
          <table className="w-full min-w-[980px] border-collapse text-left text-ui">
            <thead className="bg-muted/40 text-sm text-muted-foreground">
              <tr>
                <th className="px-4 py-2.5 font-medium">Evidence</th>
                <th className="px-4 py-2.5 font-medium">Source / owner</th>
                <th className="px-4 py-2.5 font-medium">Collected</th>
                <th className="px-4 py-2.5 font-medium">Relevant excerpt</th>
                <th className="px-4 py-2.5 font-medium">Status</th>
                <th className="w-10 px-2 py-2.5">
                  <span className="sr-only">Open</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {caseData.evidence.map((evidence) => (
                <tr
                  key={evidence.id}
                  className="border-t border-border transition-colors hover:bg-muted/40"
                >
                  <td className="max-w-[230px] px-4 py-3 align-top">
                    <button
                      type="button"
                      onClick={() => onEvidence(evidence)}
                      className="block w-full rounded-sm text-left outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      <span className="block font-medium">{evidence.title}</span>
                      <span className="mt-0.5 block text-sm text-muted-foreground">
                        {evidence.type}
                      </span>
                    </button>
                  </td>
                  <td className="max-w-[200px] px-4 py-3 align-top">
                    <span className="block">{evidence.source}</span>
                    <span className="mt-0.5 block text-sm text-muted-foreground">
                      {evidence.owner}
                    </span>
                  </td>
                  <td className="whitespace-nowrap px-4 py-3 align-top text-sm text-muted-foreground">
                    {formatDate(evidence.collectedAt)}
                  </td>
                  <td className="max-w-[360px] px-4 py-3 align-top text-muted-foreground">
                    <span className="line-clamp-2">“{evidence.excerpt}”</span>
                  </td>
                  <td className="px-4 py-3 align-top">
                    <DpiaStatus status={evidence.status} />
                  </td>
                  <td className="px-2 py-3 align-top">
                    <ArrowUpRightIcon className="size-4 text-muted-foreground" />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </TabsContent>

      <TabsContent value="questions">
        <div className="grid gap-3">
          {caseData.questions.map((question) => (
            <article key={question.id} className="rounded-lg border border-border bg-card p-4">
              <div className="grid gap-3 md:grid-cols-[minmax(160px,0.35fr)_minmax(0,1fr)_auto] md:items-start">
                <div className="flex items-start gap-2">
                  <span className="flex size-8 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground">
                    <UserRoundIcon className="size-4" />
                  </span>
                  <div>
                    <p className="font-medium">{question.stakeholder}</p>
                    <DpiaStatus status={question.status} className="mt-1" />
                  </div>
                </div>
                <div>
                  <h3 className="font-medium leading-relaxed">{question.text}</h3>
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {question.blockedDimensionIds.map((dimensionId) => (
                      <Badge key={dimensionId} variant="secondary">
                        {dimensionId.replaceAll("-", " ")}
                      </Badge>
                    ))}
                  </div>
                  {question.response && (
                    <blockquote className="mt-3 rounded-lg border border-border bg-muted/30 px-3 py-2 text-ui text-muted-foreground">
                      “{question.response}”
                      <footer className="mt-1 text-sm font-medium text-foreground">
                        {question.answeredBy} ·{" "}
                        {question.answeredAt ? formatDate(question.answeredAt) : ""}
                      </footer>
                    </blockquote>
                  )}
                </div>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="w-full md:w-auto"
                  onClick={() => onQuestion(question)}
                >
                  {question.status === "answered" ? "Edit answer" : "Record answer"}
                </Button>
              </div>
            </article>
          ))}
        </div>
      </TabsContent>
    </Tabs>
  );
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("en-GB", { dateStyle: "medium" }).format(new Date(value));
}
