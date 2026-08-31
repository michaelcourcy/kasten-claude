# VMware Tanzu for Postgres on Kubernetes — Blueprint (Pattern 5, MinIO keeper)

Kasten blueprint for [VMware Tanzu for Postgres on Kubernetes 4.5](https://techdocs.broadcom.com/us/en/vmware-tanzu/data-solutions/tanzu-for-postgres-on-kubernetes/4-5/tnz-postgres-k8s/index.html)
instances that are configured for **high availability**.

The operator ships its own backup engine, [pgBackRest](https://pgbackrest.org/), driven by four
CRDs. This blueprint aims that engine at a **MinIO Deployment inside the application namespace**
instead of at a remote bucket, and lets Kasten snapshot the MinIO PVC. The Postgres data volumes
are never snapshotted — the pgBackRest repository on MinIO is the only thing Kasten protects, and
it is sufficient for a complete recovery, including point-in-time recovery.

---

## ⚠️ Validation status — read this first

This blueprint was **written from the Broadcom documentation and only partly tested**. No Broadcom
Support Portal entitlement was available, so the operator and the Postgres instance images could
not be pulled and a real Tanzu instance was never deployed. See
[Prerequisites](#prerequisites) for what an entitlement requires.

To avoid shipping something completely unverified, the blueprint was exercised against a stub
environment on the target cluster: the real `sql.tanzu.vmware.com` CRD shapes, a real MinIO keeper,
and a **real PostgreSQL 17 pod** carrying the labels and container name the operator uses. A small
script stood in for the operator's reconcile loop and moved `PostgresBackup.status.phase` through
`Running` to `Succeeded`.

| Area | Status |
|---|---|
| MinIO keeper deploys, bucket creation | **Verified** on the cluster |
| BlueprintBinding matches the keeper Deployment | **Verified** — Kasten selected the blueprint |
| Template variables resolve (`.Object.metadata.*`) | **Verified** |
| Instance lookup and `currentState` gate | **Verified** against the stub CR |
| `PostgresBackup` creation, correct schema variant | **Verified** — see [Known API ambiguity](#known-api-ambiguity) |
| Phase polling to `Succeeded`, timeout handling | **Verified** |
| Primary pod discovery by Patroni labels | **Verified** against the documented labels |
| WAL switch and archiver wait | **Verified** against real PostgreSQL 17 |
| `kando` artifacts round-trip into the `delete` action | **Verified** — deleting the restore point expired the backup |
| RunAction completes, restore point created | **Verified** |
| pgBackRest actually writing to MinIO | **Not tested** — needs the operator |
| Restore, and point-in-time recovery | **Not tested** — needs the operator |
| Deduplication measurement (Step 6) | **Not run** — needs a realistic dataset |

Everything marked *Not tested* depends on the Broadcom images. Treat the restore procedure below as
documentation-derived, and validate it before relying on it.

---

## Versions

| Component | Version |
|---|---|
| Kubernetes | `1.32` (EKS, `v1.32.9-eks-ecaa3a6`) |
| Kasten | `9.0.4` |
| Tanzu for Postgres operator | `4.5.0` (targeted; not deployed) |
| PostgreSQL | `17.5` (`postgres-17.5`) |
| MinIO | `RELEASE.2025-04-22T22-12-26Z` |
| `kasten-tools` image | `9.0.4` |

The `kasten-tools` tag must equal the Kasten version on your cluster. Detect it with
`helm ls -n kasten-io`, then change the tag in [blueprint.yaml](blueprint.yaml). The image is
`gcr.io/kasten-images/kanister-tools` plus `kubectl` and `jq`; its Dockerfile is
[../images/kasten-tools/Dockerfile](../images/kasten-tools/Dockerfile). CI publishes any version a
blueprint references, so you do not have to build it yourself.

---

## Pattern

**Pattern 5 — database backup through a local MinIO keeper.**

A MinIO `Deployment` with a permanent ReadWriteOnce PVC holds the pgBackRest repository. The
`backupPrehook` action:

1. Checks that the Postgres instance exists and reports `currentState: Running`.
2. Creates a `PostgresBackup` CR, which makes the operator run a full pgBackRest backup into MinIO.
3. Polls `status.phase` until `Succeeded`.
4. Forces a WAL segment switch on the primary and waits until the archiver confirms that segment
   reached MinIO.

Kasten then snapshots the MinIO PVC. The Postgres data and WAL PVCs are excluded by a policy
filter.

### Why this pattern and not the alternatives

The operator owns a real data mover, so every dump-based pattern would be worse: it would discard
pgBackRest's incremental backups and its point-in-time recovery. The question is only *where*
pgBackRest should write.

| Considered | Why it was not chosen |
|---|---|
| **Pattern 11** — pgBackRest writes to a remote bucket | This is the vendor's normal setup, but Kasten then protects nothing. Immutability, retention, and encryption at the backup target all stay with pgBackRest, and the data is invisible to Kasten reporting. |
| **`PostgresBackupLocation` with `storage.pvc`** — the operator's own repository PVC, which would remove the need for MinIO | Broadcom states: *"Backup onto a PV feature is currently a tech preview, and it is not recommended for production environments."* It also drops LSN-based and transaction-ID-based recovery, requires a storage class with an `Immediate` binding policy, and has the operator take its own VolumeSnapshots of the same PVC that Kasten would snapshot. Worth revisiting when it becomes generally available. |
| **Pattern 1** — fence and quiesce a replica | Members are managed by Patroni. Fencing one is not a supported operation: Patroni would try to restart or re-initialise the member, and the operator's reconcile loop would undo the change. It also gives no point-in-time recovery. |
| **Pattern 2** — quiesce for crash-consistent snapshots | An HA instance has three or more pods, each with its own data and WAL PVC, and the snapshots are taken at slightly different moments. Restoring that set gives Patroni members whose write-ahead logs stop at different positions, plus a stale consensus state. |
| **Patterns 6–9** — `pg_dump` onto a PVC | No incremental backups, no point-in-time recovery, and no consistency guarantee across an HA instance without additional locking. |

### What Kasten adds over letting pgBackRest write to a remote bucket

- **Immutability** at the backup target, which pgBackRest retention policies cannot guarantee.
- **Encryption** of snapshot data, and authenticated movement to the backup target.
- **Backup targets pgBackRest does not support** — NFS, Veeam Vault, Veeam Backup & Replication.
- **Incremental transfer.** Kasten's PVC snapshots are incremental, and its export to object
  storage is content-addressed and deduplicated.
- **Credential isolation.** The application team creates MinIO credentials inside its own
  namespace and never handles the credentials for the external backup target.
- **A fast local restore.** Recovering from the most recent restore point reads the snapshot of a
  PVC that is already in the namespace, with no download from the external target.
- **Self-contained configuration.** Every object — MinIO, its PVC, the `PostgresBackupLocation`,
  the `Postgres` instance, the secrets — lives in the application namespace. The same manifests
  work in another namespace by changing only the namespace name, which makes the whole thing a
  good candidate for a Helm chart.

---

## Architecture

```
┌────────────────────────────────────────────────┐
│  Namespace: tanzu-postgres                     │
│                                                │
│  pg-ha-monitor-0   Patroni coordination        │
│  pg-ha-0           Leader (primary)            │
│  pg-ha-1           Sync Standby                │
│  pg-ha-2           Replica                     │
│        │                                       │
│        │  pgBackRest base backups              │
│        │  + continuous WAL archiving           │
│        ▼                                       │
│  PVCs (excluded    ┌──────────┐                │
│  from snapshot)    │  MinIO   │                │
│                    │  Keeper  │                │
│                    │   PVC    │◄───────────────┼── Kasten snapshots
│                    └──────────┘                │   only this
└────────────────────────────────────────────────┘
```

The BlueprintBinding matches Deployments labelled `tanzu-postgres-minio: "true"`. **The MinIO
Deployment must be named `<instance-name>-minio`** — the blueprint derives the Postgres instance
name by removing the `-minio` suffix.

---

## Blueprint actions

| Action | Hook | What it does |
|---|---|---|
| `backupPrehook` | Before the PVC snapshot | Verifies the instance is `Running`; creates a `PostgresBackup` CR; waits for `status.phase: Succeeded`; switches the WAL segment and waits for the archiver to ship it to MinIO; outputs `backupName`, `namespace`, `instance`, `stanzaName`, and `restoreLabel` as restore-point artifacts |
| `delete` | When a restore point is retired | Sets `spec.expire: true` on the `PostgresBackup` CR so the operator runs `pgbackrest expire` and removes the backup from the live MinIO repository, then deletes the CR. Does nothing if the namespace or the CR is already gone |

There is no `backupPosthook`: nothing was quiesced, so nothing has to be released.

There are no restore hooks. Recovery needs decisions a hook cannot make — the target instance name,
and whether to recover to the end of the archive or to a chosen moment — so it is a documented
manual procedure. See [Restoring](#restoring).

> **Why the `delete` action is needed.** Deleting a Kasten restore point removes only the MinIO PVC
> snapshot. The `PostgresBackup` CR and the base-backup files it points to inside the live MinIO
> repository are not touched, so the bucket would grow without limit. Setting `spec.expire: true`
> is the operator's documented deletion path and runs `pgbackrest expire`.

> **Why the WAL switch is needed.** The `PostgresBackup` reaches `Succeeded` once the base backup
> files are in MinIO. But `pg_backup_stop()` also writes a WAL record at the *stop LSN* — the
> position PostgreSQL must replay to before that base backup is consistent. That record lands in
> the WAL segment that is currently open and only partly filled, and PostgreSQL archives only
> *complete* segments, so on a quiet instance it can stay on local disk indefinitely.
>
> Kasten freezes the MinIO PVC the moment the prehook returns. A segment not archived by then never
> appears in the restore point, and the snapshot would hold a base backup with no consistent
> recovery point. `pg_switch_wal()` closes the segment and hands it to the archiver immediately.
>
> The blueprint then **waits for confirmation instead of sleeping a fixed number of seconds**: it
> records the segment name before the switch, and polls `pg_stat_archiver.last_archived_wal` until
> it reaches that segment. It also watches `last_failed_wal`, so a pgBackRest failure to write to
> MinIO fails the backup rather than producing a restore point that cannot be recovered.

---

## Prerequisites

### Broadcom entitlement

The operator and the Postgres instance images come from `tanzu-sql-postgres.packages.broadcom.com`,
which is not publicly accessible. You need:

1. A **Broadcom Support Portal account on a company email domain**. Free email providers cannot be
   associated with entitlements.
2. A **siteID** attached to that account, issued when a Broadcom contract is signed. Check under
   *My Entitlements*. Since 3 March 2025, users who are not entitled cannot see the product at all.
3. A **Registry Token** generated in the portal.

There is no trial or free developer tier for this product. Registry tokens issued before
26 January 2026 were invalidated and have to be regenerated.

- [Tanzu entitlement requirements (KB 385970)](https://knowledge.broadcom.com/external/article/385970)
- [Tanzu Artifactory repositories and registry tokens (KB 426904)](https://knowledge.broadcom.com/external/article/426904/vmware-tanzu-artifactory-repos-and-inst.html)

### Cluster

- Kubernetes 1.23 or later. Tanzu for Postgres does **not** require Tanzu Kubernetes Grid; Broadcom
  tests it on TKG, GKE, AKS, EKS, and OpenShift 4.10+.
- cert-manager installed.
- A CSI storage class that supports snapshots, with a `VolumeSnapshotClass` registered with Kasten.
- Kasten installed in `kasten-io`.

---

## Deploying the workload

### 1. Create the namespace and the registry secret

```bash
kubectl create namespace tanzu-postgres

kubectl create secret docker-registry regsecret \
  --docker-server=https://tanzu-sql-postgres.packages.broadcom.com/ \
  --docker-username='<BROADCOM_USERNAME>' \
  --docker-password='<REGISTRY_TOKEN>' \
  -n tanzu-postgres
```

### 2. Install the operator in its own namespace

Keep the operator separate from the application namespace, so that Kasten is never responsible for
restoring the operator itself.

```bash
kubectl create namespace tanzu-postgres-operator

kubectl create secret docker-registry regsecret \
  --docker-server=https://tanzu-sql-postgres.packages.broadcom.com/ \
  --docker-username='<BROADCOM_USERNAME>' \
  --docker-password='<REGISTRY_TOKEN>' \
  -n tanzu-postgres-operator

helm registry login tanzu-sql-postgres.packages.broadcom.com \
  --username='<BROADCOM_USERNAME>' --password='<REGISTRY_TOKEN>'

helm pull oci://tanzu-sql-postgres.packages.broadcom.com/vmware-sql-postgres-operator \
  --version v4.5.0 --untar --untardir /tmp

helm install postgres-operator /tmp/vmware-sql-postgres-operator \
  --namespace tanzu-postgres-operator --wait
```

### 3. Deploy the MinIO keeper

Review [minio-keeper.yaml](minio-keeper.yaml) before applying, in particular the
`storageClassName` on the PVC.

> **Storage class**: `ebs-sc` is the AWS EBS CSI storage class used in our test environment.
> Replace it with a storage class that supports CSI snapshots on your cluster
> (for example, `managed-csi` on AKS, `standard-rwo` on GKE, your custom class on bare-metal, and so on).
> The class must have a matching `VolumeSnapshotClass` registered with Kasten.
> Do **not** use legacy in-tree classes (for example, `gp2` on AWS) — they do not support CSI snapshots.

```bash
kubectl apply -f minio-keeper.yaml
kubectl wait deployment pg-ha-minio -n tanzu-postgres --for=condition=Available --timeout=3m
```

The credentials in that file are the defaults `minioadmin` / `minioadmin123`. Change them for
anything beyond a test.

### 4. Create the bucket

```bash
kubectl run mc-init -n tanzu-postgres --image=minio/mc:latest --restart=Never --rm -i --command -- \
  /bin/sh -c "
    mc alias set local http://pg-ha-minio:9000 minioadmin minioadmin123 &&
    mc mb --ignore-existing local/tanzu-pg-backup &&
    mc ls local
  "
```

### 5. Create the backup location

```bash
kubectl apply -f backuplocation.yaml
```

`enableSSL: false` is acceptable only because this traffic never leaves the namespace. Kasten
encrypts the data once it leaves the cluster.

### 6. Deploy the HA Postgres instance

Review [postgres-ha.yaml](postgres-ha.yaml) and replace `storageClassName: ebs-sc` with your own
CSI storage class (same requirement as step 3). Check which Postgres versions your operator ships
with `kubectl get postgresversions`.

```bash
kubectl apply -f postgres-ha.yaml
```

`highAvailability.enabled` with `readReplicas: 1` creates four pods:

| Pod | Role |
|---|---|
| `pg-ha-monitor-0` | Patroni coordination |
| `pg-ha-0` | Leader — accepts writes |
| `pg-ha-1` | Sync Standby — synchronous replica |
| `pg-ha-2` | Replica — asynchronous |

Wait for the instance to come up:

```bash
kubectl get postgres pg-ha -n tanzu-postgres -w
# wait until status.currentState is Running
```

Attaching `backupLocation` makes the operator initialise a pgBackRest stanza and start archiving
WAL. It also triggers one immediate full backup; add the annotation
`sql.tanzu.vmware.com/skip-initial-backup: "true"` on the `Postgres` object to suppress that.

### 7. Create test data

Keep the dataset tiny — this is for a fast development loop, not a performance test.

```bash
PRIMARY=$(kubectl get pods -n tanzu-postgres \
  -l "postgres-instance=pg-ha,type=data,role=primary" \
  -o jsonpath='{.items[0].metadata.name}')

kubectl exec -n tanzu-postgres "$PRIMARY" -c pg-container -- psql -d kasten_test -c "
  CREATE TABLE employees (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100),
    department VARCHAR(50),
    salary INTEGER,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
  );
  INSERT INTO employees (name, department, salary) VALUES
    ('Alice Martin',  'Engineering', 95000),
    ('Bob Chen',      'Engineering', 88000),
    ('Carol Smith',   'Marketing',   72000),
    ('David Johnson', 'Sales',       68000),
    ('Eve Williams',  'Engineering', 102000);
  SELECT * FROM employees ORDER BY id;
"
```

### 8. Deploy the blueprint and the BlueprintBinding

```bash
kubectl apply -f blueprint.yaml
kubectl apply -f blueprintbinding.yaml
```

---

## Kasten policy configuration

Create a backup policy on the `tanzu-postgres` namespace with a **Location** profile. Kanister
phases need one; an Infra profile will not work.

**Exclude the Postgres data PVCs.** Only the MinIO PVC should be snapshotted:

```yaml
backupParameters:
  filters:
    excludeResources:
      - matchLabels:
          postgres-instance: pg-ha
```

> **Verify the label on your cluster before relying on this filter.** It was taken from the
> documented pod labels and could not be confirmed against real operator-created PVCs. Check with:
>
> ```bash
> kubectl get pvc -n tanzu-postgres --show-labels
> ```
>
> If the PVCs carry a different label, adjust the filter. Confirm afterwards that a restore point
> contains exactly one volume — the MinIO PVC.

The filter matters for two reasons:

- **Correctness.** If the Postgres data PVCs were restored alongside the MinIO PVC, the operator
  would find volumes holding PostgreSQL data older than the WAL archive, and recovery from the
  archive would fail or produce an inconsistent result.
- **Cost.** Those PVCs are already fully represented inside the pgBackRest repository. Snapshotting
  them consumes storage and snapshot quota with no recovery benefit.

---

## How far back you can recover

### What one restore point contains

A Kasten restore point is a snapshot of the MinIO PVC. That PVC does not hold only the base backup
the prehook just created — it holds **the whole pgBackRest repository as it stood at that moment**:

- every base backup not yet removed by the `retentionPolicy`, and
- every WAL segment archived since the oldest retained base backup.

WAL archiving is continuous and runs independently of base backups, so by snapshot time the PVC
already holds an uninterrupted WAL stream reaching back to the oldest retained base backup.

### Worked example

Suppose Kasten runs daily and `fullRetention` keeps 4 full backups. On **3 January at 10:00** the
prehook creates a base backup and Kasten snapshots MinIO. That snapshot contains the base backups
from 31 December, 1, 2 and 3 January, plus every WAL segment from 31 December onward.

To recover to **3 January at 05:00**, pgBackRest starts from the **2 January** base backup and
replays WAL forward to 05:00. The 3 January base backup cannot be the starting point, because it is
dated after the target and WAL replay only moves forward.

### Sizing retention against the Kasten interval

`retentionPolicy` controls what stays in the **live** MinIO repository. Once a base backup falls
outside the window, pgBackRest removes it and the WAL that predates it.

If retention is shorter than the Kasten backup interval, the previous base backup can be removed
before the next Kasten snapshot captures it. That snapshot would then hold only the newly created
base backup and no earlier history, which shrinks the recovery window available from it.

**Set `fullRetention` to at least twice the Kasten backup interval.**

| Kasten backup frequency | Minimum `fullRetention.number` |
|---|---|
| Daily | `3` (this blueprint ships `4`) |
| Every 12 hours | `4` |
| Weekly | `2`, and keep more Kasten restore points instead |

---

## Verifying a backup

```bash
# The backup CR the prehook created
kubectl get postgresbackup -n tanzu-postgres

# What pgBackRest thinks it has
PRIMARY=$(kubectl get pods -n tanzu-postgres \
  -l "postgres-instance=pg-ha,type=data,role=primary" \
  -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n tanzu-postgres "$PRIMARY" -c pg-container -- \
  bash -c 'pgbackrest info --stanza=${BACKUP_STANZA_NAME}'

# What actually reached MinIO
MINIO=$(kubectl get pods -n tanzu-postgres -l app=pg-ha-minio -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n tanzu-postgres "$MINIO" -- sh -c "
  mc alias set local http://localhost:9000 minioadmin minioadmin123 2>/dev/null
  mc ls --recursive local/tanzu-pg-backup/ | tail -20
"
```

Expect a `backup/` directory with a timestamped base backup and an `archive/` directory of WAL
segments.

---

## Restoring

> Not tested — see [Validation status](#-validation-status--read-this-first).

Kasten restores the MinIO PVC, which brings back the pgBackRest repository. The Postgres instance
is then rebuilt from that repository by a `PostgresRestore`, not by Kasten.

### Step 1 — Note the recovery target (optional)

For point-in-time recovery, pick a timestamp inside the archive: after a base backup and before the
last WAL segment in the snapshot.

### Step 2 — Delete the existing instance

The operator must not be running against the old volumes while Kasten restores MinIO.

```bash
kubectl delete postgres pg-ha -n tanzu-postgres
kubectl wait pods -n tanzu-postgres -l postgres-instance=pg-ha --for=delete --timeout=5m
```

### Step 3 — Restore with Kasten

Exclude the `Postgres` CR: the instance is recreated by the `PostgresRestore` in step 4, not by
Kasten. On Kasten 9.0 no `profile` field is needed — it is extracted from the
`RestorePointContent`. On Kasten 8.x you must add it (see [AGENTS.md](../AGENTS.md)).

```bash
RESTORE_POINT=$(kubectl get restorepoint -n tanzu-postgres \
  -o jsonpath='{.items[-1].metadata.name}')

kubectl create -f - --validate=false <<EOF
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RestoreAction
metadata:
  generateName: restore-
  namespace: tanzu-postgres
spec:
  subject:
    apiVersion: apps.kio.kasten.io/v1alpha1
    kind: RestorePoint
    name: ${RESTORE_POINT}
    namespace: tanzu-postgres
  targetNamespace: tanzu-postgres
  filters:
    excludeResources:
      - group: sql.tanzu.vmware.com
        resource: postgres
        name: pg-ha
EOF
```

Wait for the action to reach 100%, then confirm MinIO is serving the restored repository.

### Step 4 — Rebuild the instance from the repository

`PostgresRestore` creates the target instance itself. Recovering to the end of the archive uses
`pitr.type: latest`.

Get the stanza name first — the blueprint records it as a restore-point artifact, and it is also on
the restored `PostgresBackup` objects:

```bash
kubectl get postgresbackup -n tanzu-postgres \
  -o custom-columns=NAME:.metadata.name,STANZA:.status.stanzaName,LABEL:.status.restoreLabel
```

```bash
kubectl apply -f - <<EOF
apiVersion: sql.tanzu.vmware.com/v1
kind: PostgresRestore
metadata:
  name: pg-ha-restore
  namespace: tanzu-postgres
spec:
  targetInstance:
    name: pg-ha-restored
    spec:
      postgresVersion:
        name: postgres-17.5
  pitr:
    type: latest
    sourceBackupLocation:
      name: pg-ha-backuplocation
      stanzaName: <STANZA_NAME>
EOF
```

For recovery to a chosen moment, replace the `pitr` block:

```yaml
  pitr:
    type: time
    timestamp: "2026-01-03T05:00:00Z"
    sourceBackupLocation:
      name: pg-ha-backuplocation
      stanzaName: <STANZA_NAME>
```

`type` also accepts `lsn` and `transaction`, both taking the value in `pitr.target`.

### Step 5 — Verify

```bash
kubectl get postgresrestore pg-ha-restore -n tanzu-postgres -w
# status.phase moves through Running, WaitForPrimary, RecreatingSecondary,
# Finalizing, to Succeeded

PRIMARY=$(kubectl get pods -n tanzu-postgres \
  -l "postgres-instance=pg-ha-restored,type=data,role=primary" \
  -o jsonpath='{.items[0].metadata.name}')

kubectl exec -n tanzu-postgres "$PRIMARY" -c pg-container -- \
  psql -d kasten_test -c "SELECT * FROM employees ORDER BY id;"
```

### Notes

- The restored instance gets its **own stanza**, because a stanza name is
  `<namespace>-<instance-name>-<unique-id>`. It can therefore archive into the same bucket as the
  original without colliding. This is simpler than barman-based setups, which need a separate
  bucket for the recovered cluster.
- A recovery timestamp must fall after the base backup and before the last committed transaction in
  the archive. Beyond the end of the archive, PostgreSQL stops with
  `FATAL: recovery ended before configured recovery target was reached`.
- Restoring in place — using the same name for source and target — is documented by Broadcom as
  destructive and intended only for development and test.

---

## Known API ambiguity

The 4.5 documentation puts the backup type in two different places:

| Source | Field |
|---|---|
| [Backing Up and Restoring](https://techdocs.broadcom.com/us/en/vmware-tanzu/data-solutions/tanzu-for-postgres-on-kubernetes/4-5/tnz-postgres-k8s/backup-restore.html) | `spec.type` |
| [PostgresBackup CRD reference](https://techdocs.broadcom.com/us/en/vmware-tanzu/data-solutions/tanzu-for-postgres-on-kubernetes/4-5/tnz-postgres-k8s/postgresbackup_crd.html) | `spec.sourceInstance.type` |

Sending both does not work. `kubectl` validates fields strictly, and the API server rejects the
whole object rather than dropping the surplus field:

```
strict decoding error: unknown field "spec.type"
```

That error was reproduced on the cluster, which is how it was found. The blueprint therefore reads
the installed CRD schema and emits whichever form the operator declares:

```bash
kubectl get crd postgresbackups.sql.tanzu.vmware.com \
  -o jsonpath='{.spec.versions[?(@.name=="v1")].schema.openAPIV3Schema.properties.spec.properties.type.type}'
```

A result of `string` means `spec.type` exists; empty means the type belongs under `sourceInstance`.
Both branches were exercised. If your operator uses a third shape, this is the code to adjust.

A second, smaller ambiguity: the blueprint runs `psql` with no `-U`, as the Broadcom documentation
shows, which authenticates as the container's OS user. If that user has no matching role the
blueprint falls back to `-U postgres`. The probe costs one `kubectl exec` and removes a whole class
of image-dependent failure.

---

## Measuring deduplication (Step 6)

**Not run.** It needs a realistic dataset written by pgBackRest, which needs the operator.

The question worth answering is whether a *full* pgBackRest backup on every Kasten run is
affordable. It may well be: Kasten's export is content-addressed and deduplicated by Kopia, so a
rewritten backup whose content barely changed can export almost nothing. Do not assume either way —
measure.

When an entitlement is available:

1. Load tens of MB with `pgbench -i -s 10 kasten_test`. A dataset under 5 KB makes the ratio
   meaningless, because Kopia's own metadata dominates.
2. Enable an export action with a Location profile on the policy. Deduplication happens only on
   export.
3. Run backup and export, then measure:
   ```bash
   CLUSTER_UID=$(kubectl get ns default -o jsonpath='{.metadata.uid}')
   NS_UID=$(kubectl get ns tanzu-postgres -o jsonpath='{.metadata.uid}')
   aws s3 ls "s3://<BUCKET>/k10/${CLUSTER_UID}/migration/repo/${NS_UID}/" --recursive --summarize | tail -3
   ```
   Also capture `transferredBytes` promptly from the data export action — it is not retained long:
   ```bash
   kubectl get exportaction -n tanzu-postgres \
     -l k10.kasten.io/exportType=portableAppData \
     --sort-by=.metadata.creationTimestamp -o yaml | sed -n '/progressDetails/,+8p'
   ```
4. Change about 1% of rows with genuinely random data, run backup and export again, and measure
   again.

Record the logical backup size, the repository size after each export, and the growth, in a table
here.

---

## Tearing down

```bash
kubectl delete postgres pg-ha pg-ha-restored -n tanzu-postgres --ignore-not-found
kubectl delete namespace tanzu-postgres
kubectl delete namespace tanzu-postgres-operator
kubectl delete blueprint tanzu-postgres-blueprint -n kasten-io
kubectl delete blueprintbinding tanzu-postgres-blueprint-binding -n kasten-io
kubectl delete policy tanzu-postgres-backup -n kasten-io

# Restore point contents are cluster-scoped and outlive the namespace
kubectl delete restorepointcontent -l k10.kasten.io/appNamespace=tanzu-postgres
```

---

## Initial prompt

> Can you create a blueprint for the VMware Tanzu for Postgres on Kubernetes described here
> https://techdocs.broadcom.com/.../tnz-postgres-k8s/index.html — read carefully the backup-restore
> documentation. For the deployment example privilege a high availability instance.
