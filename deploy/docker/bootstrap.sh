#!/usr/bin/env bash
# First-run helper: ensures deploy/docker/.env exists with the two
# required secrets (POSTGRES_PASSWORD, OMNIGENT_OIDC_COOKIE_SECRET)
# generated for you, instead of making the user run `openssl rand -hex 32`
# twice. Safe to re-run — never overwrites existing non-default values.
#
# Usage:
#   cd deploy/docker
#   ./bootstrap.sh
#   docker compose up -d
#
# Idempotency:
#   - If .env doesn't exist, copies .env.example → .env first.
#   - If POSTGRES_PASSWORD is unset, empty, or still the example
#     placeholder ("change-me-please"), mints a fresh random value.
#   - If OMNIGENT_OIDC_COOKIE_SECRET is unset OR commented out,
#     uncomments + sets it to a fresh 64-hex-char value. (Even if
#     you're not using OIDC today, having the secret ready means
#     enabling it later is a one-line edit.)
#   - Already-customized values are left alone.

set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "→ created .env from .env.example"
fi

# openssl is the dependency we already document for cookie-secret
# generation; bail loud if missing rather than papering over with a
# weaker source.
if ! command -v openssl >/dev/null 2>&1; then
  echo "ERROR: openssl not found on PATH (needed to generate secrets)" >&2
  exit 1
fi

# In-place edit helper that works on both GNU sed and BSD/macOS sed.
sed_inplace() {
  if sed --version >/dev/null 2>&1; then
    sed -i "$@"
  else
    sed -i '' "$@"
  fi
}

set_or_replace_kv() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" .env; then
    sed_inplace "s|^${key}=.*|${key}=${value}|" .env
  elif grep -qE "^# *${key}=" .env; then
    sed_inplace "s|^# *${key}=.*|${key}=${value}|" .env
  else
    printf '\n%s=%s\n' "$key" "$value" >> .env
  fi
}

current_value() {
  local key="$1"
  grep -E "^${key}=" .env | head -n 1 | cut -d= -f2- || true
}

pg_current=$(current_value POSTGRES_PASSWORD)
if [[ -z "$pg_current" || "$pg_current" == "change-me-please" ]]; then
  set_or_replace_kv POSTGRES_PASSWORD "$(openssl rand -hex 16)"
  echo "→ generated POSTGRES_PASSWORD"
else
  echo "→ POSTGRES_PASSWORD already set, leaving alone"
fi

cookie_current=$(current_value OMNIGENT_OIDC_COOKIE_SECRET)
if [[ -z "$cookie_current" || "$cookie_current" == "<64-hex-chars>" ]]; then
  set_or_replace_kv OMNIGENT_OIDC_COOKIE_SECRET "$(openssl rand -hex 32)"
  echo "→ generated OMNIGENT_OIDC_COOKIE_SECRET"
else
  echo "→ OMNIGENT_OIDC_COOKIE_SECRET already set, leaving alone"
fi

# Same generation logic for the accounts cookie secret. The two
# secrets are independent — OIDC and accounts modes are mutually
# exclusive in a single deploy, but having both pre-minted means
# the operator can switch modes by editing OMNIGENT_AUTH_PROVIDER
# without re-running bootstrap.
accounts_cookie_current=$(current_value OMNIGENT_ACCOUNTS_COOKIE_SECRET)
if [[ -z "$accounts_cookie_current" || "$accounts_cookie_current" == "<64-hex-chars>" ]]; then
  set_or_replace_kv OMNIGENT_ACCOUNTS_COOKIE_SECRET "$(openssl rand -hex 32)"
  echo "→ generated OMNIGENT_ACCOUNTS_COOKIE_SECRET"
else
  echo "→ OMNIGENT_ACCOUNTS_COOKIE_SECRET already set, leaving alone"
fi

echo
echo "✓ deploy/docker/.env is ready. Next:"
echo "    docker compose up -d && docker compose logs omnigent"
echo
echo "  Accounts mode is the default — no credentials are auto-generated."
echo "  First boot prints a 'No admin yet' line; open that URL and create"
echo "  the first admin (username + password) via the web form. For any"
echo "  public-domain deploy also set:"
echo "    OMNIGENT_ACCOUNTS_BASE_URL=<your public URL>"
echo "  in .env so that link and invite links resolve to the right host."
