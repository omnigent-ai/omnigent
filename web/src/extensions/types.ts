export interface ExtensionPage {
  id: string;
  title: string;
  route: string;
  view: string;
}

export interface ExtensionPrimaryNavigation {
  id: string;
  label: string;
  page: string;
  icon: string | null;
  order: number;
  when: string | null;
}

export interface ExtensionBrowserBundle {
  declared: boolean;
  has_styles: boolean;
  digest: string | null;
  script_url: string | null;
  style_url: string | null;
}

export interface ExtensionCatalogItem {
  object: "extension";
  id: string;
  display_name: string;
  distribution: string;
  version: string;
  extension_api: number;
  status: "enabled" | "unavailable";
  permissions: string[];
  pages: ExtensionPage[];
  primary_navigation: ExtensionPrimaryNavigation[];
  browser: ExtensionBrowserBundle;
}

export interface ExtensionCatalogResponse {
  object: "list";
  data: ExtensionCatalogItem[];
}

export interface ResolvedExtensionPage {
  extension: ExtensionCatalogItem;
  page: ExtensionPage;
}
