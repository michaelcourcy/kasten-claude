# VMware Tanzu for Postgres on Kubernetes — Blueprint (Pattern 5, MinIO keeper)

Kasten blueprint for [VMware Tanzu for Postgres on Kubernetes 4.5](https://techdocs.broadcom.com/us/en/vmware-tanzu/data-solutions/tanzu-for-postgres-on-kubernetes/4-5/tnz-postgres-k8s/index.html)
instances configured for **high availability**.

The operator ships its own backup engine, [pgBackRest](https://pgbackrest.org/), driven by four
CRDs. This blueprint aims that engine at a **MinIO Deployment inside the application namespace**
instead of at a remote bucket, and lets Kasten snapshot the MinIO PVC. The Postgres data and WAL
volumes are never snapshotted — the pgBackRest repository on MinIO is the only thing Kasten
protects, and it is sufficient for a complete recovery, including point-in-time recovery.

**Validated end to end** against a real operator: backup, restore, data recovery, and restore-point
retirement. See [What was validated](#what-was-validated).

---

## Versions

| Component | Version |
|---|---|
| Kubernetes | `1.34` (EKS — control plane `v1.34.9-eks-bca9cf6`, nodes `v1.34.10-eks-cb19647`) |
| Kasten | `9.0.4` |
| Tanzu for Postgres operator | `4.5.0` (chart `vmware-sql-postgres-operator v4.5.0`) |
| PostgreSQL | `18.4` (`VMware Postgres 18.4.0`, `PostgresVersion` object `postgres-18.4`) |
| cert-manager | `v1.21.1` |
| MinIO | `RELEASE.2025-04-22T22-12-26Z` |
| `kasten-tools` image | `9.0.4` |

The `kasten-tools` tag must equal the Kasten version on your cluster. Detect it with
`helm ls -n kasten-io`, then change the tag in [blueprint.yaml](blueprint.yaml). The image is
`gcr.io/kasten-images/kanister-tools` plus `kubectl` and `jq`; its Dockerfile is
[../images/kasten-tools/Dockerfile](../images/kasten-tools/Dockerfile). CI publishes any version a
blueprint references.

---

## Assumed environment

This blueprint **assumes the operator and the instance are already installed.** Installing them
needs a Broadcom Support Portal account whose siteID is attached to a Tanzu Data Solutions
contract, and there is no trial tier, so the install is out of scope here. The procedure below
starts at [Create test data](#1-create-test-data).

What the blueprint was developed and validated against:

| | |
|---|---|
| Cluster | 3-node EKS, Kubernetes 1.34, `ebs-sc` (`ebs.csi.aws.com`) with `ebs-snapshot-class` |
| Operator namespace | `tanzu-postgres-operator` (separate from the application, so Kasten never has to restore the operator) |
| Application namespace | `tanzu-postgres` |
| Instance | `pg-ha`, `highAvailability.enabled: true`, `readReplicas: 1`, PostgreSQL 18.4 |
| Images | relocated from the Broadcom registry into a **private** registry, as in an air-gapped install |
| Kasten | `9.0.4`, with an S3 **Location** profile (Kanister phases require one; an Infra profile will not do) |

The four manifests in this directory are the ones that were applied:
[minio-keeper.yaml](minio-keeper.yaml), [backuplocation.yaml](backuplocation.yaml),
[postgres-ha.yaml](postgres-ha.yaml), and the blueprint pair
[blueprint.yaml](blueprint.yaml) / [blueprintbinding.yaml](blueprintbinding.yaml). Read them before
applying; each carries comments explaining the choices that are not obvious.

> **Storage class**: `ebs-sc` is the AWS EBS CSI storage class used in our test environment.
> Replace it with a storage class that supports CSI snapshots on your cluster
> (for example, `managed-csi` on AKS, `standard-rwo` on GKE, your custom class on bare-metal, and so on).
> The class must have a matching `VolumeSnapshotClass` registered with Kasten.
> Do **not** use legacy in-tree classes (for example, `gp2` on AWS) — they do not support CSI snapshots.

### Relocating images to a private registry

An air-gapped install relocates the images out of `tanzu-sql-postgres.packages.broadcom.com`. The
bundle ships one Docker-format tar per image under `images/`, each already carrying its original
`RepoTags`, so relocation is a `docker load`, `docker tag`, `docker push`. Only the versions you
actually use need moving: the operator, plus one `postgres-instance` tag.

The tag the operator expects is not free-form. The chart builds it as
`<instanceRegistryRepo>:v<db-version>-v<operator-version>`, so PostgreSQL 18.4 on operator 4.5.0
must be pushed as `v18.4-v4.5.0`. Point the chart at the relocated copies with
`--set operatorImage=...` and `--set instanceRegistryRepo=...`, and name the pull secret in
`--set dockerRegistrySecretName=...`. That secret has to exist in **both** the operator namespace
and every application namespace.

The chart creates a `PostgresVersion` object for all 46 database versions it knows about,
regardless of what you relocated. The ones whose images you did not push stay in place and simply
cannot be used — only `kubectl get postgresversions` entries backed by a pushed image will start.

---

## Pattern

**Pattern 5 — database backup through a local MinIO keeper.**

A MinIO `Deployment` with a permanent ReadWriteOnce PVC holds the pgBackRest repository. The
`backupPrehook` action:

1. Checks that the Postgres instance exists and reports `currentState: Running`.
2. Creates a `PostgresBackup` CR, which makes the operator run a full pgBackRest backup into MinIO.
3. Polls `status.phase` until `Succeeded`.
4. Forces a WAL segment switch on the primary and waits until pgBackRest reports that segment in
   the repository — an RPO improvement, not a consistency requirement.
5. Runs `sync` inside the keeper pod so nothing is left in the page cache.

Kasten then snapshots the MinIO PVC. The Postgres PVCs are excluded by a policy filter.

### Why this pattern and not the alternatives

The operator owns a real data mover, so every dump-based pattern would be worse: it would discard
pgBackRest's incremental backups and its point-in-time recovery. The question is only *where*
pgBackRest should write.

| Considered | Why it was not chosen |
|---|---|
| **Pattern 11** — pgBackRest writes to a remote bucket | This is the vendor's normal setup, but Kasten then protects nothing. Immutability, retention, and encryption at the backup target all stay with pgBackRest, and the data is invisible to Kasten reporting. |
| **`PostgresBackupLocation` with `storage.pvc`** — the operator's own repository PVC, which would remove the need for MinIO | Broadcom states: *"Backup onto a PV feature is currently a tech preview, and it is not recommended for production environments."* It also drops LSN-based and transaction-ID-based recovery, requires a storage class with an `Immediate` binding policy, and has the operator take its own VolumeSnapshots of the same PVC that Kasten would snapshot. Worth revisiting when it becomes generally available. |
| **Pattern 1** — fence and quiesce a replica | Members are managed by Patroni. Fencing one is not a supported operation: Patroni would try to restart or re-initialise the member, and the operator's reconcile loop would undo the change. It also gives no point-in-time recovery. |
| **Pattern 2** — quiesce for crash-consistent snapshots | An HA instance has one data and one WAL PVC per pod, snapshotted at slightly different moments. Restoring that set gives Patroni members whose write-ahead logs stop at different positions, plus a stale consensus state. |
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
- **Self-contained configuration.** Every object lives in the application namespace, so the same
  manifests work elsewhere by changing only the namespace name.

---

## Architecture

```
┌────────────────────────────────────────────────┐
│  Namespace: tanzu-postgres                     │
│                                                │
│  pg-ha-0 ─┐                                    │
│  pg-ha-1 ─┤  one is role=primary, the other    │
│           │  role=replica (Patroni decides)    │
│           │                                    │
│           │  pgBackRest base backups           │
│           │  + continuous WAL archiving        │
│           ▼        (HTTPS)                     │
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

### Observed HA topology

`highAvailability.enabled: true` with `readReplicas: 1` produced **two pods and no monitor pod**:

| Pod | Labels |
|---|---|
| `pg-ha-0` | `role=replica`, `replica_mode=asynchronous` |
| `pg-ha-1` | `role=primary`, `replica_mode=synchronous` |

Two things to note. The primary is **not necessarily pod `-0`** — on first deployment it was
`pg-ha-1`, and after the restore it was `pg-ha-0`. Always select it by label, never by ordinal, as
the blueprint does. And the label is `type=instance`; the published documentation says `type=data`,
which matches nothing. See [Where the documentation is wrong](#where-the-documentation-is-wrong).

---

## Blueprint actions

| Action | Hook | What it does |
|---|---|---|
| `backupPrehook` | Before the PVC snapshot | Verifies the instance is `Running`; creates a `PostgresBackup` CR; waits for `status.phase: Succeeded`; switches the WAL segment and waits for it to reach the repository; `sync`s the keeper filesystem; outputs `backupName`, `namespace`, `instance`, `stanzaName`, and `restoreLabel` as restore-point artifacts |
| `delete` | When a restore point is retired | Sets `spec.expire: true` on the `PostgresBackup` CR so the operator runs `pgbackrest expire` and removes the backup from the live MinIO repository, then deletes the CR. Does nothing if the namespace or the CR is already gone |

There is no `backupPosthook`: nothing was quiesced, so nothing has to be released.

There are no restore hooks. Recovery needs decisions a hook cannot make — the target instance name,
and whether to recover to the end of the archive or to a chosen moment — so it is a documented
manual procedure. See [Restoring](#restoring).

> **Why the `delete` action is needed.** Deleting a Kasten restore point removes only the MinIO PVC
> snapshot. The `PostgresBackup` CR and the base-backup files it points to inside the live MinIO
> repository are not touched, so the bucket would grow without limit. Setting `spec.expire: true`
> is the operator's documented deletion path and runs `pgbackrest expire`.

> **What the WAL switch is for, and what it is not for.** It is **not** needed to make the base
> backup restorable. pgBackRest runs with `archive-check=y` — the default, which the operator does
> not override — so `PostgresBackup` only reports `Succeeded` once the WAL required to make that
> backup consistent is already in the repository. Verify it on your own instance with:
>
> ```bash
> kubectl exec -n tanzu-postgres "$PRIMARY" -c pg-container -- \
>   bash -c 'grep -iE "archive-check" /pgsql/custom/pgbackrest.conf || echo "unset -> default y"'
> ```
>
> What the switch buys is **recency**, which is an RPO question. Transactions committed *after* the
> base backup finished sit in the currently-open segment on the primary's disk, and PostgreSQL
> archives only *complete* segments. Kasten freezes the MinIO PVC seconds later, so without a
> switch those commits are absent from the restore point and cannot be recovered from it.
>
> Two traps the implementation has to avoid, both found by running it:
>
> 1. **A switch does not always seal a segment.** On an idle instance right after a backup the
>    current segment can be empty, and `pg_switch_wal()` then does nothing. Waiting for that
>    segment to be archived can never succeed. The blueprint compares the segment name before and
>    after the switch and skips the wait when they match.
> 2. **`pg_stat_archiver.last_archived_wal` is not a high-water mark.** With `archive-async=y` and
>    `process-max=2` pgBackRest pushes segments in parallel, so that column holds whichever
>    `archive_command` returned *last*, not the highest segment. It can also hold a `.backup` label
>    or a `.history` file. Comparing it with `>=` gives wrong answers: on one run the repository
>    already held `...1B` and `...1C` while the column still read
>    `00000003000000000000001A.00000028.backup`, which sorts *below* `...1B`, and the wait blocked
>    for its whole timeout.
>
> The blueprint therefore asks pgBackRest what is actually in MinIO, which is monotonic:
>
> ```bash
> pgbackrest info --stanza=$STANZA --output=json | jq -r '[.[0].archive[].max] | max'
> ```
>
> If the segment does not arrive in time the blueprint **warns and continues** rather than failing.
> `archive-check` already guaranteed the base backup is restorable, so failing would discard a
> usable restore point; what is lost is only the ability to recover to a moment after the base
> backup finished.

> **Why the keeper filesystem is synced.** A CSI snapshot captures the block device and bypasses
> the OS page cache, and Kasten does not `fsfreeze` the filesystem. An object MinIO has
> acknowledged but not yet flushed is captured as a **0-byte file**, so the repository looks
> complete and the restore fails. The prehook's last act is therefore `sync` inside the keeper pod,
> after which nothing can dirty the cache before the snapshot. The same root cause is documented in
> [cockroachdb-example](../cockroachdb-example/), [elasticsearch-eck-minio-example](../elasticsearch-eck-minio-example/)
> and [couchbase-operator-example](../couchbase-operator-example/).

### Why the blueprint looks up the primary pod

Only for `pg_switch_wal()`. **The backup itself is entirely the operator's job** — the blueprint
creates a `PostgresBackup` CR and the operator runs pgBackRest wherever it chooses, so no pod
selection is involved there. But `pg_switch_wal()` must run on the primary; a standby refuses it
with `recovery is in progress`. The README also needs the primary for creating and verifying test
data.

### What a stanza is

A **stanza is pgBackRest vocabulary**, not Kubernetes or Tanzu vocabulary. In pgBackRest a stanza
is the unit of configuration for **one PostgreSQL cluster**: where its data directory is, how to
reach it, and which repository its backups go to. The stanza name is also the **directory prefix
inside the repository** under which that cluster's base backups and WAL archive are stored, which
is what makes one bucket able to hold several unrelated clusters without collision. Other tools
name the same idea differently — barman calls it a *server*.

`pgbackrest stanza-create` initialises one, and that is exactly the command that failed when MinIO
was serving plaintext (see [The keeper must serve HTTPS](#the-keeper-must-serve-https)).

The operator generates the name as `<namespace>-<instance-name>-<uuid>`, for example:

```
tanzu-postgres-pg-ha-dc7f93e4-b713-4835-a16a-24488fac55e3
```

It is exposed in three places, and injected into the instance pods as `$BACKUP_STANZA_NAME`:

```bash
kubectl get postgres pg-ha -n tanzu-postgres -o jsonpath='{.status.stanzaName}'
kubectl get postgresbackup <name> -n tanzu-postgres -o jsonpath='{.status.stanzaName}'
kubectl exec -n tanzu-postgres "$PRIMARY" -c pg-container -- printenv BACKUP_STANZA_NAME
```

**The `uuid` is the part that matters operationally.** It is per-instance-lifetime, not per-name, so
an instance that is deleted and recreated under the *same* name gets a *new* stanza. Two
consequences, both observed here:

- A recovered instance can archive into the same bucket as the stanza it was recovered from,
  without collision. This is simpler than barman-based setups, which need a separate bucket for the
  recovered cluster.
- **The repository accumulates one stanza per instance lifetime**, and nothing prunes them. After
  three lifetimes of an instance always called `pg-ha`, the bucket held three, under both
  `backup/` and `archive/`:

  ```
  tanzu-pg-backup/pg-ha/backup/tanzu-postgres-pg-ha-193b12a4-.../
  tanzu-pg-backup/pg-ha/backup/tanzu-postgres-pg-ha-dc7f93e4-.../
  tanzu-pg-backup/pg-ha/backup/tanzu-postgres-pg-ha-30ae2de3-.../
  ```

  `retentionPolicy` prunes backups *within* a stanza; it does not remove a stanza that no longer
  has a live instance. Delete obsolete ones deliberately once you are sure no restore point still
  needs them — and note that a Kasten restore point captures whatever stanzas were present when
  its snapshot was taken.

Because a restore needs the stanza of the instance you are recovering *from*, and that instance is
deleted by then, the blueprint records `stanzaName` as a restore-point artifact.

### Output artifacts

Recorded on the restore point by `backupPrehook`. Only the first two are functionally required.

| Artifact | Required | Purpose |
|---|---|---|
| `backupName` | **Yes** | `delete` needs it to find the `PostgresBackup` CR and set `spec.expire` |
| `namespace` | **Yes** | `delete` runs as a `KubeTask` in `kasten-io` and has no other way to know which namespace the retired restore point belonged to |
| `stanzaName` | No | Needed at restore time for `pitr.sourceBackupLocation.stanzaName`. Recorded because once the instance is deleted the stanza name is awkward to recover |
| `restoreLabel` | No | Identifies which pgBackRest backup set the restore point matches; can be passed as `pitr.sourceBackupLocation.set` |
| `instance` | No | Redundant — derivable from `backupName` by stripping `-kasten-<timestamp>`. Kept only because it makes the restore point readable at a glance |

---

## The keeper must serve HTTPS

This is the one non-obvious requirement of the whole design.

`PostgresBackupLocation` has an `enableSSL` field, and setting it to `false` reads like "talk plain
HTTP to MinIO". It does not. The operator translates it into pgBackRest's
`repo1-storage-verify-tls=n`, which keeps TLS and only stops verifying the certificate. pgBackRest
has **no plaintext mode for an S3 repository**, so the keeper has to serve HTTPS regardless.

A plaintext MinIO fails at stanza creation, and the failure surfaces as a crash-looping
`pg-container` rather than as anything mentioning the backup location:

```
P00  ERROR: [101]: TLS error [1:167772427] wrong version number
INFO: stanza-create command end: aborted with exception [101]
ERROR: post_init script /pgsql/custom/pgBackrestConfigSetup.sh returned non-zero code 101
patroni.exceptions.PatroniFatalException: Failed to bootstrap cluster
```

`wrong version number` is what OpenSSL reports when it speaks TLS to a port answering in
cleartext. You can confirm the mechanism by decoding the generated config, which is worth knowing
about in its own right as the ground truth for what the operator did with your backup location:

```bash
kubectl get secret pg-ha-pgbackrest-config-secret -n tanzu-postgres \
  -o jsonpath='{.data.pgbackrest\.conf}' | base64 -d
```

[minio-keeper.yaml](minio-keeper.yaml) therefore issues a certificate through cert-manager — already
a prerequisite of the operator, so it costs nothing — and mounts it into MinIO. Two details make it
work:

- MinIO wants `public.crt` and `private.key`, while cert-manager writes `tls.crt` and `tls.key`, so
  the volume renames them with `items`.
- The readiness probe must switch to `scheme: HTTPS`, or the pod never becomes ready.

Because `enableSSL: false` skips verification, a **self-signed** certificate is enough and no CA
bundle has to be distributed. To verify the certificate instead, set `enableSSL: true` and put the
issuing CA in `caBundle`.

---

## 1. Create test data

Keep the dataset tiny. This is for a fast development loop, not a performance test.

Select the primary by label — it is not reliably pod `-0`:

```bash
NS=tanzu-postgres
PRIMARY=$(kubectl get pods -n $NS \
  -l "postgres-instance=pg-ha,type=instance,role=primary" \
  -o jsonpath='{.items[0].metadata.name}')
echo "$PRIMARY"
```

```bash
kubectl exec -n $NS "$PRIMARY" -c pg-container -- psql -d kasten_test -c "
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
  SELECT id, name, department, salary FROM employees ORDER BY id;
"
```

`psql` needs no `-U`: on the 4.5.0 instance image the container's OS user is `postgres`, a
superuser. The blueprint still probes and falls back to `-U postgres`, because the same call fails
with `role "root" does not exist` on images that exec in as root.

## 2. Deploy the blueprint

```bash
kubectl apply -f blueprint.yaml
kubectl apply -f blueprintbinding.yaml
```

## 3. Configure the Kasten policy

Create a backup policy on `tanzu-postgres` with a **Location** profile, and exclude the Postgres
PVCs so only the MinIO PVC is snapshotted:

```yaml
backupParameters:
  filters:
    excludeResources:
      - matchLabels:
          type: instance
```

Every object the operator creates carries `type: instance` and `postgres-instance: <name>`; the
data and WAL PVCs additionally carry `sql.tanzu.vmware.com/pvc-type: data|wal`. The MinIO PVC
carries none of them, so `type: instance` excludes exactly the four Postgres volumes and nothing
else. Use `postgres-instance: pg-ha` instead if a namespace holds several instances and you want
to filter one of them.

The filter matters for two reasons:

- **Correctness.** If the Postgres PVCs were restored alongside the MinIO PVC, the operator would
  find volumes holding PostgreSQL data older than the WAL archive, and recovery from the archive
  would fail or produce an inconsistent result.
- **Cost.** Those PVCs are already fully represented inside the pgBackRest repository. Snapshotting
  them consumes storage and snapshot quota with no recovery benefit.

Confirm it worked — a correct run produces exactly **one** VolumeSnapshot:

```bash
kubectl get volumesnapshot -n tanzu-postgres \
  -o custom-columns=NAME:.metadata.name,SOURCE:.spec.source.persistentVolumeClaimName,READY:.status.readyToUse
```

```
NAME                            SOURCE        READY
k10-csi-snap-4n9lg9wtjbjhc8x8   pg-ha-minio   true
```

## 4. Run a backup

```bash
kubectl create -f - --validate=false <<'EOF'
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RunAction
metadata:
  generateName: run-tanzu-pg-
  namespace: kasten-io
spec:
  subject:
    kind: Policy
    name: tanzu-postgres-backup
    namespace: kasten-io
EOF
```

Verify:

```bash
# The CR the prehook created
kubectl get postgresbackup -n tanzu-postgres \
  -o custom-columns=NAME:.metadata.name,PHASE:.status.phase,TYPE:.spec.type,LABEL:.status.restoreLabel,SIZE:.status.size

# What pgBackRest holds
PRIMARY=$(kubectl get pods -n tanzu-postgres \
  -l "postgres-instance=pg-ha,type=instance,role=primary" -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n tanzu-postgres "$PRIMARY" -c pg-container -- \
  bash -c 'pgbackrest info --stanza=${BACKUP_STANZA_NAME}'

# How far the repository's WAL archive reaches. This is the figure to trust —
# see the note on last_archived_wal above.
kubectl exec -n tanzu-postgres "$PRIMARY" -c pg-container -- bash -c \
  'pgbackrest info --stanza=${BACKUP_STANZA_NAME} --output=json | jq -r "[.[0].archive[].max] | max"'

# Nothing is queued for archival: every archive_status entry should end in .done,
# with no .ready files left behind.
kubectl exec -n tanzu-postgres "$PRIMARY" -c pg-container -- sh -c \
  'ls -1 /pgwal/pg18_wal/archive_status/ | sed -n "s/.*\.ready$/PENDING: &/p" ; echo "(no PENDING lines = archive drained)"'
```

`pg_stat_archiver` remains useful for spotting a *persistent* archiving failure, but read
`last_failed_wal` and `last_failed_time` rather than treating `last_archived_wal` as a
high-water mark. Note that a `.history` file failure right after a restore is benign — the
timeline switch races the archiver, and it was observed on the validated run
(`last_failed_wal = 00000003.history`) with archiving otherwise healthy.

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

## Restoring

Kasten restores the MinIO PVC, which brings back the pgBackRest repository. The Postgres instance is
then rebuilt from that repository by a `PostgresRestore`, not by Kasten.

**Restore to the original instance name.** The blueprint derives the instance name from the keeper
Deployment (`pg-ha-minio` → `pg-ha`), so recovering into `pg-ha-restored` leaves the keeper pointing
at an instance that no longer exists and every later backup fails with
`no Postgres instance 'pg-ha'`. Both were tried; reusing the original name is the one that keeps
working. It is not the "in-place restore" Broadcom warns about — the old instance is deleted first,
so this creates a fresh instance that happens to carry the same name.

### Step 1 — Note the recovery target (optional)

For point-in-time recovery, pick a timestamp inside the archive: after a base backup and before the
last WAL segment in the snapshot.

### Step 2 — Delete the existing instance

The operator must not be running against the old volumes while Kasten restores MinIO. With
`persistentVolumeClaimPolicy: delete` this also removes the Postgres PVCs, which is fine — they
were never snapshotted.

```bash
kubectl delete postgres pg-ha -n tanzu-postgres --timeout=5m
```

### Step 3 — Restore the keeper with Kasten

Exclude **everything the operator owns**, not just the `Postgres` CR. This is the step that is easy
to get wrong: excluding only the CR still restores the StatefulSet, and with no owning CR to
reconcile it, its PVCs are created and immediately garbage-collected in a loop. The RestoreAction
then sits at 92% waiting for pods that never schedule.

Excluding the `postgres-instance` label covers the StatefulSet, the services, the PodDisruptionBudget,
the ConfigMap, the fourteen generated secrets, and the Postgres PVCs in one condition. On Kasten 9.0
no `profile` field is needed — it is extracted from the `RestorePointContent`. On Kasten 8.x you
must add it (see [AGENTS.md](../AGENTS.md)).

```bash
NS=tanzu-postgres
RESTORE_POINT=$(kubectl get restorepoint -n $NS -o jsonpath='{.items[-1].metadata.name}')

kubectl create -f - --validate=false <<EOF
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RestoreAction
metadata:
  generateName: restore-
  namespace: $NS
spec:
  subject:
    apiVersion: apps.kio.kasten.io/v1alpha1
    kind: RestorePoint
    name: ${RESTORE_POINT}
    namespace: $NS
  targetNamespace: $NS
  filters:
    excludeResources:
      - matchLabels:
          postgres-instance: pg-ha
      - group: sql.tanzu.vmware.com
        resource: postgres
        name: pg-ha
EOF
```

Wait for the action to reach `Complete`, then confirm MinIO is serving the restored repository:

```bash
kubectl run mcls -n $NS --image=minio/mc:latest --restart=Never --rm -i --command -- /bin/sh -c "
  mc alias set l https://pg-ha-minio:9000 minioadmin minioadmin123 --insecure >/dev/null 2>&1
  mc ls --insecure l/tanzu-pg-backup/pg-ha/backup/
"
```

### Step 4 — Rebuild the instance from the repository

`PostgresRestore` creates the target instance itself, so `targetInstance.spec` must carry everything
the instance needs — **including both storage classes and the image pull secret**. Omitting
`walStorageClassName` on a cluster with no default StorageClass leaves the WAL PVC unbindable, and
omitting `imagePullSecret` makes the pull from a private registry fail.

Get the stanza name — the blueprint records it as a restore-point artifact, and it is also on the
`PostgresBackup` objects and in `Postgres.status.stanzaName`:

```bash
kubectl get postgresbackup -n $NS \
  -o custom-columns=NAME:.metadata.name,STANZA:.status.stanzaName,LABEL:.status.restoreLabel
```

```bash
kubectl apply -f - <<EOF
apiVersion: sql.tanzu.vmware.com/v1
kind: PostgresRestore
metadata:
  name: pg-ha-recover
  namespace: $NS
spec:
  targetInstance:
    name: pg-ha                 # the original name — see above
    spec:
      postgresVersion:
        name: postgres-18.4
      storageClassName: ebs-sc
      storageSize: 2G
      walStorageClassName: ebs-sc
      walStorageSize: 2G
      imagePullSecret:
        name: regsecret
      highAvailability:
        enabled: true
        readReplicas: 1
      backupLocation:
        name: pg-ha-backuplocation
      seccompProfile:
        type: RuntimeDefault
      persistentVolumeClaimPolicy: delete
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

`type` accepts `latest`, `time`, `lsn`, and `transaction`; the last two take the value in
`pitr.target`.

### Step 5 — Verify

```bash
kubectl get postgresrestore pg-ha-recover -n $NS -w
# phase runs WaitForPrimary -> Finalizing -> Succeeded

PRIMARY=$(kubectl get pods -n $NS \
  -l "postgres-instance=pg-ha,type=instance,role=primary" -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n $NS "$PRIMARY" -c pg-container -- \
  psql -d kasten_test -c "SELECT id,name,department,salary FROM employees ORDER BY id;"
```

### Notes

- Setting `backupLocation` on the restored instance makes it archive again immediately. It gets its
  **own stanza**, because a stanza name is `<namespace>-<instance-name>-<uuid>` and the uuid is new,
  so it can share the bucket with the recovered stanza without colliding. This is simpler than
  barman-based setups, which need a separate bucket for the recovered cluster.
- Recovering leaves the old stanza in the repository. `pgbackrest info` will list both. Prune the
  obsolete one deliberately once you no longer need it.
- A recovery timestamp must fall after the base backup and before the last committed transaction in
  the archive. Beyond the end of the archive, PostgreSQL stops with
  `FATAL: recovery ended before configured recovery target was reached`.

---

## Where the documentation is wrong

Four places where the published 4.5 documentation does not match the shipped 4.5.0 operator. All
four were found by running it.

| Topic | Documentation says | Reality |
|---|---|---|
| Pod labels | select the primary with `type=data` | The label is **`type=instance`**. `type=data` matches nothing, so the selector silently returns no pod. |
| HA topology | a monitor pod, a primary, and a sync standby | With `readReplicas: 1` there are **two pods and no monitor pod**. Patroni uses the Kubernetes API as its store, so no separate coordinator runs. |
| Backup type field | `spec.sourceInstance.type` (CRD reference page) | **`spec.type`** at the top level; `sourceInstance` accepts only `name`. The other page, "Backing Up and Restoring", has it right. |
| `enableSSL: false` | reads as plain HTTP | Becomes `repo1-storage-verify-tls=n`. TLS stays on. See [The keeper must serve HTTPS](#the-keeper-must-serve-https). |

On the third point, sending both spellings to be safe does not work. `kubectl` validates fields
strictly and the API server rejects the whole object rather than dropping the surplus one:

```
strict decoding error: unknown field "spec.type"
```

The shipped CRD is the authority. Read it directly rather than trusting either page:

```bash
kubectl get crd postgresbackups.sql.tanzu.vmware.com -o yaml \
  | python3 -c "import yaml,sys; d=yaml.safe_load(sys.stdin); \
print(list(d['spec']['versions'][0]['schema']['openAPIV3Schema']['properties']['spec']['properties']))"
```

### Escape Go templates inside a blueprint script

Not a Broadcom issue, but it cost a failed run here and will bite anyone adapting this blueprint.
Kanister renders the whole script as a **Go template** before executing it, so a
double-curly-brace expression is consumed by Kanister and never reaches the command you wrote it
for. Reaching for `kubectl -o go-template=...` unescaped fails with:

```
template: config:181:40: executing "config" at <.spec.selector.matchLabels>:
can't evaluate field spec in type param.TemplateParams
```

Kanister tried to resolve `.spec.selector.matchLabels` against its own parameters.

**It can be escaped.** Go's escape is a string literal inside an action, so `{{ "{{" }}` emits a
literal `{{`. Both quoting styles work, verified through a Kasten RunAction on the cluster:

| Written in the blueprint | Reaches `kubectl` as |
|---|---|
| `{{ "{{" }}range $k,$v := .spec.selector.matchLabels{{ "}}" }}` | `{{range $k,$v := .spec.selector.matchLabels}}` |
| `` {{ `{{` }}range $k,$v := .spec.selector.matchLabels{{ `}}` }} `` | same |

Nesting does **not** work — `{{ {{ }}...{{ }} }}` is a parse error, `unexpected "{" in command`.

This blueprint still uses `-o json | jq` instead, because the escaped form is close to unreadable
once every brace is doubled, and `jq` needs no escaping at all: its interpolation syntax is
`\(...)`. Reach for the escape when only a Go template will do, and prefer `-o jsonpath=` or
`-o json | jq` otherwise.

The trap extends to **comments**: a comment containing an unescaped double-brace expression is
rendered too, so a comment written to warn about this problem can cause it.

### Set both storage classes explicitly

`storageClassName` and `walStorageClassName` are independent, and each falls back to the cluster's
**default** StorageClass when omitted — not to the other. On a cluster with no default class,
setting only `storageClassName` leaves the WAL PVC with an empty class:

```
FailedBinding  no persistent volumes available for this claim and no storage class is set
FailedScheduling  0/3 nodes are available: pod has unbound immediate PersistentVolumeClaims
```

The instance then sits in `Unavailable` indefinitely. The same applies to
`PostgresRestore.spec.targetInstance.spec`.

---

## What was validated

Run on the environment in [Assumed environment](#assumed-environment), against the real 4.5.0
operator and PostgreSQL 18.4.

| Step | Result |
|---|---|
| Images relocated to a private registry; anonymous pull refused, authenticated pull allowed | Pass |
| Operator installed from the bundled chart, pulling only from the private registry | Pass |
| HA instance `Running`, `dbVersion 18.4`, `WalArchiving: True` | Pass |
| BlueprintBinding selected the keeper Deployment | Pass |
| `backupPrehook` created a `PostgresBackup`, reached `Succeeded` (`62.3MB`, label `20260902-103755F`) | Pass |
| WAL switch on an **active** database sealed a segment and waited until pgBackRest reported it in the repository | Pass |
| WAL switch on an **idle** database correctly detected the empty segment and skipped the wait, instead of blocking for its 5-minute timeout | Pass |
| Keeper filesystem `sync` ran as the prehook's last step | Pass |
| Exactly one VolumeSnapshot taken, of `pg-ha-minio` | Pass |
| `DROP TABLE employees`, instance deleted, Kasten restore, `PostgresRestore` → **all 5 rows recovered** | Pass |
| Restore into the original name keeps the keeper convention working; a later backup succeeded | Pass |
| Retiring the restore point ran `delete`, which expired and removed the `PostgresBackup` | Pass |

One caveat found while testing the `delete` action: retiring a `RestorePointContent` whose
`RestoreAction` was still `Running` after being cancelled did **not** invoke the action. On a
normally retired restore point it ran within 20 seconds. Kasten appears not to run the retire hook
while an action still references the restore point, so cancel and let the action settle before
retiring.

### Not measured: deduplication (Step 6)

The dataset here is five rows, which makes a deduplication ratio meaningless — Kopia's own metadata
dominates. The open question is whether a *full* pgBackRest backup on every Kasten run is
affordable; it may well be, since Kasten's export is content-addressed and deduplicated, so a
rewritten backup whose content barely changed can export almost nothing. Do not assume either way.

To measure it: load tens of MB with `pgbench -i -s 10 kasten_test`, add an export action with a
Location profile to the policy, run backup and export, then measure the repository:

```bash
CLUSTER_UID=$(kubectl get ns default -o jsonpath='{.metadata.uid}')
NS_UID=$(kubectl get ns tanzu-postgres -o jsonpath='{.metadata.uid}')
aws s3 ls "s3://<BUCKET>/k10/${CLUSTER_UID}/migration/repo/${NS_UID}/" --recursive --summarize | tail -3
```

Capture `transferredBytes` promptly from the data export action — it is not retained long:

```bash
kubectl get exportaction -n tanzu-postgres \
  -l k10.kasten.io/exportType=portableAppData \
  --sort-by=.metadata.creationTimestamp -o yaml | sed -n '/progressDetails/,+8p'
```

Then change about 1% of rows with genuinely random data, run backup and export again, and compare.

---

## Tearing down

```bash
kubectl delete postgres pg-ha -n tanzu-postgres --ignore-not-found --timeout=5m
kubectl delete namespace tanzu-postgres
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
