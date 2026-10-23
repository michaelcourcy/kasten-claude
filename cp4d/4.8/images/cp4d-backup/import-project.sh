#!/usr/bin/env bash
# Import a CP4D project from a restored backup PVC (mounted at /backup) into an
# EXISTING target project. The target project must already be created by the caller
# (restore.sh handles create + the delete-and-replace confirmation).
set -euo pipefail
cpdctl-login.sh
: "${TARGET_PID:?TARGET_PID (target project guid) is required}"
: "${IMPORT_DIR:=/backup/current}"
: "${ENCRYPTION_KEY:=}"     # must match the key used at export time, if any

[ -d "$IMPORT_DIR" ] || { echo "no bundle at $IMPORT_DIR"; exit 1; }
echo "importing $(find "$IMPORT_DIR" -type f | wc -l) files from $IMPORT_DIR into project $TARGET_PID"
ENC=()
[ -n "$ENCRYPTION_KEY" ] && ENC=(--encryption-key "$ENCRYPTION_KEY")
cpdctl asset import start --project-id "$TARGET_PID" --import-dir "$IMPORT_DIR" \
  "${ENC[@]}" | grep -iE 'State:|OK'
echo "import complete into $TARGET_PID"
