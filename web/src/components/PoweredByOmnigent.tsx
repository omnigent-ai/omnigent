import { OttoEyes } from "@/components/OttoEyes";

/**
 * Understated "Powered by Omnigent" mark for the landing footer. Always uses
 * the real Otto mascot (not the operator's logo), since it credits the product.
 */
export function PoweredByOmnigent() {
  return (
    <div
      className="group inline-flex select-none items-center gap-1 text-xs text-muted-foreground/45 transition-colors duration-300 ease-out hover:text-muted-foreground/75"
      data-testid="powered-by-omnigent"
    >
      <span>Powered by</span>
      <OttoEyes className="h-3.5 w-auto opacity-60 transition-opacity duration-300 ease-out group-hover:opacity-100" />
      <span>Omnigent</span>
    </div>
  );
}
