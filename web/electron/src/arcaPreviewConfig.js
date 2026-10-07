"use strict";

// One budget spans renderer metadata lookup and main-process Arca preparation.
// A cold Arca status startup can take about 26s before mux setup begins.
const ARCA_PREVIEW_TIMEOUT_MS = 60_000;

module.exports = { ARCA_PREVIEW_TIMEOUT_MS };
