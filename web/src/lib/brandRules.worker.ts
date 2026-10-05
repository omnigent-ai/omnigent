// Runs brand-rule scans off the main thread, so a slow pattern can be terminated.

import { handleBrandScan, type BrandScanInput } from "./brandRules";

self.onmessage = (e: MessageEvent<BrandScanInput>) => self.postMessage(handleBrandScan(e.data));
