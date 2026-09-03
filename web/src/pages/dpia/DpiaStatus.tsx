import {
  AlertTriangleIcon,
  CheckCircle2Icon,
  CircleHelpIcon,
  Clock3Icon,
  RefreshCwIcon,
  SearchXIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

const statusPresentation: Record<
  string,
  { label: string; className: string; icon: typeof CheckCircle2Icon }
> = {
  answerable: {
    label: "Answerable",
    className: "border-status-green/25 bg-status-green/10 text-status-green",
    icon: CheckCircle2Icon,
  },
  confirmed: {
    label: "Confirmed",
    className: "border-status-green/25 bg-status-green/10 text-status-green",
    icon: CheckCircle2Icon,
  },
  "needs-judgement": {
    label: "Needs judgement",
    className: "border-status-blue/25 bg-status-blue/10 text-status-blue",
    icon: CircleHelpIcon,
  },
  "potential-issue": {
    label: "Potential issue",
    className: "border-status-yellow/30 bg-status-yellow/10 text-status-yellow",
    icon: AlertTriangleIcon,
  },
  "missing-evidence": {
    label: "Missing evidence",
    className: "border-status-red/25 bg-status-red/10 text-status-red",
    icon: SearchXIcon,
  },
  "stale-after-change": {
    label: "Stale after change",
    className: "border-status-yellow/30 bg-status-yellow/10 text-status-yellow",
    icon: RefreshCwIcon,
  },
  current: {
    label: "Current",
    className: "border-status-green/25 bg-status-green/10 text-status-green",
    icon: CheckCircle2Icon,
  },
  stale: {
    label: "Stale",
    className: "border-status-yellow/30 bg-status-yellow/10 text-status-yellow",
    icon: RefreshCwIcon,
  },
  expired: {
    label: "Expired",
    className: "border-status-red/25 bg-status-red/10 text-status-red",
    icon: Clock3Icon,
  },
  answered: {
    label: "Answered",
    className: "border-status-green/25 bg-status-green/10 text-status-green",
    icon: CheckCircle2Icon,
  },
  approved: {
    label: "Approved",
    className: "border-status-blue/25 bg-status-blue/10 text-status-blue",
    icon: CheckCircle2Icon,
  },
  draft: {
    label: "Draft",
    className: "border-border bg-muted text-muted-foreground",
    icon: Clock3Icon,
  },
  unanswered: {
    label: "Unanswered",
    className: "border-status-red/25 bg-status-red/10 text-status-red",
    icon: SearchXIcon,
  },
};

export function DpiaStatus({ status, className }: { status: string; className?: string }) {
  const presentation = statusPresentation[status] ?? {
    label: status,
    className: "border-border bg-muted text-muted-foreground",
    icon: CircleHelpIcon,
  };
  const Icon = presentation.icon;

  return (
    <Badge variant="outline" className={cn("gap-1", presentation.className, className)}>
      <Icon className="size-3" aria-hidden="true" />
      {presentation.label}
    </Badge>
  );
}
