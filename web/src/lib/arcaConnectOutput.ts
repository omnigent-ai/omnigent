import { showToast } from "@/components/ui/toast";
import { showArcaConnectOutput } from "@/lib/nativeBridge";

/**
 * Open the running Arca connect's output window. Some connects have none,
 * such as an automatic connect the shell started itself, so say so.
 */
export function openArcaConnectOutput(): void {
  void showArcaConnectOutput().then((shown) => {
    if (!shown) showToast("There's no connection output to show for this Arca connect.");
  });
}
