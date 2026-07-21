import { OttoEyes } from "@/components/OttoEyes";
import { OttoIcon } from "@/components/icons/OttoIcon";
import { useAppName, useLogoUrl } from "@/lib/branding";

/**
 * The app's brand logo: the operator's custom logo when configured, else the
 * Otto mascot. `variant` picks the logo variant and matching fallback —
 * `"eyes"` (hero) → `main`/`OttoEyes`, `"icon"` (indicators) → `loading`/`OttoIcon`.
 */
export function BrandLogo({
  className,
  variant = "eyes",
}: {
  className?: string;
  variant?: "eyes" | "icon";
}) {
  const logoUrl = useLogoUrl(variant === "icon" ? "loading" : "main");
  const appName = useAppName();
  if (logoUrl) {
    return <img src={logoUrl} className={className} alt={appName} />;
  }
  return variant === "eyes" ? (
    <OttoEyes className={className} />
  ) : (
    <OttoIcon className={className} aria-hidden />
  );
}
