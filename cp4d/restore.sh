#!/usr/bin/env bash
#
# restore.sh — recreate a CP4D project from a restored backup PVC.
#
#   ./restore.sh <namespace> <pvc> [project-name]
#
#   <namespace>      namespace where Kasten has ALREADY restored the backup PVC
#   <pvc>            name of that restored PVC (e.g. cp4d-my-new-project-21a0ff72)
#   [project-name]   optional target CP4D project name. If omitted, the original
#                    name stored in the bundle is used.
#
# Kasten's job is only to put the PVC into <namespace>. This script does the CP4D
# import: it creates the target project and imports the bundle from the PVC.
#
# If a project with the target name already EXISTS, the matching project(s) are
# listed and the operator must type the exact name to confirm delete-and-replace.
#
# Requirements on the machine running this: kubectl (cluster access) + the cpdctl
# binary (default ./bin/cpdctl). Credentials are read from the cpd secret you have
# access to and fed to the import pod via env (never hardcoded).
set -euo pipefail

NS="${1:?usage: restore.sh <namespace> <pvc> [project-name]}"
PVC="${2:?usage: restore.sh <namespace> <pvc> [project-name]}"
WANT_NAME="${3:-}"

CRED_NS="${CRED_NS:-cpd}"
CRED_SECRET="${CRED_SECRET:-cp4d-backup-cpdctl-creds}"
IMAGE="${IMAGE:-docker.io/michaelcourcy/cp4d-backup:1.8.244-3}"
ENCRYPTION_KEY="${ENCRYPTION_KEY:-}"
CPDCTL="${CPDCTL:-$(dirname "$0")/bin/cpdctl}"

# --- source CP4D credentials (admin has read on the cpd secret) ---
CPD_URL=$(kubectl get secret -n "$CRED_NS" "$CRED_SECRET" -o jsonpath='{.data.url}'      | base64 -d)
CPD_USER=$(kubectl get secret -n "$CRED_NS" "$CRED_SECRET" -o jsonpath='{.data.username}' | base64 -d)
CPD_APIKEY=$(kubectl get secret -n "$CRED_NS" "$CRED_SECRET" -o jsonpath='{.data.apikey}'   | base64 -d)
CPDTMP="$(mktemp -d)"; export CPDCONFIG="$CPDTMP/config.json"   # dir exists, file created by cpdctl
trap 'rm -rf "$CPDTMP"' EXIT
"$CPDCTL" config user set svc --username "$CPD_USER" --apikey "$CPD_APIKEY" >/dev/null
"$CPDCTL" config profile set cp4d --url "$CPD_URL" --user svc >/dev/null
"$CPDCTL" config profile use cp4d >/dev/null

# --- sanity: PVC exists in NS ---
kubectl get pvc "$PVC" -n "$NS" >/dev/null || { echo "PVC $PVC not found in namespace $NS"; exit 1; }

# --- determine target project name: arg, else peek the bundle on the PVC ---
if [ -z "$WANT_NAME" ]; then
  echo "peeking project name from the restored PVC ..."
  POD="cp4d-restore-peek-$$"
  kubectl run "$POD" -n "$NS" --restart=Never --image="$IMAGE" --command \
    --overrides='{"spec":{"volumes":[{"name":"b","persistentVolumeClaim":{"claimName":"'"$PVC"'"}}],"containers":[{"name":"peek","image":"'"$IMAGE"'","command":["cat","/backup/current/.cp4d-project-name"],"volumeMounts":[{"name":"b","mountPath":"/backup"}]}]}}' >/dev/null
  kubectl wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$POD" -n "$NS" --timeout=120s >/dev/null 2>&1 || true
  WANT_NAME=$(kubectl logs "$POD" -n "$NS" 2>/dev/null)
  kubectl delete pod "$POD" -n "$NS" --ignore-not-found >/dev/null 2>&1
  [ -n "$WANT_NAME" ] || { echo "could not read project name from PVC"; exit 1; }
fi
echo "target project name: '$WANT_NAME'"

# --- delete-and-replace confirmation if a project with that name exists ---
EXISTING=$("$CPDCTL" project list --name "$WANT_NAME" --match exact --output json 2>/dev/null \
            | jq -r '.resources[]? | [.metadata.guid, .entity.name] | @tsv')
if [ -n "$EXISTING" ]; then
  echo
  echo "!! A project named '$WANT_NAME' already exists:"
  echo "$EXISTING" | sed 's/^/     /'
  echo
  echo "Proceeding will DELETE the project(s) above and replace with the backup."
  printf "Type the project name exactly to confirm delete-and-replace: "
  read -r CONFIRM
  [ "$CONFIRM" = "$WANT_NAME" ] || { echo "confirmation did not match; aborting."; exit 1; }
  while IFS=$'\t' read -r guid name; do
    [ -z "$guid" ] && continue
    echo "deleting existing project $name ($guid) ..."
    "$CPDCTL" project delete --project-id "$guid" >/dev/null
  done <<< "$EXISTING"
fi

# --- create the target project ---
echo "creating target project '$WANT_NAME' ..."
"$CPDCTL" project create --name "$WANT_NAME" --type cpd --storage-type assetfiles > /tmp/restore-create.$$ 2>&1 || { cat /tmp/restore-create.$$; exit 1; }
NEWPID=$(grep -oE '/v2/projects/[a-f0-9-]+' /tmp/restore-create.$$ | head -1 | sed 's#.*/##')
rm -f /tmp/restore-create.$$
[ -n "$NEWPID" ] || { echo "failed to create target project"; exit 1; }
echo "created project $NEWPID"

# --- import the bundle from the PVC via a pod (only a pod can mount the PVC) ---
POD="cp4d-restore-import-$$"
kubectl delete pod "$POD" -n "$NS" --ignore-not-found >/dev/null 2>&1
cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: $POD
  namespace: $NS
spec:
  restartPolicy: Never
  containers:
    - name: import
      image: $IMAGE
      imagePullPolicy: Always
      command: ["import-project.sh"]
      env:
        - { name: TARGET_PID,     value: "$NEWPID" }
        - { name: ENCRYPTION_KEY, value: "$ENCRYPTION_KEY" }
        - { name: CPD_URL,        value: "$CPD_URL" }
        - { name: CPD_USERNAME,   value: "$CPD_USER" }
        - { name: CPD_APIKEY,     value: "$CPD_APIKEY" }
      volumeMounts: [ { name: backup, mountPath: /backup } ]
  volumes:
    - name: backup
      persistentVolumeClaim: { claimName: $PVC }
EOF
echo "importing (pod $POD) ..."
kubectl wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$POD" -n "$NS" --timeout=600s >/dev/null 2>&1 || true
kubectl logs "$POD" -n "$NS" 2>/dev/null | sed 's/^/    /'
PH=$(kubectl get pod "$POD" -n "$NS" -o jsonpath='{.status.phase}' 2>/dev/null || echo Unknown)
kubectl delete pod "$POD" -n "$NS" --ignore-not-found >/dev/null 2>&1
[ "$PH" = Succeeded ] || { echo "import pod ended in phase $PH"; exit 1; }

echo
echo "DONE — project '$WANT_NAME' ($NEWPID) restored into CP4D from PVC $PVC."
