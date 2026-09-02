#!/usr/bin/env sh
set -eu

: "${GBRAIN_HOME:?set GBRAIN_HOME to the isolated company brain state directory}"
: "${GBRAIN_DATABASE_URL:?set GBRAIN_DATABASE_URL to the isolated Postgres database}"
: "${GBRAIN_PUBLIC_URL:?set GBRAIN_PUBLIC_URL to the public HTTPS origin}"
: "${GBRAIN_ADMIN_BOOTSTRAP_TOKEN:?set GBRAIN_ADMIN_BOOTSTRAP_TOKEN through a secret manager}"

case "$GBRAIN_DATABASE_URL" in
  postgres://*|postgresql://*) ;;
  *)
    printf '%s\n' 'GBRAIN_DATABASE_URL must use PostgreSQL for the production HTTP service' >&2
    exit 1
    ;;
esac

case "$GBRAIN_PUBLIC_URL" in
  https://*) ;;
  *)
    printf '%s\n' 'GBRAIN_PUBLIC_URL must use HTTPS' >&2
    exit 1
    ;;
esac

expected_version='gbrain 0.46.30.0'
actual_version=$(gbrain --version)
if [ "$actual_version" != "$expected_version" ]; then
  printf 'expected %s, found %s\n' "$expected_version" "$actual_version" >&2
  exit 1
fi

mkdir -p "$GBRAIN_HOME"
config_path="$GBRAIN_HOME/.gbrain/config.json"
if [ ! -f "$config_path" ]; then
  case "${GBRAIN_NO_EMBEDDING:-1}" in
    1) gbrain init --url "$GBRAIN_DATABASE_URL" --non-interactive --no-embedding ;;
    0) gbrain init --url "$GBRAIN_DATABASE_URL" --non-interactive ;;
    *)
      printf '%s\n' 'GBRAIN_NO_EMBEDDING must be 0 or 1' >&2
      exit 1
      ;;
  esac
elif ! bun -e '
const config = await Bun.file(process.argv[1]).json();
const noEmbedding = (process.env.GBRAIN_NO_EMBEDDING ?? "1") === "1";
if (
  config.engine !== "postgres" ||
  config.database_url !== process.env.GBRAIN_DATABASE_URL ||
  config.embedding_disabled !== noEmbedding
) process.exit(1);
' "$config_path"; then
  printf '%s\n' 'persisted gbrain engine configuration differs from this deployment' >&2
  exit 1
fi
gbrain doctor

exec gbrain serve \
  --http \
  --bind "${GBRAIN_BIND:-0.0.0.0}" \
  --port "${GBRAIN_PORT:-3131}" \
  --public-url "$GBRAIN_PUBLIC_URL" \
  --surface starter \
  --suppress-bootstrap-token
