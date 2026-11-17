// The app's zod, configured before any schema runs. Zod's fast path probes for
// `new Function`, which a Content-Security-Policy without 'unsafe-eval' reports
// as a violation; interpreted parsing is enough for the payloads validated here.
import { z } from "zod";

z.config({ jitless: true });

export { z };
