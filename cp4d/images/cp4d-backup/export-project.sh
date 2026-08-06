#!/usr/bin/env bash
# Export ONE CP4D project into the per-project backup PVC (mounted at /backup).
# Stores the bundle UNZIPPED (so CSI block-level incremental dedup works) and
# writes it atomically to /backup/current (a killed pod never leaves a half-written
# tree that could be snapshotted).
set -euo pipefail
cpdctl-login.sh
: "${PID:?PID (project guid) is required}"
: "${PROJECT_NAME:?PROJECT_NAME is required}"
: "${ENCRYPTION_KEY:=}"     # optional; empty => plaintext bundle (V1 default, see README §security)

echo "exporting project $PID ($PROJECT_NAME)"
ENC=()
[ -n "$ENCRYPTION_KEY" ] && ENC=(--encryption-key "$ENCRYPTION_KEY")
cpdctl asset export start --project-id "$PID" --assets-all-assets \
  "${ENC[@]}" --name "kasten-${PID}" --output-file /tmp/export.zip | grep -iE 'State:|OK'

rm -rf /backup/.staging && mkdir -p /backup/.staging
unzip -q /tmp/export.zip -d /backup/.staging
printf '%s' "$PROJECT_NAME" > /backup/.staging/.cp4d-project-name
printf '%s' "$PID"          > /backup/.staging/.cp4d-project-id
# atomic swap
rm -rf /backup/previous 2>/dev/null || true
[ -d /backup/current ] && mv /backup/current /backup/previous
mv /backup/.staging /backup/current
rm -rf /backup/previous 2>/dev/null || true
echo "export complete for $PID: $(find /backup/current -type f | wc -l) files on PVC"
