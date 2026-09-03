import { type FormEvent, useState } from "react";
import { AlertTriangleIcon, ArrowLeftIcon, FilePlus2Icon, ShieldCheckIcon } from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { createStudentSuccessAlertSeed } from "@/lib/dpia/seed";
import { saveDpiaCase, updateDpiaIntake } from "@/lib/dpia/dpiaApi";
import { Link, useNavigate } from "@/lib/routing";

export function DpiaNewAssessmentPage() {
  const navigate = useNavigate();
  const [title, setTitle] = useState("Student Success Alert — AI Early-Warning and Intervention");
  const [owner, setOwner] = useState("Student Services Transformation");
  const [purpose, setPurpose] = useState(
    "Identify possible student disengagement early and recommend proportionate support outreach.",
  );
  const [dataSubjects, setDataSubjects] = useState(
    "Enrolled undergraduate and postgraduate students, including students receiving support.",
  );

  function submit(event: FormEvent) {
    event.preventDefault();
    const seed = createStudentSuccessAlertSeed();
    const now = new Date().toISOString();
    const updated = updateDpiaIntake(
      { ...seed, title: title.trim(), owner: owner.trim() },
      { purpose: purpose.trim(), "data-subjects": dataSubjects.trim() },
      now,
      "Alex Morgan",
    );
    saveDpiaCase(updated);
    navigate("/dpia/cases/student-success-alert");
  }

  return (
    <PageScroll
      maxWidthClassName="max-w-4xl"
      contentClassName="px-5 md:px-8"
      data-testid="dpia-new-assessment"
    >
      <header className="mb-6 border-b border-border pb-5">
        <Button asChild variant="ghost" size="sm" className="-ml-2 mb-3 text-muted-foreground">
          <Link to="/dpia">
            <ArrowLeftIcon data-icon="inline-start" />
            Assessments
          </Link>
        </Button>
        <div className="flex items-start gap-3">
          <div className="flex size-10 shrink-0 items-center justify-center rounded-md bg-status-blue/10 text-status-blue">
            <FilePlus2Icon className="size-5" />
          </div>
          <div>
            <h1 className="text-2xl font-semibold tracking-normal">New DPIA assessment</h1>
            <p className="mt-1 text-ui text-muted-foreground">
              Start from the reviewed university scenario and refine the processing facts in the
              case cockpit.
            </p>
          </div>
        </div>
      </header>

      <div className="mb-6 flex items-start gap-3 rounded-lg border border-border bg-muted/30 px-4 py-3 text-ui">
        <AlertTriangleIcon className="mt-0.5 size-4 shrink-0 text-status-yellow" />
        <div>
          <p className="font-medium">Synthetic data only</p>
          <p className="text-muted-foreground">
            This creates a browser-local demonstration record. Do not enter names, identifiers, case
            notes, health information, attainment, or live university data.
          </p>
        </div>
      </div>

      <form onSubmit={submit} className="space-y-7">
        <section aria-labelledby="assessment-identity-heading">
          <div className="mb-3 flex items-center gap-2">
            <ShieldCheckIcon className="size-4 text-muted-foreground" />
            <h2 id="assessment-identity-heading" className="font-semibold">
              Assessment identity
            </h2>
          </div>
          <div className="grid gap-4 rounded-lg border border-border bg-card p-4 md:grid-cols-2">
            <label htmlFor="dpia-assessment-title" className="md:col-span-2">
              <span className="mb-1.5 block text-sm font-medium">Processing activity</span>
              <Input
                id="dpia-assessment-title"
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                required
              />
            </label>
            <label htmlFor="dpia-assessment-owner">
              <span className="mb-1.5 block text-sm font-medium">Business owner</span>
              <Input
                id="dpia-assessment-owner"
                value={owner}
                onChange={(event) => setOwner(event.target.value)}
                required
              />
            </label>
            <label htmlFor="dpia-assessment-jurisdiction">
              <span className="mb-1.5 block text-sm font-medium">Jurisdiction</span>
              <Input id="dpia-assessment-jurisdiction" value="United Kingdom" disabled />
            </label>
          </div>
        </section>

        <section aria-labelledby="initial-scope-heading">
          <h2 id="initial-scope-heading" className="mb-3 font-semibold">
            Initial scope
          </h2>
          <div className="grid gap-4 rounded-lg border border-border bg-card p-4">
            <label htmlFor="dpia-assessment-purpose">
              <span className="mb-1.5 block text-sm font-medium">
                Purpose and intended outcomes
              </span>
              <Textarea
                id="dpia-assessment-purpose"
                value={purpose}
                onChange={(event) => setPurpose(event.target.value)}
                required
              />
            </label>
            <label htmlFor="dpia-assessment-subjects">
              <span className="mb-1.5 block text-sm font-medium">Data subjects</span>
              <Textarea
                id="dpia-assessment-subjects"
                value={dataSubjects}
                onChange={(event) => setDataSubjects(event.target.value)}
                required
              />
            </label>
          </div>
        </section>

        <div className="flex items-center justify-end gap-2 border-t border-border pt-5">
          <Button asChild variant="outline">
            <Link to="/dpia">Cancel</Link>
          </Button>
          <Button type="submit" componentId="dpia.create_synthetic_assessment">
            Create synthetic assessment
          </Button>
        </div>
      </form>
    </PageScroll>
  );
}
