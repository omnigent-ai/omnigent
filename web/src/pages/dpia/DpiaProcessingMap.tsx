import {
  ArchiveIcon,
  ArrowRightIcon,
  DatabaseIcon,
  EyeIcon,
  FolderInputIcon,
  NetworkIcon,
  SendIcon,
  Share2Icon,
  Trash2Icon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { DpiaCaseSnapshot, LifecycleNode, LifecycleStage } from "@/lib/dpia/types";

const stagePresentation: Record<
  LifecycleStage,
  { label: string; icon: typeof FolderInputIcon; accent: string }
> = {
  collection: {
    label: "Collection",
    icon: FolderInputIcon,
    accent: "text-status-blue bg-status-blue/10",
  },
  storage: { label: "Storage", icon: DatabaseIcon, accent: "text-status-gray bg-status-gray/10" },
  access: { label: "Access", icon: EyeIcon, accent: "text-status-yellow bg-status-yellow/10" },
  use: { label: "Use", icon: NetworkIcon, accent: "text-status-green bg-status-green/10" },
  sharing: { label: "Sharing", icon: Share2Icon, accent: "text-status-blue bg-status-blue/10" },
  transfer: { label: "Transfer", icon: SendIcon, accent: "text-status-red bg-status-red/10" },
  retention: {
    label: "Retention",
    icon: ArchiveIcon,
    accent: "text-status-yellow bg-status-yellow/10",
  },
  deletion: { label: "Deletion", icon: Trash2Icon, accent: "text-status-gray bg-status-gray/10" },
};

export function DpiaProcessingMap({ caseData }: { caseData: DpiaCaseSnapshot }) {
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="font-semibold">Eight-stage data lifecycle</h2>
          <p className="mt-0.5 text-sm text-muted-foreground">
            Each stage separates confirmed flow details from facts that still block a defensible
            determination.
          </p>
        </div>
        <Badge variant="outline">Processing model v{caseData.processingModel.version}</Badge>
      </div>

      <ol className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        {caseData.lifecycle.map((node, index) => (
          <li key={node.stage} className="relative min-w-0">
            <LifecycleCard node={node} index={index} />
            {index < caseData.lifecycle.length - 1 && (
              <ArrowRightIcon className="absolute top-5 -right-2 z-10 hidden size-4 rounded-full bg-background text-muted-foreground xl:block" />
            )}
          </li>
        ))}
      </ol>

      <div className="grid gap-4 xl:grid-cols-2">
        {caseData.lifecycle.map((node) => {
          const presentation = stagePresentation[node.stage];
          const Icon = presentation.icon;
          return (
            <section
              key={`detail-${node.stage}`}
              className="overflow-hidden rounded-lg border border-border bg-card"
            >
              <header className="flex items-center justify-between gap-3 border-b border-border bg-muted/30 px-4 py-3">
                <div className="flex items-center gap-2">
                  <span
                    className={`flex size-7 items-center justify-center rounded-md ${presentation.accent}`}
                  >
                    <Icon className="size-4" />
                  </span>
                  <h3 className="font-semibold">{presentation.label}</h3>
                </div>
                <Badge
                  variant="outline"
                  className={
                    node.missingFacts.length > 0
                      ? "border-status-red/25 bg-status-red/10 text-status-red"
                      : "border-status-green/25 bg-status-green/10 text-status-green"
                  }
                >
                  {node.missingFacts.length > 0
                    ? `${node.missingFacts.length} blockers`
                    : "No blockers"}
                </Badge>
              </header>
              <dl className="grid text-ui sm:grid-cols-2">
                <LifecycleField label="Data" value={node.data.join(", ")} />
                <LifecycleField label="Purpose" value={node.purpose} />
                <LifecycleField label="Actor" value={node.actors.join(", ")} />
                <LifecycleField label="System" value={node.systems.join(", ")} />
                <LifecycleField label="Location" value={node.location} />
                <LifecycleField label="Recipients" value={node.recipients.join(", ")} />
                <LifecycleField label="Legal basis" value={node.legalBasis} />
                <LifecycleField label="Retention" value={node.retention} />
              </dl>
              <div className="border-t border-border px-4 py-3">
                <p className="mb-2 text-sm font-medium text-muted-foreground">Controls</p>
                <div className="flex flex-wrap gap-1.5">
                  {node.controls.map((control) => (
                    <Badge key={control} variant="secondary">
                      {control}
                    </Badge>
                  ))}
                </div>
              </div>
              {node.missingFacts.length > 0 && (
                <div className="border-t border-border bg-muted/30 px-4 py-3">
                  <p className="mb-2 text-sm font-medium text-status-red">Missing facts</p>
                  <ul className="grid gap-1 text-sm text-muted-foreground">
                    {node.missingFacts.map((fact) => (
                      <li key={fact}>• {fact}</li>
                    ))}
                  </ul>
                </div>
              )}
            </section>
          );
        })}
      </div>
    </div>
  );
}

function LifecycleCard({ node, index }: { node: LifecycleNode; index: number }) {
  const presentation = stagePresentation[node.stage];
  const Icon = presentation.icon;
  return (
    <div className="h-full rounded-lg border border-border bg-card p-3">
      <div className="mb-3 flex items-center justify-between gap-2">
        <span
          className={`flex size-8 items-center justify-center rounded-md ${presentation.accent}`}
        >
          <Icon className="size-4" />
        </span>
        <span className="text-sm font-medium tabular-nums text-muted-foreground">0{index + 1}</span>
      </div>
      <h3 className="font-semibold">{presentation.label}</h3>
      <p className="mt-1 line-clamp-3 min-h-15 text-sm text-muted-foreground">{node.purpose}</p>
      <div className="mt-3 flex items-center justify-between border-t border-border pt-2 text-sm">
        <span className="text-muted-foreground">Blockers</span>
        <span
          className={
            node.missingFacts.length > 0 ? "font-semibold text-status-red" : "text-status-green"
          }
        >
          {node.missingFacts.length}
        </span>
      </div>
    </div>
  );
}

function LifecycleField({ label, value }: { label: string; value: string }) {
  return (
    <div className="border-b border-border px-4 py-3 odd:sm:border-r">
      <dt className="mb-1 text-sm font-medium text-muted-foreground">{label}</dt>
      <dd className="leading-relaxed">{value}</dd>
    </div>
  );
}
