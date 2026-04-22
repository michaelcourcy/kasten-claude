# Kasten Blueprint — Percona Server for MongoDB (PSMDB) Operator

Backup and restore for MongoDB clusters managed by the
[Percona Operator for MongoDB](https://docs.percona.com/percona-operator-for-mongodb/).

## When this blueprint is needed — and when it is not

> **TL;DR — do not deploy this blueprint if your storage driver guarantees crash-consistent snapshots.**

Modern CSI drivers (AWS EBS, GCE PD, Azure Disk, Ceph RBD, and most enterprise SANs) capture
a point-in-time block image of the volume. MongoDB's WiredTiger storage engine uses a
write-ahead journal (WAL) stored on the same volume. On restart after a crash-consistent
snapshot, WiredTiger replays the journal and brings the storage engine to a fully consistent
state — exactly as if the server had been powered off suddenly. This is the standard cloud
backup method for MongoDB and is explicitly documented:
https://www.mongodb.com/docs/manual/tutorial/backup-with-filesystem-snapshots/

**If your storage is crash-consistent, `fsyncLock()` and this blueprint add nothing.**
Do not deploy the blueprint or the BlueprintBinding. Let Kasten snapshot the three PVCs
directly — WiredTiger handles recovery automatically.

**This blueprint is necessary when your storage backend does NOT guarantee crash-consistent
snapshots.** Some older NFS-backed, CIFS, or certain SAN/NAS implementations capture blocks
from different points in time within the same volume ("fuzzy" snapshot). WiredTiger cannot
reliably replay a journal whose blocks span multiple points in time, so MongoDB may fail to
start from a fuzzy snapshot.

`fsyncLock()` on a secondary flushes all pending writes to disk and halts new I/O before
Kasten takes the snapshot. The result is a perfectly clean volume image that does not require
any journal replay — safe on any storage backend.

---

## Backup/Restore pattern

**Pattern 1 — Fence and quiesce a secondary replica.**

`backupPrehook` dynamically identifies a secondary member each time, runs `db.fsyncLock()`
on it, and labels both the pod and the PVC. Kasten snapshots **all three PVCs** (the quiesced
secondary provides a guaranteed clean anchor; the other two are also captured but discarded on
restore). `backupPosthook` unlocks the secondary.

On restore, `restorePosthook` **immediately** pauses the cluster (before WiredTiger activates
on the non-quiesced PVCs), deletes all PVCs except the quiesced one, and resumes. The PSMDB
operator creates fresh empty PVCs for the deleted members; they perform MongoDB initial sync
from the quiesced node, which becomes the new primary.

Zero primary impact: writes continue normally while the secondary is frozen.

### Why three PVCs are snapshotted but only one is used for restore

The quiesced secondary's PVC is the sole authoritative restore source. The other two snapshots
are taken alongside it because Kasten snapshots all PVCs it discovers at backup time — they
are present in the restore point but discarded by `restorePosthook`. This is intentional:
discarding non-quiesced PVCs avoids any risk of fuzzy-snapshot data contaminating the cluster
on non-crash-consistent storage.

---

## Blueprint actions

| Action | Function | What it does |
|---|---|---|
| `backupPrehook` | KubeTask | Removes any stale `kasten.io/psmdb-quiesced` label, picks a secondary (highest ordinal first), calls `db.fsyncLock()`, labels the pod (`kasten.io/psmdb-fsync-locked`) and the PVC (`kasten.io/psmdb-quiesced`), emits quiesced PVC name as a restore-point artifact |
| `backupPosthook` | KubeTask | Finds the labeled pod, calls `db.fsyncUnlock()`, removes the pod label |
| `restorePrehook` | KubeTask | Pauses the cluster before PVC restore (**not yet triggered in Kasten ≤ 8.5.x** — `restorePosthook` handles this automatically) |
| `restorePosthook` | KubeTask + WaitV2 | Immediately pauses cluster, deletes non-quiesced PVCs, resumes, waits for `.status.state == "ready"` |

---

## Versions

| Component | Version |
|---|---|
| Kubernetes | `1.32.9` (EKS) |
| Kasten | `8.5.4` |
| PSMDB Operator / Helm chart | `1.22.0` |
| Percona Server for MongoDB | `8.0.19-7` |

---

## Prerequisites

### Operator namespace separation

Deploy the PSMDB operator in a dedicated namespace and workload clusters in separate
namespaces. This keeps operator lifecycle (GitOps / ArgoCD) independent of application
data managed by Kasten.

```bash
helm repo add percona https://percona.github.io/percona-helm-charts/
helm repo update

helm upgrade --install psmdb-operator percona/psmdb-operator \
  --namespace psmdb-operator --create-namespace \
  --version 1.22.0 \
  --set watchAllNamespaces=true \
  --wait
```

### Custom tool image

The blueprint uses `michaelcourcy/kasten-tools:8.5.2`, which adds `kubectl` to
`gcr.io/kasten-images/kanister-tools:8.5.2` (the base image ships without `kubectl`).
KubeTask phases that call `kubectl` must run in the `kasten-io` namespace for RBAC.

---

## Deploy the workload

```bash
kubectl create namespace psmdb-test

helm upgrade --install my-psmdb percona/psmdb-db \
  --namespace psmdb-test \
  --version 1.22.2 \
  --set replsets.rs0.size=3 \
  --set replsets.rs0.resources.requests.cpu=300m \
  --set replsets.rs0.resources.requests.memory=512Mi \
  --set replsets.rs0.resources.limits.cpu=500m \
  --set replsets.rs0.resources.limits.memory=1Gi \
  --set replsets.rs0.volumeSpec.pvc.storageClassName=ebs-sc \
  --set replsets.rs0.volumeSpec.pvc.resources.requests.storage=5Gi \
  --set pmm.enabled=false \
  --set backup.enabled=false \
  --set sharding.enabled=false \
  --wait --timeout=10m
```

> **Storage class**: `ebs-sc` is the AWS EBS CSI storage class used in our test environment.
> Replace it with a storage class that supports CSI snapshots on your cluster
> (e.g. `managed-csi` on AKS, `standard-rwo` on GKE, your custom class on bare-metal, etc.).
> The class must have a matching `VolumeSnapshotClass` registered with Kasten.
> Do **not** use legacy in-tree classes (e.g. `gp2` on AWS) — they do not support CSI snapshots.

Wait for the cluster to become ready:

```bash
kubectl get psmdb -n psmdb-test -w
# STATUS column should reach: ready
```

### Create test data

```bash
DBADMIN_USER=$(kubectl -n psmdb-test get secret my-psmdb-psmdb-db-secrets \
  -o jsonpath="{.data.MONGODB_DATABASE_ADMIN_USER}" | base64 -d)
DBADMIN_PASS=$(kubectl -n psmdb-test get secret my-psmdb-psmdb-db-secrets \
  -o jsonpath="{.data.MONGODB_DATABASE_ADMIN_PASSWORD}" | base64 -d)

kubectl exec -n psmdb-test my-psmdb-psmdb-db-rs0-0 -c mongod -- \
  mongosh "mongodb://localhost:27017/admin" -u "$DBADMIN_USER" -p "$DBADMIN_PASS" \
  --quiet --eval '
db.getSiblingDB("testdb").customers.insertMany([
  {name:"Alice",email:"alice@example.com",plan:"gold"},
  {name:"Bob",email:"bob@example.com",plan:"silver"},
  {name:"Carol",email:"carol@example.com",plan:"gold"}
]);
print("count:", db.getSiblingDB("testdb").customers.countDocuments());
'
```

### Verify test data

```bash
kubectl exec -n psmdb-test my-psmdb-psmdb-db-rs0-0 -c mongod -- \
  mongosh "mongodb://localhost:27017/admin" -u "$DBADMIN_USER" -p "$DBADMIN_PASS" \
  --quiet --eval '
var docs = db.getSiblingDB("testdb").customers.find().toArray();
print("count:", docs.length);
docs.forEach(d => print(d.name, d.email, d.plan));
'
```

### Delete the workload

```bash
helm uninstall my-psmdb -n psmdb-test
kubectl delete namespace psmdb-test
kubectl delete restorepointcontent -l k10.kasten.io/appNamespace=psmdb-test
```

---

## Deploy the blueprint

```bash
kubectl apply -f blueprint.yaml
kubectl apply -f blueprintbinding.yaml
```

---

## ⚠️ restorePrehook not yet triggered in Kasten ≤ 8.5.x

The `restorePrehook` blueprint action is **never called** in Kasten ≤ 8.5.x
(confirmed by Kasten engineering — fix is in progress).

`restorePrehook` is designed to pause the cluster before Kasten replaces PVCs, so that
running MongoDB pods do not hold the volumes open during `restoreVolumes`.

**No manual pre-restore steps are required.** `restorePosthook` handles the full cleanup
sequence automatically:

1. Immediately pauses the cluster on entry (before WiredTiger finishes starting).
2. Identifies the quiesced PVC via the `kasten.io/psmdb-quiesced=true` label (or artifact fallback).
3. Deletes all other PVCs.
4. Resumes the cluster.
5. Waits for readiness.

Because MongoDB pods take 30–120 seconds to initialize, `restorePosthook` executes
well within that window. The risk of WiredTiger activating on a fuzzy PVC before
`restorePosthook` runs is negligible in practice.

Once `restorePrehook` is fixed in a future Kasten release, it will provide a cleaner
separation: the pause happens before `restoreVolumes`, and `restorePosthook` only
needs to delete non-quiesced PVCs and wait for readiness.

---

## Backup policy

Create a backup policy targeting the `psmdb-test` namespace with a **Location** profile
(required for Kanister phases). No PVC filtering configuration is needed.

### How the quiesced PVC is tracked across backup and restore

`backupPrehook` uses two mechanisms in tandem:

1. **PVC label** `kasten.io/psmdb-quiesced=true`: applied to the chosen PVC before Kasten
   takes the snapshot. Kasten preserves PVC labels when restoring a PVC from a snapshot,
   so this label survives into the restored PVC and is the primary identifier used by
   `restorePosthook`.

2. **Kanister artifact** `psmdbMeta.quiescedPVC`: written via `kando output` and stored in
   the Kasten restore point alongside the PVC snapshot. Used as a fallback if the PVC label
   is ever absent.

At the start of every `backupPrehook`, all existing `kasten.io/psmdb-quiesced` labels are
removed before a new secondary is chosen. This prevents stale labels accumulating after a
replica-set failover changes which member is the secondary.

### Verify the backup hooks ran

```bash
kubectl logs -n kasten-io -l component=executor --tail=10000 | \
  grep -E "quiesceSecondary|unquiesceSecondary|fsyncLock|fsyncUnlock|Quiesced PVC"
```

---

## Restore

No manual preparation is required. Trigger the restore normally via the Kasten UI or:

```bash
# List available restore points
kubectl get restorepoint -n psmdb-test

kubectl create -f - --validate=false <<EOF
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RestoreAction
metadata:
  generateName: restore-
  namespace: psmdb-test
spec:
  subject:
    apiVersion: apps.kio.kasten.io/v1alpha1
    kind: RestorePoint
    name: <RESTORE_POINT_NAME>
    namespace: psmdb-test
  targetNamespace: psmdb-test
  profile:
    name: <LOCATION_PROFILE_NAME>
    namespace: kasten-io
EOF
```

`restorePosthook` will automatically:
1. Pause the cluster.
2. Delete non-quiesced PVCs (identified by `kasten.io/psmdb-quiesced=true` label or artifact).
3. Resume — the quiesced node becomes primary; others sync from it via initial sync.
4. Wait until `.status.state == "ready"`.

### Verify restore

```bash
DBADMIN_USER=$(kubectl -n psmdb-test get secret my-psmdb-psmdb-db-secrets \
  -o jsonpath="{.data.MONGODB_DATABASE_ADMIN_USER}" | base64 -d)
DBADMIN_PASS=$(kubectl -n psmdb-test get secret my-psmdb-psmdb-db-secrets \
  -o jsonpath="{.data.MONGODB_DATABASE_ADMIN_PASSWORD}" | base64 -d)

kubectl exec -n psmdb-test my-psmdb-psmdb-db-rs0-0 -c mongod -- \
  mongosh "mongodb://localhost:27017/admin" -u "$DBADMIN_USER" -p "$DBADMIN_PASS" \
  --quiet --eval '
var docs = db.getSiblingDB("testdb").customers.find().toArray();
print("count:", docs.length);
docs.forEach(d => print(d.name, d.email, d.plan));
'
```
