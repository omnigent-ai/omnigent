import type { DecisionPack } from "@/lib/dpia/types";

export function DpiaPrintPack({ pack }: { pack: DecisionPack | null }) {
  if (!pack) return null;
  return (
    <article className="dpia-print-pack" aria-label="Printable DPIA decision pack">
      <header>
        <p className="dpia-print-kicker">DPIA Investigation Desk · Synthetic demonstration</p>
        <h1>Student Success Alert — DPIA Decision Pack</h1>
        <p>
          Processing model v{pack.processingModelVersion} · Policy pack {pack.policyPackVersion} ·
          Generated {formatDate(pack.generatedAt)}
        </p>
        <div className="dpia-print-warning">
          All documents, organisations, people, and responses in this pack are synthetic. This
          environment is not approved for real student data.
        </div>
      </header>

      <section>
        <h2>Officer decision</h2>
        <dl className="dpia-print-grid">
          <PrintField label="Outcome" value={pack.officerDecision.outcome.replaceAll("-", " ")} />
          <PrintField label="Action" value={pack.officerDecision.action} />
          <PrintField label="Officer" value={pack.officerDecision.officer} />
          <PrintField label="Decided" value={formatDate(pack.officerDecision.decidedAt)} />
        </dl>
        <p>{pack.officerDecision.rationale}</p>
      </section>

      <section>
        <h2>Processing map</h2>
        <div className="dpia-print-lifecycle">
          {pack.lifecycle.map((node, index) => (
            <div key={node.stage}>
              <strong>
                {index + 1}. {titleCase(node.stage)}
              </strong>
              <span>{node.purpose}</span>
              <small>{node.location}</small>
            </div>
          ))}
        </div>
      </section>

      <section className="dpia-print-break">
        <h2>Screening determinations</h2>
        {pack.determinations.map((finding) => (
          <div key={finding.id} className="dpia-print-finding">
            <h3>{finding.question}</h3>
            <p>
              <strong>{finding.outcome.replace("-", " ")}</strong> ·{" "}
              {finding.status.replaceAll("-", " ")} · {finding.reviewer}
            </p>
            <p>{finding.reasoning}</p>
            <small>
              Evidence:{" "}
              {finding.evidenceReferences.map((reference) => reference.evidenceId).join(", ") ||
                "unsupported"}{" "}
              · Policy:{" "}
              {finding.policyReferences.map((reference) => reference.label).join(", ") ||
                "unsupported"}
            </small>
            {finding.dissent && (
              <p>
                <strong>Dissent:</strong> {finding.dissent}
              </p>
            )}
          </div>
        ))}
      </section>

      <section className="dpia-print-break">
        <h2>Evidence ledger</h2>
        <table>
          <thead>
            <tr>
              <th>Evidence</th>
              <th>Source / owner</th>
              <th>Excerpt</th>
            </tr>
          </thead>
          <tbody>
            {pack.evidence.map((item) => (
              <tr key={item.id}>
                <td>
                  <strong>{item.title}</strong>
                  <br />
                  <small>{item.id}</small>
                </td>
                <td>
                  {item.source}
                  <br />
                  <small>{item.owner}</small>
                </td>
                <td>“{item.excerpt}”</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="dpia-print-break">
        <h2>Risk register</h2>
        <table>
          <thead>
            <tr>
              <th>Student harm</th>
              <th>Inherent</th>
              <th>Mitigation</th>
              <th>Residual</th>
              <th>Owner / due</th>
            </tr>
          </thead>
          <tbody>
            {pack.risks.map((risk) => (
              <tr key={risk.id}>
                <td>
                  <strong>{risk.harm}</strong>
                  <br />
                  <small>{risk.affectedSubjects}</small>
                </td>
                <td>{risk.inherentRating}</td>
                <td>{risk.mitigation}</td>
                <td>{risk.residualRating}</td>
                <td>
                  {risk.owner}
                  <br />
                  <small>{risk.dueDate}</small>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section>
        <h2>Verification and disagreement</h2>
        <p>
          <strong>{pack.verification.verdict.replaceAll("-", " ")}</strong> ·{" "}
          {pack.verification.citationCoverage}
        </p>
        {pack.verification.notes.map((note) => (
          <p key={note}>• {note}</p>
        ))}
        {pack.contradictions.map((item) => (
          <div key={item.id} className="dpia-print-finding">
            <h3>{item.title}</h3>
            <p>{item.summary}</p>
            <small>Resolution: {item.resolution ?? "Unresolved"}</small>
          </div>
        ))}
      </section>

      <section className="dpia-print-break">
        <h2>Attributed audit trail</h2>
        <ol>
          {pack.audit.map((event) => (
            <li key={event.id}>
              <strong>
                {formatDate(event.timestamp)} · {event.actor}
              </strong>{" "}
              ({event.role}) — {event.action}: {event.object}
              {event.newValue ? ` → ${event.newValue}` : ""}
            </li>
          ))}
        </ol>
      </section>
    </article>
  );
}

function PrintField({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("en-GB", { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(value),
  );
}

function titleCase(value: string) {
  return value.charAt(0).toUpperCase() + value.slice(1);
}
