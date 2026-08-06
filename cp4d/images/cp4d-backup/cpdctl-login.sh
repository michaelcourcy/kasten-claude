#!/usr/bin/env bash
# Configure cpdctl headlessly by reading the credential secret cross-namespace
# (the SA needs get on this one secret; the secret is NOT mounted).
# The rendered config lives only in $CPDCONFIG (an ephemeral path) — never persisted.
set -euo pipefail
: "${CRED_NS:=cpd}"
: "${CRED_SECRET:=cp4d-backup-cpdctl-creds}"
: "${CPDCONFIG:=/tmp/.cpdctl-config.json}"
export CPDCONFIG

# Prefer explicit env creds (used by the host-run restore path, which feeds an
# arbitrary restore namespace without pre-provisioning cross-ns RBAC there).
# Otherwise read the credential secret cross-namespace (the backup path).
if [ -n "${CPD_URL:-}" ] && [ -n "${CPD_USERNAME:-}" ] && [ -n "${CPD_APIKEY:-}" ]; then
  URL="$CPD_URL"; USR="$CPD_USERNAME"; KEY="$CPD_APIKEY"
else
  URL=$(kubectl get secret -n "$CRED_NS" "$CRED_SECRET" -o jsonpath='{.data.url}'      | base64 -d)
  USR=$(kubectl get secret -n "$CRED_NS" "$CRED_SECRET" -o jsonpath='{.data.username}' | base64 -d)
  KEY=$(kubectl get secret -n "$CRED_NS" "$CRED_SECRET" -o jsonpath='{.data.apikey}'   | base64 -d)
fi

cpdctl config user set svc --username "$USR" --apikey "$KEY" >/dev/null
cpdctl config profile set cp4d --url "$URL" --user svc        >/dev/null
cpdctl config profile use cp4d                                >/dev/null
echo "cpdctl authenticated to $URL as $USR"
