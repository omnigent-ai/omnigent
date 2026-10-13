import { PencilIcon, PlusIcon, ServerIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import type { StaticSandboxPicker } from "@/hooks/useStaticSandboxPicker";
import { sandboxProviderName } from "@/lib/capabilities";
import {
  AddStaticSandboxDialog,
  RenameStaticSandboxDialog,
  StaticSandboxStatusBadge,
} from "./StaticSandboxDialogs";

/** The New Session host menu's static sandbox rows plus the Add row. */
export function StaticSandboxMenuSection({ picker }: { picker: StaticSandboxPicker }) {
  if (!picker.enabled) return null;
  return (
    <>
      {picker.sandboxes.map((vm) =>
        vm.status === "gone" ? (
          <DropdownMenuItem
            key={`static-sandbox-${vm.id}`}
            // Not selectable, but its Remove button must stay usable.
            onSelect={(e) => e.preventDefault()}
            className="flex items-center justify-between gap-2 text-sm"
            data-testid={`new-chat-landing-static-sandbox-option-${vm.id}`}
          >
            <span className="flex min-w-0 flex-col">
              <span className="flex items-center gap-1 text-muted-foreground line-through">
                <span className="flex size-4 shrink-0 items-center justify-center">
                  <ServerIcon className="size-3.5" />
                </span>
                {vm.label}
              </span>
              <span className="pl-5 text-xs text-destructive">VM no longer exists</span>
            </span>
            <Button
              variant="outline"
              size="sm"
              className="h-6 px-2 text-xs"
              onClick={(e) => {
                e.stopPropagation();
                picker.remove(vm.id);
              }}
              data-testid={`new-chat-landing-static-sandbox-remove-${vm.id}`}
            >
              Remove
            </Button>
          </DropdownMenuItem>
        ) : (
          <DropdownMenuItem
            key={`static-sandbox-${vm.id}`}
            onSelect={() => picker.selectFromMenu(vm.id)}
            data-active={picker.activeId === vm.id ? "true" : undefined}
            className="flex items-center justify-between gap-2 text-sm data-[active=true]:bg-muted dark:data-[active=true]:bg-muted/50"
            data-testid={`new-chat-landing-static-sandbox-option-${vm.id}`}
          >
            <span className="flex min-w-0 items-center gap-1">
              <span className="flex size-4 shrink-0 items-center justify-center">
                <ServerIcon className="size-3.5 text-muted-foreground" />
              </span>
              <span className="flex min-w-0 flex-col" title={vm.id}>
                <span className="text-sm break-words">{vm.label}</span>
                {vm.label !== vm.id && (
                  <span className="text-xs break-all text-muted-foreground">{vm.id}</span>
                )}
              </span>
            </span>
            <span className="flex shrink-0 items-center gap-1">
              <StaticSandboxStatusBadge status={vm.status} />
              <button
                type="button"
                aria-label={`Rename ${vm.label}`}
                className="inline-flex size-5 items-center justify-center rounded-sm text-muted-foreground hover:bg-background/60 hover:text-foreground"
                onClick={() => picker.requestRename(vm.id)}
                data-testid={`new-chat-landing-static-sandbox-rename-${vm.id}`}
              >
                <PencilIcon className="size-3" />
              </button>
            </span>
          </DropdownMenuItem>
        ),
      )}
      <DropdownMenuItem
        onSelect={picker.requestAdd}
        className="text-sm text-muted-foreground"
        data-testid="new-chat-landing-add-static-sandbox"
      >
        <span className="flex size-4 shrink-0 items-center justify-center">
          <PlusIcon className="size-3.5" />
        </span>
        <span>
          {picker.provider !== null
            ? `Add ${sandboxProviderName(picker.provider)} Static Sandbox…`
            : "Add Static Sandbox…"}
        </span>
      </DropdownMenuItem>
    </>
  );
}

/**
 * The Add and Rename dialogs. Rendered outside the host menu: they open once
 * it has closed, which unmounts its content.
 */
export function StaticSandboxPickerDialogs({ picker }: { picker: StaticSandboxPicker }) {
  if (!picker.enabled) return null;
  return (
    <>
      <AddStaticSandboxDialog
        open={picker.addOpen}
        onOpenChange={picker.setAddOpen}
        onAdded={picker.select}
        provider={picker.provider}
      />
      <RenameStaticSandboxDialog
        target={picker.renaming}
        onOpenChange={(open) => {
          if (!open) picker.closeRename();
        }}
      />
    </>
  );
}
