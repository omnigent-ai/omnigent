import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { Link } from "@/lib/routing";

export function WorkflowSessionLink({ labels }: { labels: Record<string, string | null> }) {
  const info = useServerInfo();
  const workflowId = labels["omnigent.workflow.id"];
  if (
    !isFeatureEnabled(info, "dynamic_workflows") ||
    labels["omnigent.workflow.root"] !== "true" ||
    !workflowId ||
    !/^[0-9a-f]{32}$/.test(workflowId)
  )
    return null;
  return (
    <div
      className="shrink-0 border-b px-4 py-2 text-sm"
      aria-label="Workflow root session"
      style={{
        paddingTop: "calc(var(--omnigent-header-height) + var(--omnigent-inset-top) + 0.5rem)",
      }}
    >
      <span className="text-muted-foreground">This is the workflow’s root session. </span>
      <Link className="underline" to={"/workflows?workflow=" + workflowId}>
        Open workflow designer
      </Link>
    </div>
  );
}
