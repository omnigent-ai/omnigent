#!/usr/bin/env python3
"""Signed-in ``cursor-agent`` double: version, auth status and a fixed model list."""

import json
import os
import sys
import time

ARGS = sys.argv[1:]
VERSION = "2026.09.26-dd393fe"

log_path = os.environ.get("CURSOR_AGENT_DOUBLE_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": ARGS, "cwd": os.getcwd(), "time": time.time()}) + "\n")

if "--version" in ARGS or "-v" in ARGS:
    print(VERSION)
    raise SystemExit(0)
if ARGS[:1] == ["status"]:
    status = {
        "status": "authenticated",
        "isAuthenticated": True,
        "hasAccessToken": True,
        "hasRefreshToken": True,
        "version": VERSION,
    }
    print(json.dumps(status))
    raise SystemExit(0)
if ARGS[:1] == ["models"]:
    models_file = os.environ.get("CURSOR_AGENT_DOUBLE_MODELS_FILE")
    if not models_file:
        print("stub cursor-agent: CURSOR_AGENT_DOUBLE_MODELS_FILE is not set", file=sys.stderr)
        raise SystemExit(1)
    with open(models_file, encoding="utf-8") as fh:
        sys.stdout.write(fh.read())
    raise SystemExit(0)
print("stub cursor-agent: unsupported invocation: " + " ".join(ARGS), file=sys.stderr)
raise SystemExit(2)
