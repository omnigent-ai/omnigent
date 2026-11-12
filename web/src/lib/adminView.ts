const ADMIN_VIEW_COOKIE = "omnigent_admin_view";

export function isAdminViewEnabled(): boolean {
  return (
    typeof document === "undefined" ||
    !document.cookie.split(";").some((cookie) => cookie.trim() === `${ADMIN_VIEW_COOKIE}=off`)
  );
}

export function setAdminViewEnabled(enabled: boolean): void {
  document.cookie = `${ADMIN_VIEW_COOKIE}=${enabled ? "on" : "off"}; Path=/; Max-Age=31536000; SameSite=Lax${location.protocol === "https:" ? "; Secure" : ""}`;
  if (isAdminViewEnabled() !== enabled)
    throw new Error("Allow browser cookies to change admin view.");
  location.reload();
}
