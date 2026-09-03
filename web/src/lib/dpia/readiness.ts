import type {
  Contradiction,
  Determination,
  EvidenceItem,
  ProcessingModel,
  ReadinessDefinition,
  ReadinessDimension,
  ReadinessSummary,
} from "./types";

export function calculateReadiness(
  processingModel: ProcessingModel,
  evidence: EvidenceItem[],
  contradictions: Contradiction[],
  definitions: ReadinessDefinition[],
  determinations: Determination[] = [],
): ReadinessSummary {
  const evidenceById = new Map(evidence.map((item) => [item.id, item]));
  const factsById = new Map(processingModel.facts.map((fact) => [fact.id, fact]));

  const dimensions = definitions.map<ReadinessDimension>((definition) => {
    const requiredFacts = definition.requiredFactIds.map((factId) => factsById.get(factId));
    const missingFactIds = definition.requiredFactIds.filter((_, index) => {
      const fact = requiredFacts[index];
      return (
        !fact ||
        fact.status === "missing" ||
        fact.value.trim() === "" ||
        fact.evidenceIds.length === 0
      );
    });
    const evidenceIds = Array.from(
      new Set(requiredFacts.flatMap((fact) => fact?.evidenceIds ?? [])),
    );
    const staleFactIds = requiredFacts
      .filter(
        (fact) =>
          fact?.status === "stale" ||
          fact?.evidenceIds.some(
            (evidenceId) => evidenceById.get(evidenceId)?.status !== "current",
          ),
      )
      .map((fact) => fact?.id)
      .filter((factId): factId is string => factId !== undefined);
    const staleDeterminations = determinations.filter(
      (determination) =>
        determination.dimensionId === definition.id &&
        (determination.status === "stale-after-change" ||
          determination.processingModelVersion !== processingModel.version),
    );
    const unresolvedContradictions = contradictions.filter(
      (contradiction) =>
        contradiction.material &&
        !contradiction.resolved &&
        contradiction.dimensionIds.includes(definition.id),
    );

    if (staleFactIds.length > 0 || staleDeterminations.length > 0) {
      return {
        ...definition,
        status: "stale-after-change",
        detail: "A material input changed after this determination was reviewed.",
        blockingFactIds: Array.from(
          new Set([
            ...staleFactIds,
            ...staleDeterminations.flatMap((determination) => determination.dependencyFactIds),
          ]),
        ),
        evidenceIds,
      };
    }

    if (missingFactIds.length > 0) {
      return {
        ...definition,
        status: "missing-evidence",
        detail: "Required facts are absent or are not supported by usable evidence.",
        blockingFactIds: missingFactIds,
        evidenceIds,
      };
    }

    if (unresolvedContradictions.length > 0) {
      return {
        ...definition,
        status: "needs-judgement",
        detail: "A material contradiction must be resolved or explicitly judged.",
        blockingFactIds: definition.requiredFactIds,
        evidenceIds,
      };
    }

    return {
      ...definition,
      status: "answerable",
      detail: "Required facts are supported by current evidence with no unresolved contradiction.",
      blockingFactIds: [],
      evidenceIds,
    };
  });

  return {
    answerable: dimensions.filter((dimension) => dimension.status === "answerable").length,
    total: dimensions.length,
    dimensions,
  };
}

export function changeProcessingFact(
  processingModel: ProcessingModel,
  determinations: Determination[],
  change: { factId: string; value: string; evidenceIds?: string[]; changedAt: string },
): { processingModel: ProcessingModel; determinations: Determination[]; changed: boolean } {
  const currentFact = processingModel.facts.find((fact) => fact.id === change.factId);
  if (!currentFact) throw new Error(`Unknown processing fact: ${change.factId}`);
  if (currentFact.value === change.value) {
    return { processingModel, determinations, changed: false };
  }

  const nextVersion = currentFact.material ? processingModel.version + 1 : processingModel.version;
  const nextModel: ProcessingModel = {
    ...processingModel,
    version: nextVersion,
    updatedAt: change.changedAt,
    facts: processingModel.facts.map((fact) =>
      fact.id === change.factId
        ? {
            ...fact,
            value: change.value,
            status: "confirmed",
            evidenceIds: change.evidenceIds ?? fact.evidenceIds,
          }
        : fact,
    ),
  };

  if (!currentFact.material) {
    return { processingModel: nextModel, determinations, changed: true };
  }

  return {
    processingModel: nextModel,
    determinations: determinations.map((determination) => {
      if (determination.dependencyFactIds.includes(change.factId)) {
        return {
          ...determination,
          status: "stale-after-change",
          staleReason: `${currentFact.label} changed in processing model v${nextVersion}.`,
        };
      }
      return determination.status === "stale-after-change"
        ? determination
        : { ...determination, processingModelVersion: nextVersion };
    }),
    changed: true,
  };
}
