import { useRef, useState, type ReactNode } from "react";
import { ComposerPermissionPicker } from "./ComposerControls";
import { SessionHarnessPicker } from "./SessionHarnessPicker";
import { useSessionPickerInputs } from "./sessionPickerInputs";
import { useSessionHarnessPickerProps } from "./useSessionHarnessPickerProps";
import type { GridPermissionControl } from "@/grid/useGridPermissionControl";
import { isNativeWrapper } from "@/lib/nativeCodingAgents";
import { readOnlyReasonForSessionLabels } from "@/lib/sessionCapabilities";
import type { Session } from "@/lib/types";

/** The embedded session's permission mode, never the active full-screen session's. */
export function SessionPermissionPicker({ permission }: { permission: GridPermissionControl }) {
  const { chip } = permission;
  if (chip === null) return null;
  return (
    <ComposerPermissionPicker
      label="Permission mode"
      value={chip.label || "Permission mode"}
      harness={permission.harness}
      selectedValue={chip.value}
      options={chip.options}
      disabled={!permission.owner || permission.unreachable || permission.busy}
      disabledReason={
        permission.owner ? undefined : "Only the session owner can change the permission mode"
      }
      onSelect={permission.select}
    />
  );
}

/** Shared model/effort controls for existing sessions embedded in projects or grids. */
export function ScopedSessionHarnessPicker({
  sessionId,
  session,
  canEdit,
  unreachable,
  runnerOnline,
  agentIcon,
}: {
  sessionId: string;
  session: Session;
  canEdit: boolean;
  unreachable: boolean;
  runnerOnline: boolean | undefined;
  agentIcon?: ReactNode;
}) {
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const inputs = useSessionPickerInputs(sessionId, session);
  const wrapper = session.labels?.["omnigent.wrapper"] ?? null;
  const picker = useSessionHarnessPickerProps({
    sessionId,
    hostId: session.hostId,
    inputs,
    agentName: session.agentName,
    wrapperLabel: wrapper,
    isNativeWrapper: isNativeWrapper(wrapper),
    runnerOnline,
    gates: {
      permissionLevel: canEdit ? (session.permissionLevel ?? null) : 1,
      readOnlyReason: readOnlyReasonForSessionLabels(session, null),
      sendDisabledReason: null,
      unreachable,
    },
  });
  return (
    <SessionHarnessPicker
      appearance="composer"
      agentIcon={agentIcon}
      busy={busy}
      busyRef={busyRef}
      setBusy={setBusy}
      {...picker}
    />
  );
}
