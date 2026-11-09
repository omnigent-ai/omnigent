"use strict";

const path = require("node:path");
const { startReactFixture } = require("./reactFixtureServer");

const startDropdownFixture = () =>
  startReactFixture(
    path.join(__dirname, "dropdownFixture.jsx"),
    path.join(__dirname, "dropdownFixture.html"),
    "/deploy-stage",
  );

module.exports = { startDropdownFixture };
