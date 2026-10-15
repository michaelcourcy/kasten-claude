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
# To abandon the restore after this script has run, take the operator out of maintenance:
#   kubectl patch datastage datastage -n cpd --type=merge -p '{"spec":{"ignoreForMaintenance":false}}'
#   (and the same on every PXRuntime CR). The operator then scales the workloads back up itself.
#
set -uo pipefail

NS="${NS:-cpd}"
RP="${1:-}"
LABEL="blueprint-ai/datastage-engine=true"

# IBM's own numbers. datastage-maint-aux-qu-cm gives enable-maint timeout 1800s for the
# DataStage CR and 600s for PXRuntime; we use the larger for both.
TIMEOUT_SECS=1800
# How long to wait for the pods to release the PVCs after the scale-down. Measured: 30 s.
RELEASE_TIMEOUT_SECS=600

for tool in kubectl jq; do
  command -v "${tool}" >/dev/null || { echo "ERROR: ${tool} is required on this workstation"; exit 1; }
done

echo "1/6  parking the operator (ignoreForMaintenance=true) in namespace ${NS}"
kubectl patch datastage datastage -n "${NS}" --type=merge \
  -p '{"spec":{"ignoreForMaintenance":true}}' >/dev/null || exit 1
for px in $(kubectl get pxruntime -n "${NS}" -o jsonpath='{.items[*].metadata.name}'); do
  kubectl patch pxruntime "${px}" -n "${NS}" --type=merge \
    -p '{"spec":{"ignoreForMaintenance":true}}' >/dev/null || exit 1
done

# The gate. IBM's enable-maint builtin polls this same field (params.statusFieldName: dsStatus),
# so this is not a heuristic — it is the condition IBM itself waits for. There is no useful
# fixed delay to recommend: on an idle engine this returns in seconds, mid-reconcile it does not.
echo "2/6  waiting for dsStatus=InMaintenance (IBM's own gate, timeout ${TIMEOUT_SECS}s)"
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
echo "3/6  checking for job runs in flight"
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
echo "4/6  checking the PVC label the restore filter selects on"
PVCS=$(kubectl get pvc -n "${NS}" -l "${LABEL}" -o jsonpath='{.items[*].metadata.name}')
if [ -z "${PVCS}" ]; then
  echo "       ERROR: no PVC in ${NS} carries ${LABEL}."
  echo "              The restore would report success and replace nothing."
  echo "              See Prerequisites in README.md."
  exit 1
fi
for p in ${PVCS}; do echo "       ${p}"; done

echo "5/6  restore point"
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

# Kasten can only replace a PVC once no pod mounts it, and it scales down only the workloads it
# restores. The restore point contains a single workload (the runtime Deployment, see policy.yaml),
# so everything else that mounts the engine PVCs must be stopped here. These three selectors are
# IBM's: datastage-maint-aux-br-cm, aux-meta.managed-resources, the list cpdbr itself scales down
# for a restore. The operator is parked (step 2), so nothing scales them back up behind our back;
# when restorePosthook takes it out of maintenance, the operator restores the replica counts itself.
echo "6/6  scaling down the DataStage workloads (IBM's br-cm managed-resources)"
kubectl scale deployment  -n "${NS}" -l app.kubernetes.io/name=datastage        --replicas=0 >/dev/null || exit 1
kubectl scale deployment  -n "${NS}" -l app.kubernetes.io/component=px-runtime  --replicas=0 >/dev/null || exit 1
kubectl scale statefulset -n "${NS}" -l app.kubernetes.io/component=px-compute  --replicas=0 >/dev/null || exit 1

# Wait until no pod mounts a labelled PVC. A pod that keeps one mounted — typically a user volume
# also used by another service — would make the RestoreAction fail on "Failed to delete PVC".
holders() {
  kubectl get pods -n "${NS}" -o json | jq -r --arg pvcs "${PVCS}" '
    ($pvcs | split(" ")) as $want
    | .items[]
    | select([.spec.volumes[]?.persistentVolumeClaim.claimName // empty] | any(. as $c | $want | index($c)))
    | .metadata.name'
}
release_deadline=$(( $(date +%s) + RELEASE_TIMEOUT_SECS ))
while :; do
  h=$(holders)
  if [ -z "${h}" ]; then echo "       no pod mounts the labelled PVCs"; break; fi
  if [ "$(date +%s)" -gt "${release_deadline}" ]; then
    echo "       ERROR: these pods still mount a labelled PVC after ${RELEASE_TIMEOUT_SECS}s:"
    echo "${h}" | sed 's/^/         /'
    echo "              Stop them (they are not DataStage workloads), then re-run this script."
    exit 1
  fi
  sleep 5
done

cat <<'NOTE'

READY — launch the RestoreAction now (Step 2).

What to expect:

  * The volume restore itself is quick. Measured on the reference environment: about 1.5 minutes
    from RestoreAction start until Kasten reaches ~92%.

  * Then a long plateau at ~92%. That is restorePosthook taking the operator out of maintenance
    and waiting for dsStatus to return to Completed while the operator scales every workload
    back up. Measured: about 15 minutes. The action withholds success on purpose while the
    engine reassembles. Do not scale anything up yourself.

Verify afterwards with View data, not with the schema:

    cpdctl dsjob view-dataset --project-id <PID> --name <dataset>

describe-dataset reads its row count out of the descriptor and will report the pre-loss figure
over an empty volume. Only a live read proves the bytes came back.
NOTE
