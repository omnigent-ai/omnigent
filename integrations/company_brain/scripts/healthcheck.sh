#!/bin/sh
set -eu

curl -fsS http://127.0.0.1:3131/health >/dev/null
