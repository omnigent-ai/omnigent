import { useState } from "react";
import { BellIcon, PlusIcon, Trash2Icon } from "lucide-react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { CostAlert } from "@/lib/usageApi";
import { createCostAlert, deleteCostAlert, updateCostAlert } from "@/lib/usageApi";
import { formatSessionCostUsd } from "@/lib/formatCost";

interface Props {
  alerts: CostAlert[];
  alertTriggered: boolean;
}

export function CostAlertSettings({ alerts, alertTriggered }: Props) {
  const [isAdding, setIsAdding] = useState(false);
  const [newThreshold, setNewThreshold] = useState("");
  const [newPeriod, setNewPeriod] = useState<"daily" | "monthly">("daily");
  const queryClient = useQueryClient();

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["usage"] });

  const createMutation = useMutation({
    mutationFn: () => createCostAlert(parseFloat(newThreshold), newPeriod),
    onSuccess: () => {
      invalidate();
      setIsAdding(false);
      setNewThreshold("");
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteCostAlert(id),
    onSuccess: invalidate,
  });

  const toggleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      updateCostAlert(id, { enabled }),
    onSuccess: invalidate,
  });

  return (
    <div className="rounded-lg border border-border bg-card">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <BellIcon className="h-4 w-4 text-muted-foreground" />
          <h3 className="text-sm font-medium">Cost alerts</h3>
          {alertTriggered && (
            <span className="rounded-full bg-destructive/10 px-2 py-0.5 text-xs font-medium text-destructive">
              Triggered
            </span>
          )}
        </div>
        <button
          type="button"
          className="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-primary hover:bg-muted"
          onClick={() => setIsAdding(!isAdding)}
        >
          <PlusIcon className="h-3 w-3" />
          Add
        </button>
      </div>

      {isAdding && (
        <div className="flex items-center gap-2 border-b border-border px-4 py-3">
          <span className="text-sm text-muted-foreground">$</span>
          <input
            type="number"
            min="0.01"
            step="0.01"
            placeholder="0.00"
            value={newThreshold}
            onChange={(e) => setNewThreshold(e.target.value)}
            className="w-24 rounded-md border border-border bg-background px-2 py-1 text-sm"
          />
          <select
            value={newPeriod}
            onChange={(e) => setNewPeriod(e.target.value as "daily" | "monthly")}
            className="rounded-md border border-border bg-background px-2 py-1 text-sm"
          >
            <option value="daily">Daily</option>
            <option value="monthly">Monthly</option>
          </select>
          <button
            type="button"
            disabled={!newThreshold || parseFloat(newThreshold) <= 0 || createMutation.isPending}
            className="rounded-md bg-primary px-3 py-1 text-xs font-medium text-primary-foreground disabled:opacity-50"
            onClick={() => createMutation.mutate()}
          >
            {createMutation.isPending ? "..." : "Save"}
          </button>
          <button
            type="button"
            className="text-xs text-muted-foreground hover:text-foreground"
            onClick={() => setIsAdding(false)}
          >
            Cancel
          </button>
        </div>
      )}

      {alerts.length === 0 && !isAdding ? (
        <p className="px-4 py-4 text-center text-sm text-muted-foreground">No alerts configured</p>
      ) : (
        <div className="divide-y divide-border">
          {alerts.map((alert) => (
            <div key={alert.id} className="flex items-center justify-between px-4 py-2.5">
              <div className="flex items-center gap-3">
                <button
                  type="button"
                  className={`h-4 w-8 rounded-full transition-colors ${
                    alert.enabled ? "bg-primary" : "bg-muted"
                  }`}
                  onClick={() => toggleMutation.mutate({ id: alert.id, enabled: !alert.enabled })}
                >
                  <div
                    className={`h-3 w-3 rounded-full bg-white transition-transform ${
                      alert.enabled ? "translate-x-4" : "translate-x-0.5"
                    }`}
                  />
                </button>
                <span className="text-sm">
                  {formatSessionCostUsd(alert.thresholdUsd)}{" "}
                  <span className="text-muted-foreground">{alert.period}</span>
                </span>
              </div>
              <button
                type="button"
                className="text-muted-foreground hover:text-destructive"
                onClick={() => deleteMutation.mutate(alert.id)}
              >
                <Trash2Icon className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
