# Couchbase (Autonomous Operator) — Kasten Blueprint

## Tested versions

| Component | Version |
|---|---|
| Kubernetes | 1.32 (EKS) |
| Kasten | 8.5.2 |
| Couchbase Autonomous Operator (Helm chart) | 2.7.1 |
| Couchbase Server | 7.6.6 |

---

## Pattern: Logical dump on a temporary PVC (Pattern 4)

Couchbase stores its data in Couchstore and Magma file formats across multiple vBuckets.
A raw filesystem (CSI) snapshot of Couchbase data PVCs without a prior coordinated flush is
**not crash-consistent** — Couchbase may start from inconsistent state and trigger rebalancing
or data loss. Therefore, a logical backup using `cbbackupmgr` is required.

**Why Pattern 4, not Pattern 2 (Quiesce)?**
Couchbase does not provide a single quiesce/freeze API that guarantees filesystem-level
consistency across all vBuckets and all nodes. Quiescing Couchbase for a filesystem snapshot
would require pausing all I/O at the OS layer, which is not supported without disrupting service.

**Why Pattern 4, not Pattern 10 (Vendor operator data mover)?**
The Couchbase operator's `CouchbaseBackup` CRD lifecycle (creating a PVC, running a CronJob,
managing retentions) is designed for scheduled backup workflows, not for integration with
Kasten's snapshot-based restore points. Using `cbbackupmgr` directly in a KubeTask avoids
the state-management complexity of the `CouchbaseBackup` CRD.

**Pattern 4 chosen**: `cbbackupmgr backup --full-backup` dumps all bucket data to a temporary
PVC each time. Kasten CSI-snapshots **all** PVCs in the namespace (data PVCs + dump PVC).
On restore, Kasten restores all PVCs from their CSI snapshots (including the dump PVC), then
`cbbackupmgr restore` replays the dump into Couchbase, guaranteeing data consistency.

Kasten is the data mover (owns immutability via CSI snapshots). `cbbackupmgr` provides the
consistent logical view of the data at backup time.

---

## Blueprint actions

| Action | What it does |
|---|---|
| `backupPrehook` | Deletes any leftover backup PVC from a previous failed run (idempotency); creates a new `<cluster>-kasten-backup` PVC; runs `cbbackupmgr config` to initialize the archive; runs `cbbackupmgr backup --full-backup` to dump all Couchbase data |
| `backupPosthook` | Deletes the temporary backup PVC (already captured in the restore point by Kasten) |
| `restorePrehook` | Patches the `CouchbaseCluster` with `spec.paused: true` to stop operator reconciliation; scales down all Couchbase StatefulSets so Kasten can safely replace PVC contents |
| `restorePosthook` | Unpauses the `CouchbaseCluster`; waits for the cluster to reach `Running` phase; runs `cbbackupmgr restore` from the restored dump PVC; deletes the dump PVC |

---

## Known limitation — `restorePrehook` not yet triggered (Kasten ≤ 8.5.x)

> **Warning**: As of Kasten 8.5.x, the `restorePrehook` blueprint action is defined but
> **never triggered** by the Kasten executor during restore. Kasten engineering has confirmed
> this will be fixed in a near-future release. The `restorePrehook` is implemented in the
> blueprint so it will work automatically once the fix ships.
>
> Until then, **run the steps below manually before triggering a Kasten restore**.
> Failing to do so will leave the Couchbase operator running while Kasten tries to replace
> the PVCs. The operator will immediately recreate pods, blocking PVC replacement and causing
> the restore to fail.

### Manual pre-restore steps (required until the fix ships)

Replace `couchbase-test` and `cb-example` with your actual namespace and cluster name:

```bash
# Pause the Couchbase operator reconciliation for this cluster
kubectl patch couchbasecluster cb-example -n couchbase-test \
  --type merge -p '{"spec":{"paused":true}}'

# Scale down all Couchbase StatefulSets to release PVCs
kubectl get statefulset -n couchbase-test \
  -l "couchbase_cluster=cb-example" \
  -o jsonpath='{.items[*].metadata.name}' | \
  tr ' ' '\n' | \
  xargs -I{} kubectl scale statefulset {} -n couchbase-test --replicas=0

# Wait for all Couchbase pods to terminate before triggering the Kasten restore
kubectl wait pod -n couchbase-test \
  -l "couchbase_cluster=cb-example" \
  --for=delete --timeout=300s 2>/dev/null || true

echo "Ready — trigger the Kasten restore now."
```

Once these commands complete, trigger the Kasten restore. The `restorePosthook` will
unpause the cluster and run `cbbackupmgr restore` automatically.

---

## Prerequisites

### Couchbase Autonomous Operator

Install the operator via Helm. The operator and admission controller are deployed in a
dedicated namespace; the CouchbaseCluster is deployed in a separate test namespace.

```bash
helm repo add couchbase https://couchbase-partners.github.io/helm-charts/
helm repo update

# Install operator and admission controller
helm install couchbase-operator couchbase/couchbase-operator \
  --namespace couchbase-operator \
  --create-namespace \
  --set install.admissionController=true

kubectl -n couchbase-operator wait deploy \
  --all --for=condition=Available --timeout=120s
```

### Custom tool image

The blueprint uses `michaelcourcy/couchbase-backup-tool:7.6.6`, which adds `kubectl` and `jq`
to `couchbase/server:7.6.6` (which contains `cbbackupmgr` at `/opt/couchbase/bin/cbbackupmgr`).
See [images/couchbase-backup-tool/Dockerfile](images/couchbase-backup-tool/Dockerfile).

```bash
cd images/couchbase-backup-tool
docker buildx build --platform linux/amd64 \
  -t michaelcourcy/couchbase-backup-tool:7.6.6 \
  --push .
```

---

## Step 2 — Deploy the workload and create test data

### Deploy

```bash
kubectl create namespace couchbase-test

# Admin credentials secret
kubectl apply -f operator/cb-example-auth.yaml

# Couchbase cluster and bucket
kubectl apply -f operator/couchbasebucket.yaml
kubectl apply -f operator/couchbasecluster.yaml

# Wait for the cluster to be Running (3–5 minutes for EKS)
kubectl -n couchbase-test get couchbasecluster cb-example -w
# Wait until PHASE=Running
```

### Create test data

```bash
# Port-forward the Couchbase REST API
kubectl port-forward -n couchbase-test svc/cb-example 8091:8091 &
PF_PID=$!
sleep 3

# Insert 3 documents into the default bucket via the REST API
for i in 1 2 3; do
  curl -s -u "Administrator:password" -X POST \
    "http://localhost:8091/pools/default/buckets/default/docs/kasten-doc-${i}" \
    -H "Content-Type: application/json" \
    -d "{\"id\": ${i}, \"message\": \"kasten-backup-test-doc-${i}\"}"
  echo
done

# Verify — expect 3 documents listed
curl -s -u "Administrator:password" \
  "http://localhost:8091/pools/default/buckets/default/docs?limit=10" | \
  python3 -c "import sys, json; docs=json.load(sys.stdin); print(f'Document count: {len(docs[\"rows\"])}')"

kill $PF_PID
```

### Teardown

```bash
kubectl delete namespace couchbase-test

kubectl delete -f blueprintbinding.yaml
kubectl delete -f blueprint.yaml

kubectl delete restorepointcontent \
  -l k10.kasten.io/appNamespace=couchbase-test

# Uninstall operator (if no longer needed)
helm uninstall couchbase-operator -n couchbase-operator
kubectl delete namespace couchbase-operator
# CRDs remain cluster-wide; remove if desired:
kubectl get crd | grep couchbase.com | awk '{print $1}' | xargs kubectl delete crd
```

---

## Step 3 — Validate strategy manually (without blueprint)

### Run cbbackupmgr backup manually

```bash
# Create backup PVC
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: cb-example-kasten-backup
  namespace: couchbase-test
spec:
  accessModes: ["ReadWriteOnce"]
  storageClassName: ebs-sc
  resources:
    requests:
      storage: 10Gi
EOF

kubectl wait pvc cb-example-kasten-backup -n couchbase-test \
  --for=jsonpath='{.status.phase}'=Bound --timeout=60s

# Run backup in a pod
kubectl run -n couchbase-test kasten-backup-test \
  --image=michaelcourcy/couchbase-backup-tool:7.6.6 \
  --restart=Never \
  --overrides='{"spec":{"volumes":[{"name":"backup","persistentVolumeClaim":{"claimName":"cb-example-kasten-backup"}}],"containers":[{"name":"kasten-backup-test","image":"michaelcourcy/couchbase-backup-tool:7.6.6","command":["bash","-c","cbbackupmgr config --archive /backup --repo kasten-repo 2>/dev/null || true && cbbackupmgr backup --archive /backup --repo kasten-repo --cluster http://cb-example.couchbase-test.svc:8091 --username Administrator --password password --full-backup --no-progress-bar && cbbackupmgr info --archive /backup --repo kasten-repo"],"volumeMounts":[{"name":"backup","mountPath":"/backup"}]}]}}' \
  --rm -it -- bash 2>/dev/null || \
kubectl run -n couchbase-test kasten-backup-test \
  --image=michaelcourcy/couchbase-backup-tool:7.6.6 \
  --restart=Never \
  --overrides='{"spec":{"volumes":[{"name":"backup","persistentVolumeClaim":{"claimName":"cb-example-kasten-backup"}}],"containers":[{"name":"kasten-backup-test","image":"michaelcourcy/couchbase-backup-tool:7.6.6","command":["bash","-c","cbbackupmgr config --archive /backup --repo kasten-repo 2>/dev/null || true && cbbackupmgr backup --archive /backup --repo kasten-repo --cluster http://cb-example.couchbase-test.svc:8091 --username Administrator --password password --full-backup --no-progress-bar && cbbackupmgr info --archive /backup --repo kasten-repo"],"volumeMounts":[{"name":"backup","mountPath":"/backup"}]}]}}'
kubectl logs -n couchbase-test kasten-backup-test -f
kubectl delete pod -n couchbase-test kasten-backup-test --ignore-not-found
```

### Emulate a CSI snapshot of the backup PVC

```bash
kubectl apply -f - <<'EOF'
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: cb-kasten-backup-manual-test
  namespace: couchbase-test
spec:
  volumeSnapshotClassName: ebs-snapshot-class
  source:
    persistentVolumeClaimName: cb-example-kasten-backup
EOF

kubectl wait volumesnapshot/cb-kasten-backup-manual-test \
  -n couchbase-test --for=jsonpath='{.status.readyToUse}'=true --timeout=120s
echo "Snapshot ready"
```

### Validate restore

```bash
# Delete test documents to simulate data loss
kubectl port-forward -n couchbase-test svc/cb-example 8091:8091 &
PF_PID=$!
sleep 3

for i in 1 2 3; do
  curl -s -u "Administrator:password" -X DELETE \
    "http://localhost:8091/pools/default/buckets/default/docs/kasten-doc-${i}"
  echo
done

# Verify data is gone
curl -s -u "Administrator:password" \
  "http://localhost:8091/pools/default/buckets/default/docs?limit=10" | \
  python3 -c "import sys, json; docs=json.load(sys.stdin); print(f'Document count after delete: {len(docs[\"rows\"])}')"
kill $PF_PID

# Run restore from backup PVC
kubectl run -n couchbase-test kasten-restore-test \
  --image=michaelcourcy/couchbase-backup-tool:7.6.6 \
  --restart=Never \
  --overrides='{"spec":{"volumes":[{"name":"backup","persistentVolumeClaim":{"claimName":"cb-example-kasten-backup"}}],"containers":[{"name":"kasten-restore-test","image":"michaelcourcy/couchbase-backup-tool:7.6.6","command":["bash","-c","cbbackupmgr restore --archive /backup --repo kasten-repo --cluster http://cb-example.couchbase-test.svc:8091 --username Administrator --password password --start oldest --end latest --no-progress-bar --force-updates"],"volumeMounts":[{"name":"backup","mountPath":"/backup"}]}]}}'
kubectl logs -n couchbase-test kasten-restore-test -f
kubectl delete pod -n couchbase-test kasten-restore-test --ignore-not-found

# Verify data is restored
kubectl port-forward -n couchbase-test svc/cb-example 8091:8091 &
PF_PID=$!
sleep 3
curl -s -u "Administrator:password" \
  "http://localhost:8091/pools/default/buckets/default/docs?limit=10" | \
  python3 -c "import sys, json; docs=json.load(sys.stdin); print(f'Document count after restore: {len(docs[\"rows\"])}')"
kill $PF_PID
```

Expected: document count returns to 3.

### Cleanup manual test

```bash
kubectl delete volumesnapshot cb-kasten-backup-manual-test -n couchbase-test --ignore-not-found
kubectl delete pvc cb-example-kasten-backup -n couchbase-test --ignore-not-found
```

---

## Step 4 — Deploy the blueprint

```bash
kubectl apply -f blueprint.yaml
kubectl apply -f blueprintbinding.yaml
```

---

## Step 5 — Test with Kasten

1. Create a backup policy on the `couchbase-test` namespace with a location profile.
2. Run an on-demand backup. Check kanister-svc logs for `backupPrehook`/`backupPosthook`.
3. Delete the test documents (simulate data loss):
   ```bash
   kubectl port-forward -n couchbase-test svc/cb-example 8091:8091 &
   for i in 1 2 3; do
     curl -s -u "Administrator:password" -X DELETE \
       "http://localhost:8091/pools/default/buckets/default/docs/kasten-doc-${i}"
   done
   kill %1
   ```
4. Run the manual pre-restore steps (required until the restorePrehook bug is fixed — see above).
5. Restore from the restore point. Check executor logs for `restorePosthook`.
6. Verify the restored data:
   ```bash
   kubectl port-forward -n couchbase-test svc/cb-example 8091:8091 &
   curl -s -u "Administrator:password" \
     "http://localhost:8091/pools/default/buckets/default/docs?limit=10" | \
     python3 -c "import sys, json; docs=json.load(sys.stdin); print(f'Count: {len(docs[\"rows\"])}')"
   kill %1
   ```
   Expected: 3 documents.

```bash
# Debug logs
kubectl logs -n kasten-io -l app=kanister-svc --tail=200
kubectl logs -n kasten-io -l component=executor --tail=10000 -f
```
