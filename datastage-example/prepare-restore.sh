#!/usr/bin/env bash
#
# Prepare DataStage for an in-place Kasten restore, and BLOCK until it is genuinely safe.
#
# Run this instead of patching ignoreForMaintenance by hand. Patching and waiting are one
# operation here on purpose: if they are two, there is a window in which you can park the
# operator and launch the RestoreAction before the operator has actually stopped reconciling,
# and it will then fight Kasten over the very PVCs being replaced.
#
#   ./prepare-restore.sh [RESTORE_POINT_NAME]
#   NS=my-cpd-namespace ./prepare-restore.sh scheduled-4jmdc
#
# Exits non-zero and explains itself if the restore should not be launched. Safe to re-run.
#
set -uo pipefail

NS="${NS:-cpd}"
RP="${1:-}"
LABEL="blueprint-ai/datastage-engine=true"

# IBM's own numbers. datastage-maint-aux-qu-cm gives enable-maint timeout 1800s for the
# DataStage CR and 600s for PXRuntime; we use the larger for both.
TIMEOUT_SECS=1800

echo "1/5  parking the operator (ignoreForMaintenance=true) in namespace ${NS}"
kubectl patch datastage datastage -n "${NS}" --type=merge \
  -p '{"spec":{"ignoreForMaintenance":true}}' >/dev/null || exit 1
for px in $(kubectl get pxruntime -n "${NS}" -o jsonpath='{.items[*].metadata.name}'); do
  kubectl patch pxruntime "${px}" -n "${NS}" --type=merge \
    -p '{"spec":{"ignoreForMaintenance":true}}' >/dev/null || exit 1
done

# The gate. IBM's enable-maint builtin polls this same field (params.statusFieldName: dsStatus),
# so this is not a heuristic — it is the condition IBM itself waits for. There is no useful
# fixed delay to recommend: on an idle engine this returns in seconds, mid-reconcile it does not.
echo "2/5  waiting for dsStatus=InMaintenance (IBM's own gate, timeout ${TIMEOUT_SECS}s)"
deadline=$(( $(date +%s) + TIMEOUT_SECS ))
wait_status() {   # $1=kind  $2=name  $3=wanted value
  while :; do
    s=$(kubectl get "$1" "$2" -n "${NS}" -o jsonpath='{.status.dsStatus}' 2>/dev/null)
    if [ "${s}" = "$3" ]; then echo "       $1/$2 = $3"; return 0; fi
    if [ "$(date +%s)" -gt "${deadline}" ]; then
      echo "       TIMEOUT: $1/$2 = ${s:-<none>} (wanted $3)"; return 1
    fi
    sleep 5
  done
}
wait_status datastage datastage InMaintenance || exit 1
for px in $(kubectl get pxruntime -n "${NS}" -o jsonpath='{.items[*].metadata.name}'); do
  wait_status pxruntime "${px}" InMaintenance || exit 1
done

# Restoring underneath a running job is worse than snapshotting one: the job keeps writing to
# volumes that are about to be replaced. backupPrehook only warns about this; here it matters more.
echo "3/5  checking for job runs in flight"
POD=$(kubectl get pods -n "${NS}" -l app.kubernetes.io/component=px-runtime \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -n "${POD}" ]; then
  n=$(kubectl exec -n "${NS}" "${POD}" -- \
        bash -c 'ls /px-storage/PXRuntime/runningJobs 2>/dev/null | wc -l' 2>/dev/null || echo 0)
  if [ "${n:-0}" -gt 0 ] 2>/dev/null; then
    echo "       WARNING: ${n} run(s) in flight — let them finish or cancel them first"
  else
    echo "       none"
  fi
else
  echo "       (no px-runtime pod found; engine already down)"
fi

# A RestoreAction whose filter matches no PVC SUCCEEDS and restores nothing. Same silent-success
# trap the backup side has, so fail loudly here instead.
echo "4/5  checking the PVC label the restore filter selects on"
PVCS=$(kubectl get pvc -n "${NS}" -l "${LABEL}" -o jsonpath='{.items[*].metadata.name}')
if [ -z "${PVCS}" ]; then
  echo "       ERROR: no PVC in ${NS} carries ${LABEL}."
  echo "              The restore would report success and replace nothing."
  echo "              See Prerequisites in README.md."
  exit 1
fi
for p in ${PVCS}; do echo "       ${p}"; done

echo "5/5  restore point"
if [ -n "${RP}" ]; then
  if kubectl get restorepoint "${RP}" -n "${NS}" >/dev/null 2>&1; then
    echo "       ${RP} found"
  else
    echo "       ERROR: restore point ${RP} not found in ${NS}"; exit 1
  fi
else
  echo "       (none given) available:"
  kubectl get restorepoint -n "${NS}" --no-headers 2>/dev/null | awk '{print "       "$1}'
fi

cat <<'NOTE'

READY — launch the RestoreAction now (Step 2).

Two things that look like failures and are not:

  * Several minutes of PVC churn. ignoreForMaintenance deliberately does NOT stop pods, so
    Kasten must scale the workloads down and wait for every mount holder to release before it
    can delete the RWX PVCs. Measured on the reference environment: about 7 minutes from
    RestoreAction start to all 11 engine pods recreated. You do not need to scale anything
    down yourself — Kasten does it, because the restore includes the workloads.

  * A plateau at ~94%. That is restorePosthook clearing maintenance and waiting for dsStatus to
    return to Completed. The action withholds success on purpose while the engine reassembles.

Verify afterwards with View data, not with the schema:

    cpdctl dsjob view-dataset --project-id <PID> --name <dataset>

describe-dataset reads its row count out of the descriptor and will report the pre-loss figure
over an empty volume. Only a live read proves the bytes came back.
NOTE
