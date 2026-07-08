#!/usr/bin/env bash
# Start the Omnigent host inside the microVM. The hooks server spawns this on
# the /run lifecycle hook, with the launcher's runHookPayload exported into the
# environment: OMNIGENT_HOST_ID / OMNIGENT_HOST_NAME / OMNIGENT_HOST_TOKEN /
# OMNIGENT_SERVER (+ any harness credentials), and optionally OMNIGENT_REPO_URL
# (+ _BRANCH / _NAME) for a repository workspace.
#
# `omnigent host` opens an OUTBOUND tunnel to OMNIGENT_SERVER using the injected
# identity + token, so the real agent work rides that tunnel, not the hooks.
set -euo pipefail

# The base image sets no HOME guarantee; under `set -u` a bare ${HOME} would
# abort the whole start. Default to /root (the image's container user).
WORKSPACE="${HOME:-/root}/workspace"
mkdir -p "${WORKSPACE}"

if [ -n "${OMNIGENT_REPO_URL:-}" ]; then
  # Sanitize the server-supplied repo name so it can't escape ${WORKSPACE}.
  # basename strips any path, but basename "." / ".." are still "." / ".." and a
  # leading '-' could read as a flag — collapse those to the safe default "repo".
  REPO_DIR_NAME="$(basename -- "${OMNIGENT_REPO_NAME:-repo}")"
  case "${REPO_DIR_NAME}" in
    "" | "." | ".." | -*) REPO_DIR_NAME="repo" ;;
  esac
  CLONE_DIR="${WORKSPACE}/${REPO_DIR_NAME}"
  if [ -n "${OMNIGENT_REPO_BRANCH:-}" ]; then
    git clone --branch "${OMNIGENT_REPO_BRANCH}" --single-branch -- \
      "${OMNIGENT_REPO_URL}" "${CLONE_DIR}"
  else
    git clone -- "${OMNIGENT_REPO_URL}" "${CLONE_DIR}"
  fi
  cd "${CLONE_DIR}"
else
  cd "${WORKSPACE}"
fi

# The server URL must be passed with --server (a bare `omnigent host` with no
# server spawns a LOCAL server and connects to itself instead of dialing back).
# Identity + token come from the OMNIGENT_HOST_* environment the hooks server
# exported from the /run payload.
if [ -z "${OMNIGENT_SERVER:-}" ]; then
  echo "start_host: OMNIGENT_SERVER not set in /run payload — cannot dial back" >&2
  exit 1
fi
exec omnigent host --server "${OMNIGENT_SERVER}"
