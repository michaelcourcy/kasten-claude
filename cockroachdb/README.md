# CockroachDB Blueprint — Pattern 5 (local MinIO keeper)

Backup and restore a [CockroachDB](https://www.cockroachlabs.com/) cluster running on Kubernetes
with Kasten, using CockroachDB's native `BACKUP` / `RESTORE` statements writing to a local MinIO
instance whose PVC is snapshotted by Kasten.

## Why this pattern

CockroachDB is a **distributed SQL database**: data is split into ranges and replicated (Raft)
across nodes, each node holding only a partial, replicated slice on its own PVC. A per-node PVC
snapshot is **not** a consistent, restorable copy of the cluster, so Kasten cannot be the data
mover by snapshotting the CockroachDB data PVCs.

Instead this blueprint uses **Pattern 5 — database backup via a local MinIO keeper**:

- CockroachDB's transactionally-consistent `BACKUP` writes to a MinIO instance (a local S3 endpoint)
  running in the application namespace.
- Kasten snapshots **only the MinIO PVC** (the CockroachDB data PVCs are excluded via a policy
  filter). The MinIO snapshot contains a self-consistent CockroachDB backup collection.
- Incrementality comes for free: `BACKUP INTO` an existing collection produces incremental backups,
  and Kasten's PVC snapshots are themselves incremental.

### Patterns considered and rejected

| Pattern | Why rejected |
|---|---|
| Quiesce / fence a replica (1–2) | No way to freeze CockroachDB's distributed Raft state into a restorable disk snapshot. |
| DB snapshot on a permanent workload/keeper PVC (3–4) | `BACKUP` targets an S3 object API, not a filesystem path; `nodelocal://` scatters files across all nodes. |
| Temporary-PVC patterns (7–9) | SRP/OCP violations, no BlueprintBinding; strictly worse than the permanent MinIO PVC. |
| Vendor operator data mover (11) | The CockroachDB K8s operator exposes no backup CRD; backups are SQL-driven. |

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Namespace: cockroachdb-test                          │
│                                                       │
│   ┌───────────────┐   BACKUP DATABASE …               │
│   │  CockroachDB  │   INTO 's3://…minio…'              │
│   │  StatefulSet  │ ───────────────────────┐          │
│   │  (3 nodes)    │                         ▼          │
│   │  datadir PVCs │              ┌────────────────┐    │
│   │  (EXCLUDED    │              │  MinIO keeper  │    │
│   │   from snap)  │              │  Deployment    │    │
│   └───────────────┘              │  + PVC ◄───────┼── Kasten snapshots
│                                  └────────────────┘    │  ONLY this PVC
└─────────────────────────────────────────────────────┘
```

The Blueprint binds to the **MinIO Deployment** (the keeper). The `backupPrehook` execs into a
CockroachDB pod to run `BACKUP`; Kasten then snapshots the MinIO PVC. On restore, the MinIO PVC is
restored and the `restorePosthook` runs `RESTORE` into the CockroachDB cluster.

### Naming convention (required)

`<cluster>` is the keeper Deployment name with the `-minio` suffix removed.

| Resource | Name |
|---|---|
| CockroachDB StatefulSet | `<cluster>` (e.g. `cockroachdb`) |
| CockroachDB pod label | `app=<cluster>` |
| MinIO keeper Deployment / Service / PVC | `<cluster>-minio` |
| MinIO credentials Secret | `<cluster>-minio-creds` (keys `rootUser` / `rootPassword`) |
| MinIO bucket | `<cluster>-backups` (auto-created by the blueprint) |

> MinIO credentials must be **URL-safe** (alphanumeric). They are embedded in the `s3://` URL query
> string; special characters would need URL-encoding.

## Versions

Versions used when this blueprint was developed and tested:

| Component | Version |
|---|---|
| Kubernetes | `1.32` (EKS) |
| Kasten | `8.5.9` |
| CockroachDB | `v24.3.33` |
| MinIO | `RELEASE.2025-04-22T22-12-26Z` |
| Tool image | `michaelcourcy/kasten-tools:8.5.2` (adds `kubectl` to `kanister-tools`) |

Detect your Kasten version from the cluster:

```bash
helm ls -n kasten-io
```

## Custom image

The blueprint runs its phases with `michaelcourcy/kasten-tools:8.5.2`, which is
`gcr.io/kasten-images/kanister-tools:8.5.2` plus `kubectl` (the stock image has no `kubectl`).
The CockroachDB `BACKUP`/`RESTORE` SQL runs **inside** a CockroachDB pod via `kubectl exec`, so no
extra database tooling is needed in the image. A `Dockerfile` for an equivalent image:

```dockerfile
FROM gcr.io/kasten-images/kanister-tools:8.5.2
RUN curl -LO "https://dl.k8s.io/release/v1.32.0/bin/linux/amd64/kubectl" \
    && install -m 0755 kubectl /usr/local/bin/kubectl && rm kubectl
```

## Deployment

### 1. Create the namespace

```bash
kubectl create namespace cockroachdb-test
```

### 2. Deploy the MinIO keeper

```bash
kubectl apply -f minio-keeper.yaml
```

This creates the `cockroachdb-minio` Secret, PVC (10Gi), Deployment (labelled
`cockroachdb-minio: "true"`), and Service.

> **Storage class**: `ebs-sc` is the AWS EBS CSI storage class used in our test environment.
> Replace it with a storage class that supports CSI snapshots on your cluster
> (e.g. `managed-csi` on AKS, `standard-rwo` on GKE, your custom class on bare-metal, etc.).
> The class must have a matching `VolumeSnapshotClass` registered with Kasten.
> Do **not** use legacy in-tree classes (e.g. `gp2` on AWS) — they do not support CSI snapshots.

### 3. Deploy CockroachDB and initialize the cluster

```bash
kubectl apply -f cockroachdb-statefulset.yaml
# wait for all 3 pods to be Running (they stay 0/1 until the cluster is initialized)
kubectl rollout status statefulset/cockroachdb -n cockroachdb-test --timeout=120s || true
kubectl apply -f cockroachdb-init.yaml
```

The same `ebs-sc` storage-class note applies to the `volumeClaimTemplates` in
`cockroachdb-statefulset.yaml`.

After the init Job completes, all three pods become `1/1 Ready`.

### 4. Create test data

```bash
kubectl exec -n cockroachdb-test cockroachdb-0 -- /cockroach/cockroach sql --insecure -e "
CREATE DATABASE IF NOT EXISTS bank;
CREATE TABLE IF NOT EXISTS bank.accounts (id INT PRIMARY KEY, owner STRING, balance DECIMAL);
INSERT INTO bank.accounts (id, owner, balance) VALUES
  (1,'alice',1000.50),(2,'bob',250.00),(3,'carol',9999.99),(4,'dave',42.42),(5,'erin',777.77);
SELECT * FROM bank.accounts ORDER BY id;
"
```

### 5. Install the blueprint and binding

```bash
kubectl apply -f blueprint.yaml
kubectl apply -f blueprintbinding.yaml
```

## Kasten policy configuration

Create a backup policy on the `cockroachdb-test` namespace with a **Location profile** (S3, GCS,
Azure Blob, etc.) in `backupParameters.profile` — a Kanister-compatible profile is required so the
restore can extract it from the `RestorePointContent`.

**Critical: exclude the CockroachDB data PVCs from the snapshot.** Only the MinIO PVC must be
snapshotted — the CockroachDB data PVCs hold distributed Raft state that is not a valid restore
source, and they are fully represented in the MinIO backup collection:

```yaml
filters:
  excludeResources:
    - resource: persistentvolumeclaims
      matchLabels:
        app: cockroachdb
```

The CockroachDB data PVCs carry the label `app=cockroachdb` (inherited from the StatefulSet); the
MinIO PVC has no such label, so it remains in the snapshot.

## Blueprint actions

| Action | When called | What it does |
|---|---|---|
| `backupPrehook` | Before the PVC snapshot | Ensures the MinIO bucket exists; discovers user databases (excludes `system`/`defaultdb`/`postgres`); runs `BACKUP DATABASE … INTO` the MinIO collection; `sync`s the MinIO PVC; outputs `databases`/`bucket`/`namespace` artifacts. |
| `restorePosthook` | After the MinIO PVC is restored | Waits for the MinIO keeper and a ready CockroachDB pod; discovers databases in the LATEST backup; `DROP DATABASE IF EXISTS` each then `RESTORE DATABASE … FROM LATEST IN` the MinIO collection. |
| `restorePrehook` | Before PVC restore | Placeholder — **not yet triggered** by Kasten (≤ 8.5.x). The restore logic runs in `restorePosthook` so restore works today. |
| `delete` | On restore-point deletion | Informational no-op — the backup collection lives on the MinIO PVC, whose lifecycle is governed by Kasten snapshot retention. |

## Restore

### Same-namespace rollback (data loss / corruption)

The CockroachDB cluster keeps running. Kasten restores the MinIO PVC, and the `restorePosthook`
drops and re-creates the user databases from the backup collection.

```bash
# list restore points
kubectl get restorepoint -n cockroachdb-test

# trigger the restore (no profile needed — extracted from RestorePointContent)
kubectl create -f - --validate=false <<EOF
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RestoreAction
metadata:
  generateName: restore-
  namespace: cockroachdb-test
spec:
  subject:
    apiVersion: apps.kio.kasten.io/v1alpha1
    kind: RestorePoint
    name: <RESTORE_POINT_NAME>
    namespace: cockroachdb-test
  targetNamespace: cockroachdb-test
EOF
```

### Disaster recovery to a fresh namespace

The CockroachDB data PVCs are excluded from the backup, so a restored namespace has **no
initialized cluster**. Before triggering the Kasten restore you must provision and initialize a
fresh CockroachDB cluster (and the MinIO keeper is restored by Kasten):

1. Deploy `cockroachdb-statefulset.yaml` and run `cockroachdb-init.yaml` in the target namespace.
2. Trigger the Kasten `RestoreAction`. Kasten restores the MinIO PVC; the `restorePosthook` then
   restores the databases into the freshly initialized cluster.

> ### ⚠️ `restorePrehook` not yet triggered (Kasten ≤ 8.5.x)
>
> Kasten does not yet invoke `restorePrehook`. This blueprint performs all restore work in
> `restorePosthook`, which **is** triggered, so no manual step is required for the supported flows
> above. The `restorePrehook` is implemented only for forward compatibility.

## Verify a restore

```bash
# corrupt the data
kubectl exec -n cockroachdb-test cockroachdb-0 -- /cockroach/cockroach sql --insecure -e "DROP DATABASE bank CASCADE;"

# ... trigger the RestoreAction above, wait for it to complete ...

# confirm recovery
kubectl exec -n cockroachdb-test cockroachdb-0 -- /cockroach/cockroach sql --insecure -e "SELECT * FROM bank.accounts ORDER BY id;"
```

## Cleanup

```bash
# remove the workload and dependencies
kubectl delete -f cockroachdb-init.yaml --ignore-not-found
kubectl delete -f cockroachdb-statefulset.yaml --ignore-not-found
kubectl delete -f minio-keeper.yaml --ignore-not-found
kubectl delete pvc -l app=cockroachdb -n cockroachdb-test
kubectl delete pvc cockroachdb-minio -n cockroachdb-test --ignore-not-found
kubectl delete namespace cockroachdb-test

# remove the blueprint + binding
kubectl delete -f blueprintbinding.yaml --ignore-not-found
kubectl delete -f blueprint.yaml --ignore-not-found

# remove Kasten restore point contents created for the test
kubectl delete restorepointcontent -l k10.kasten.io/appNamespace=cockroachdb-test
```

## Troubleshooting

```bash
# Kanister controller (blueprint execution)
kubectl logs -n kasten-io -l app=kanister-svc --tail=100

# Executor service (primary debug target)
kubectl logs -n kasten-io -l component=executor --tail=10000 -f
```
