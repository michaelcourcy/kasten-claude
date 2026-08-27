# Aerospike — partitioned, incremental logical backup on keeper PVCs with `absctl`

Kasten blueprint for Aerospike Enterprise clusters managed by the Aerospike Kubernetes Operator
(AKO). A keeper StatefulSet holds one permanent PVC **per replica**; `backupPrehook` uses **`absctl`**
to write a logical dump of every Aerospike namespace onto them — a full, or an incremental against
the current chain — and Kasten snapshots those PVCs.

Each StatefulSet replica owns a fixed slice of the namespace's 4096 partitions and runs
**concurrently** with the others. `replicas: 1` is the simple case (one backup-shard covering everything);
raise it to backup-shard a large database across parallel workers.

> ⚠️ **This directory is an example, not a general-purpose solution.** It encodes assumptions about
> the AKO version, the CR layout, the storage class, the absence of Aerospike security, the
> namespace topology and the acceptable consistency level of the deployment it was developed
> against. Copy the directory, rename it, adapt it, then re-run Steps 2–6 to validate it. See the
> ownership disclaimer in the [repository README](../README.md).

> **Two settings, both environment variables on the keeper, both measured:**
> - **Incremental chains** cut the *export* cost ~69x — a full dump every run ships the whole
>   dataset because absctl output is not byte-stable. See [Incremental chains](#incremental-chains).
> - **Partition backup-shards** (`replicas`) cut the *backup wall-clock* — measured **2.37x at 4 backup-shards**,
>   but only once the database's storage is not the bottleneck. See
>   [Partition backup-shards](#backup-shards) and [Measured](#partitioning--measured).

---

## Versions

| Component | Version |
|---|---|
| Kubernetes | `1.32` (EKS) |
| Kasten | `9.0.4` |
| Operator | `aerospike-kubernetes-operator 4.5.0` (Helm) |
| Database | Aerospike Server Enterprise `8.1.2.4` |
| Backup CLI | `aerospike/absctl:v1.1.0` |
| Tools image | `aerospike/aerospike-tools:13.0.2` (`asinfo`, `aql`, `asbench`) |
| Kasten tools image | `ghcr.io/kastenhq/blueprint-ai/kasten-tools:9.0.4` |
| cert-manager | `v1.19.2` (AKO prerequisite) |

Detect the Kasten version on your own cluster rather than trusting this table:

```bash
helm ls -n kasten-io
```

The `kasten-tools` tag **must** equal that version — see
[images/kasten-tools/README.md](../images/kasten-tools/README.md).

---

## Why this pattern

### Why not snapshot the Aerospike volumes

Aerospike stores namespace data on **raw block devices, one PVC per pod**, and each node holds only
its own subset of the 4096 partitions. A single PVC snapshot *is* crash-consistent within that
volume, but that is not sufficient:

- **No cross-volume/cross-node consistency.** Aerospike's own documentation states snapshots are
  *"consistent within a volume, but may not be consistent between volumes"*. A 3-node namespace
  needs 6 volumes (`nsdata` + `workdir`) cut at the same instant; plain CSI `VolumeSnapshot` does
  not provide that.
- **Crash-consistent ≠ no acknowledged writes lost.** Writes sit in an 8 MiB streaming write buffer
  until it fills or `flush-max-ms` expires. Only `commit-to-device` closes that window, at a
  throughput cost.
- **System metadata (SMD)** — roster, truncate records, secondary index definitions, UDFs — lives on
  the `workdir` volume and Aerospike documents it as **not** covered by snapshot-based backup.

So the Aerospike device PVCs are **excluded** from the policy and `absctl` produces a logical dump
onto a keeper PVC instead.

### Why `absctl` and not the Aerospike Backup Service

ABS is a **lifecycle manager**: it owns cron schedules (`interval-cron`, `incr-interval-cron`) and
its own retention. Inside a Kasten blueprint that is a category error — Kasten owns backup frequency
and retention. Running both means two schedulers and two retention regimes over the same bytes.

`absctl` is the **primitive**: one invocation, one backup, no opinions about time. What that buys:

| | ABS service | `absctl` (this blueprint) |
|---|---|---|
| Scheduling | its own cron | Kasten only |
| Retention | its own | Kasten only |
| Chain state | internal to the service | the directory names on the PVC — inspectable with `ls` |
| Chain length control | two cron expressions | one env var on the keeper |
| Deleted records resurrected | yes, bounded by its full interval | same risk, bounded by `ABSCTL_INCREMENTALS_BEFORE_FULL` (`0` removes it) |
| Namespace list | hand-maintained, can silently miss one | discovered from the live cluster |
| Kubernetes objects to deploy | Deployment + ConfigMap + Service + PVC | StatefulSet + PVC |

What genuinely improves:

- **No second scheduler and no second retention policy.** Kasten decides when a backup happens and
  how long restore points live. The keeper's env var only decides how long a chain may get before a
  full is forced, which is a storage-layout concern rather than a lifecycle one.
- **The chain state is a directory listing.** You can see exactly what a restore would replay with
  `ls /backup/<namespace>`. There is no service to interrogate and nothing to get out of sync.
- **No namespace list to get wrong.** The blueprint runs `asinfo -v namespaces` against the live
  cluster, so adding a namespace to the database protects it automatically. There is no config field
  that can silently omit one.

What does **not** improve: the deleted-record risk is inherent to logical incrementals, not to ABS.
It applies here too, bounded by `ABSCTL_INCREMENTALS_BEFORE_FULL` and eliminated only by setting it
to `0`. In practice it is an accepted trade-off for this workload class — see
[Incremental chains](#incremental-chains).

---

## Incremental chains

`absctl` has no chain state of its own — `--modified-after <YYYY-MM-DD_HH:MM:SS>` is supplied by the
caller. This blueprint keeps that state **entirely in the directory names on the keeper PVC**, so
there is no marker file, no ConfigMap and nothing that can drift from reality:

```
/backup/<namespace>/full-20260826T104218Z/
/backup/<namespace>/incr-20260826T104714Z/
/backup/<namespace>/incr-20260826T105003Z/
```

The names sort chronologically, so the current chain is *the newest `full-` plus every `incr-` after
it*. Counting the incrementals decides whether the next run is another incremental or a forced full,
and the newest directory's stamp becomes the next `--modified-after`. Reading the volume is the whole
of the state machine.

### The setting that controls it

```yaml
# abs-keeper.yaml, on the keeper container
env:
  - name: ABSCTL_INCREMENTALS_BEFORE_FULL
    value: "3"        # full, incr, incr, incr, then full again (and prune)
                      # 0 = never take an incremental; every run is a full
```

The blueprint reads it from the **running** pod (`kubectl exec -- printenv`), so changing the value
and rolling the StatefulSet is enough — no blueprint edit. When a full is taken, every superseded
`full-`/`incr-` directory for that namespace is pruned. Pruning the live volume never affects
existing restore points, which are immutable snapshots.

> ℹ️ **Incrementals can resurrect a deleted record — usually an acceptable trade.** absctl
> incrementals carry changed and new records, never deletions, so a record deleted *after* the last
> full comes back if that chain is restored. The next full heals it, which makes
> `ABSCTL_INCREMENTALS_BEFORE_FULL` the maximum exposure window. Durable deletes do **not** help — an
> incremental taken after a durable delete produced no output at all.
>
> Per Aerospike's own solution architects, operators of this workload class generally **do not**
> consider this a blocker: Aerospike is typically a serving or session layer where records are
> re-derivable and the recovery objective is "get the cluster back fast", not "reproduce the exact
> deletion history". That is why this blueprint ships with `3` rather than `0`.
>
> Set `0` when a deletion genuinely must never come back — a compliance erasure path (GDPR and
> similar), or a namespace holding the authoritative copy of something. It costs a full export every
> run; see [Export cost — measured](#export-cost--measured).

A detail worth knowing if you modify this: absctl writes **nothing at all — not even the
directory** — when an incremental finds no changed records. The blueprint pre-creates the directory
so the chain counter still advances and the forced full still triggers on schedule.

---

## Export cost — measured

Measured with [Step 6 of AGENTS.md](../AGENTS.md#step-6--measure-export-deduplication-efficiency) on
100,012 records (~200-byte random values), 1% of records churned before every run,
`ABSCTL_INCREMENTALS_BEFORE_FULL=3`. Five consecutive policy runs:

| Run | Kind | Kopia repo growth | Chain after the run |
|---|---|---|---|
| 1 | **full** | **23,526,001 B** (22.4 MiB) | `full-102609Z` |
| 2 | incr | **341,899 B** (334 KiB) | + `incr-102956Z` |
| 3 | incr | **341,899 B** | + `incr-103336Z` |
| 4 | incr | **368,928 B** | + `incr-103719Z` |
| 5 | **full** | **23,525,933 B** | `full-104218Z` — **all earlier members pruned** |

**An incremental export costs ~69x less than a full.** That is the difference between this blueprint
being usable at scale and not.

### Why a full costs the whole dump

A full dump cannot be deduplicated against the previous full, because **absctl output is not
byte-stable**. Two consecutive dumps of *identical, unchanged* data differ — at `--parallel 4` and at
`--parallel 1`:

```
--parallel 4:  run A md5 = 0ae0a39ff38543be583766521956d288
               run B md5 = 9ab9f4235c1d952758af5f42e3f394b9   => NOT deterministic
--parallel 1:  run A md5 = c6d5cd5a3c74ec1dd8330a555670878f
               run B md5 = f7bab52ede5d081c4d21cdf789451654   => NOT deterministic
```

Record scan order varies between runs, so ~83% of the dump's bytes differ even after a 1% change and
content-defined chunking has nothing to match. A control confirms the pipeline itself is faithful:
50 MiB of `/dev/urandom` written to the keeper PVC grew the repository by 52,443,164 bytes, 1:1.

Incrementals sidestep this entirely — each one is a **new, small, never-rewritten directory**, so the
layout is append-only and Kasten's export only ships the new member.

### Backup duration

absctl reads **only the changed records** — it does not scan the whole namespace:

| | Records read | Bytes written | absctl duration |
|---|---|---|---|
| full | 100,012 | 28,702,695 | ~143 ms |
| incremental (1% churn) | **1,000** | 288,129 | **~13 ms** |

So the tool itself is ~10x faster. The Kasten **BackupAction** duration, however, ranged 71–152 s
across the five runs with no correlation to full vs incremental: at this dataset size it is dominated
by orchestration — snapshot creation, pod scheduling, export setup — and absctl's contribution is
milliseconds. Expect the incremental gain to become visible in the action duration only once the
dataset is large enough for the scan to dominate.

### How many bytes Kasten actually sent to object storage

The table above uses object-store repository growth because it is durable and re-checkable. Kasten's
own accounting is smaller and even more favourable, because the export is *block*-incremental on top
of the append-only layout — taken from the `portableAppData` ExportAction status:

| Export | `readBytes` | `transferredBytes` |
|---|---|---|
| full | 28,700,000 | **28,700,000** |
| incremental | **168** | **4,400** |

`processedBytes: 29000168` alongside `readBytes: 168` shows it walking the whole volume but reading
only the changed blocks. For an incremental that is ~6,500x fewer bytes sent to object storage.

> **Reading these figures yourself.** Use the **data** export action, labelled
> `k10.kasten.io/exportType: portableAppData`. The *metadata* export action
> (`k10.kasten.io/isMetadataExport: "true"`) has no `progressDetails` at all and is easy to grab by
> mistake when simply taking the newest action:
> ```bash
> kubectl get exportaction -n aerospike-test \
>   -l k10.kasten.io/exportType=portableAppData \
>   --sort-by=.metadata.creationTimestamp -o yaml | sed -n '/progressDetails/,+8p'
> ```
> **Capture it promptly** — `progressDetails` is not durably retained. On this cluster every one of
> 26 older completed data-export actions had lost it, so do not plan to read it hours after the run.
> That is why the results table uses repository growth.

## Backup-shards

> **What a "backup-shard" is.** It is this blueprint's own term — Aerospike does not use it, and
> neither does `absctl`. A **backup-shard** is one unit of backup *work*: a keeper StatefulSet pod,
> its own PVC, and the fixed range of Aerospike partitions that pod alone is responsible for
> reading. `replicas: 4` gives four backup-shards.
>
> | | Whose concept | What it is | Configurable |
> |---|---|---|---|
> | **Aerospike partition** | Aerospike's | one of exactly 4096 logical divisions of a namespace's keyspace, chosen by hashing the record key | No — always 4096 |
> | **Backup-shard** | this blueprint's | a group of those partitions assigned to one keeper replica to back up | Yes — `replicas` |
>
> The distinction that matters: **nothing about the database is sharded here.** Aerospike already
> distributes its partitions across its own nodes; the `replicas` setting only decides *who reads
> what*. The database layout, replication and node count are untouched. That is why the term is
> "backup-shard" rather than plain "shard", which usually implies the data itself was split up.
>
> Two consequences worth holding on to:
> - **Backup-shard count is unrelated to Aerospike node count.** The measurements below used
>   4 backup-shards reading from 3 Aerospike nodes. Each backup-shard reads from *all* of them,
>   because partition-to-node assignment is pseudo-random rather than contiguous.
> - **Each backup-shard is independent state** — its own chain directories, its own `.partition`
>   marker, its own `absctl` process, its own `sync` and verification. That independence is what
>   makes them safely concurrent: the partition ranges are disjoint, so two backup-shards can never
>   touch the same record, on backup or on restore.
>
> You will see the term in the blueprint's log output, for example:
> ```
> backup-shard 0 (partitions 0-1024): rc=0 records=600531 bytes=1253903007 absctl_duration=42.089640131s
> ```

Aerospike hashes every record into one of exactly **4096 partitions**. `absctl --partition-list
<start>-<count>` backs up a contiguous range of them, and a set of ranges is a deterministic,
non-overlapping, complete cover of the keyspace. That is what makes backup-sharding safe: no coordination
between workers, no duplication, no gaps.

Each StatefulSet replica owns one backup-shard and one PVC:

```
replicas: 4   ->  backup-shard 0: partitions    0-1024   on backup-...-0
                  backup-shard 1: partitions 1024-1024   on backup-...-1
                  backup-shard 2: partitions 2048-1024   on backup-...-2
                  backup-shard 3: partitions 3072-1024   on backup-...-3
```

`replicas: 1` is not a special case in the code — it is one backup-shard covering `0-4096`, which absctl
treats exactly as "no partition filter". `replicas` must divide 4096 evenly (use a power of two);
the blueprint refuses to run otherwise rather than silently leaving partitions uncovered.

**Range-based backup-sharding is load-balanced by construction.** Aerospike's partition-to-node assignment is
pseudo-random, not contiguous, so every backup-shard reads from every node. Measured on the 3-node test
cluster, each 1024-partition range was served almost evenly:

| node | 0–1023 | 1024–2047 | 2048–3071 | 3072–4095 |
|---|---|---|---|---|
| aerocluster-0-0 | 326 | 346 | 336 | 357 |
| aerocluster-0-1 | 328 | 333 | 354 | 350 |
| aerocluster-0-2 | 370 | 345 | 334 | 317 |

No backup-shard concentrates its reads on one node.

### Backup-shards run concurrently

`backupPrehook` **launches** the backup-shards one after another but **waits** for all of them together, so
wall-clock is the slowest backup-shard rather than the sum. Two markers are echoed into the
`kanister-svc` log so the parallel window can be measured independently of Kasten's snapshot time:

```
TIMING test full replicas=4 ALL_LAUNCHED epoch_ms=1787835072261
TIMING test full replicas=4 ALL_FINISHED epoch_ms=1787835113536
```

Restore replays each chain member across all backup-shards concurrently too — the ranges are disjoint, so
backup-shards never touch the same record.

### Changing `replicas` forces a full and wipes every volume

The partition range each backup-shard owns is **recorded** in `/backup/<namespace>/.partition` as
`start count replicas`, and restore **reads** it rather than recomputing it. If the recorded value no
longer matches the range implied by the current replica count, the blueprint:

1. deletes `/backup/<namespace>` on **every** backup-shard, and
2. takes a **full** backup under the new layout.

This applies to scaling **up and down** alike, and it is deliberate. A repartition invalidates every
existing chain member, because they cover ranges that no longer exist; mixing old and new ranges
would silently produce both gaps and duplicates on restore. Rather than try to reconcile layouts,
the blueprint deletes everything and starts again from a full backup.

> ⚠️ **Consequences to plan for.**
> - The first backup after any `replicas` change is a **full**, at full cost.
> - Every previous chain member on the live volumes is **deleted**. Existing Kasten restore points
>   are untouched — they are immutable snapshots — but they can only be restored into a keeper scaled
>   back to the replica count they were taken with. The blueprint enforces this: restore aborts if a
>   backup-shard's recorded `replicas` differs from the current count, and again if the ranges do not tile
>   0–4095 exactly once.
> - **Scaling down leaves orphaned PVCs.** A StatefulSet never deletes them. They still hold stale
>   chains and are still snapshotted by Kasten, wasting space. Delete them by hand:
>   `kubectl delete pvc backup-aerospike-backup-keeper-<n> -n aerospike-test` for each retired
>   ordinal.

---

## Partitioning — measured

Same dataset throughout: ~2.4M records of ~2 KB, **5,011,201,867 bytes** of logical dump.
`ABSCTL_PARALLEL_PER_BACKUP_SHARD=1` in the comparison rows, so each added backup-shard contributes exactly one
scan thread.

### The result depends entirely on the database's storage

**Rig A — Aerospike data devices on default gp3 (3,000 IOPS each, 9,000 total):**

| replicas | parallel | total scan threads | wall-clock |
|---|---|---|---|
| 1 | 1 | 1 | 266.0 s |
| 1 | 4 | 4 | 266.0 s |
| 4 | 1 | 4 | 265.0 s |
| 4 | 2 | 8 | 264.9 s |
| 4 | 4 | 16 | **failed** — `MAX_RETRIES_EXCEEDED` |

Every configuration lands on ~265 s. One thread is as fast as eight, and sixteen breaks it.

The reason is the **storage devices, not the database engine**. Aerospike keeps its index in RAM but
record data on the device, so a backup scan issues roughly one device read per record. A gp3 volume
serves a fixed number of operations per second and queues the rest: 3 volumes x 3,000 IOPS = 9,000/s.
Measured: **2,382,499 records / 265 s = 8,990 records/s.** The devices were delivering their exact
maximum, so a *single* scan thread was already enough to reach it — which is why adding threads or
backup-shards changed nothing.

The 16-thread failure is a different mechanism, not a worse version of the same one: reads waited so
long in the device queue that absctl's client timeout (10 s by default) expired, so they failed with
`ResultCode: TIMEOUT` and the client gave up after 5 retries. Queueing latency crossing a timeout,
not the database falling over. `ABSCTL_SOCKET_TIMEOUT_MS` exists for this reason.
Ruled out by measurement: Aerospike CPU (35–44 m of a 2000 m limit, ~2%), the keeper volume
(181 MB/s via `dd`), and pod co-location (the four backup-shards ran on four different nodes).

**Rig B — same cluster, data devices raised to gp3 16,000 IOPS each (48,000 total):**

| replicas | parallel | records | wall-clock | speedup |
|---|---|---|---|---|
| 1 | 1 | 2,400,010 | 97.8 s | — |
| 4 | 1 | 2,400,010 | **41.3 s** | **2.37x** |

Per-shard absctl durations at 4 backup-shards: 42.1 s, 40.2 s, 40.6 s, 40.9 s — genuinely concurrent. Record
counts are identical between the two rows, so coverage stayed exact.

### What to take from this

- **Partitioning works, and the gain is real** — but it is bounded by what the Aerospike nodes'
  storage can deliver. On default gp3 there is nothing to win: the devices are the bottleneck.
- Raising IOPS alone took the single-shard case from 266 s to 97.8 s. **Fix the storage before
  reaching for backup-shards.**
- 2.37x rather than 4x because 4 backup-shards approach the *new* ceiling too
  (2,400,010 / 41.3 s ≈ 58,000 records/s against 48,000 provisioned IOPS).
- **Too many backup-shards makes backups fail — it does not simply stop making them faster.**
  `replicas x parallel` is the total scan concurrency
  against a shared database; exceed what it can serve and backup-shards die with
  `ResultCode: TIMEOUT ... MAX_RETRIES_EXCEEDED`. Raise it gradually and watch the blueprint log.
- Nodes backed by **local NVMe** should move the bottleneck somewhere else entirely. Local NVMe
  delivers hundreds of thousands of IOPS per node, so the data devices should stop being the limit
  and something else takes over — Aerospike CPU, the network, or the keeper's own write path. Expect
  backup-sharding to scale considerably further there, but treat that as an expectation rather than
  a measurement: size `replicas` by measuring in the target environment, using the
  `ALL_LAUNCHED`/`ALL_FINISHED` markers.

---

## Prerequisites

- A Kubernetes cluster with Kasten in `kasten-io`.
- A CSI storage class supporting snapshots, with a `VolumeSnapshotClass` registered with Kasten.
- **cert-manager** — AKO's admission webhooks need it.
- **An Aerospike Enterprise feature-key file.** AKO supports Enterprise Edition only.

### The feature-key file is mandatory under AKO

The `aerospike-server-enterprise` image bundles an evaluation key at
`/etc/aerospike/features.conf` (single node, all features, version-bound rather than time-bound).
**It is unusable under AKO**, which mounts an emptyDir named `confdir` at `/etc/aerospike` in the
`aerospike-server` container, hiding the bundled file. Omitting `feature-key-file` fails with:

```
unable to open file /etc/aerospike/features.conf: No such file or directory
```

Note the AKO repository's `config/samples/secrets/` contains TLS certs and passwords but **no
`features.conf`**, even though the sample CRs reference one. Obtain a key by either:

- **Free 8-node, 60-day trial** — sign up on [aerospike.com](https://aerospike.com/). This is what
  this blueprint was tested with.
- **Extract the bundled single-node evaluation key:**
  ```bash
  docker run --rm --entrypoint cat \
    aerospike/aerospike-server-enterprise:8.1.2.4 /etc/aerospike/features.conf > features.conf
  grep -E "account-ID|cluster-nodes-limit|valid-until" features.conf
  ```

> 🔒 Feature-key files are licences. Never commit one. This repository's
> [.gitignore](../.gitignore) excludes `features.conf`, `*features.conf` and `*-features.conf`.

---

## Step 2 — Deploy the workload and create test data

### 2.1 Install AKO

Operator in its own namespace, workload in another, so operator lifecycle stays independent of the
application data Kasten protects.

```bash
kubectl create ns aerospike-operator
kubectl create ns aerospike-test

helm repo add aerospike https://aerospike.github.io/aerospike-kubernetes-enterprise
helm repo update aerospike

helm install aerospike-kubernetes-operator aerospike/aerospike-kubernetes-operator \
  --namespace aerospike-operator \
  --version 4.5.0 \
  --set watchNamespaces="aerospike-test" \
  --wait --timeout 5m
```

### 2.2 RBAC for the cluster pods

```bash
kubectl apply -f aerospike-rbac.yaml
```

> ⚠️ **The AKO documentation's namespace-scoped snippet is not sufficient.** It creates a
> `RoleBinding`, but the `aerospike-init` container runs `akoinit`, which **lists `nodes` at cluster
> scope** — something a `RoleBinding` can never grant. Without a `ClusterRoleBinding` the init
> container crash-loops with:
>
> ```
> Error: nodes is forbidden: User "system:serviceaccount:aerospike-test:aerospike-operator-controller-manager"
> cannot list resource "nodes" in API group "" at the cluster scope
> ```

### 2.3 Feature-key Secret

```bash
kubectl create secret generic aerospike-secret -n aerospike-test \
  --from-file=features.conf=<path-to-your-features.conf>
```

### 2.4 Deploy the Aerospike cluster

```bash
kubectl apply -f aerospike-cluster.yaml
kubectl get aerospikecluster -n aerospike-test -w    # wait for PHASE=Completed
```

[aerospike-cluster.yaml](aerospike-cluster.yaml) creates a 3-node cluster, namespace `test` at
`replication-factor 2`, a 2Gi filesystem `workdir` and a 10Gi **raw block** `nsdata` device per pod.

> **Storage performance matters more than storage size here.** The `nsdata` devices are what a backup
> reads from, and on default gp3 (3,000 IOPS) they saturate at a *single* absctl scan thread — see
> [Partitioning — measured](#partitioning--measured). If you intend to backup-shard the backup across
> replicas, provision the Aerospike data devices for IOPS first, or backup-sharding will give you no
> improvement at all.
> Note gp3 caps at **500 IOPS/GiB**, so 16,000 IOPS needs a volume of at least 32 GiB; asking for
> more fails with `Iops to volume size ratio ... too high`.

> **Storage class**: `ebs-sc` is the AWS EBS CSI storage class used in our test environment.
> Replace it with a storage class that supports CSI snapshots on your cluster
> (for example, `managed-csi` on AKS, `standard-rwo` on GKE, your custom class on bare-metal, and so on).
> The class must have a matching `VolumeSnapshotClass` registered with Kasten.
> Do **not** use legacy in-tree classes (for example, `gp2` on AWS) — they do not support CSI snapshots.

> ⚠️ **`blockVolumePolicy.initMethod` must zeroize.** A freshly provisioned EBS block volume contains
> non-zero data and Aerospike refuses a device that is neither already an Aerospike device nor
> erased:
>
> ```
> CRITICAL (drv_ssd): /test/dev/xvdf: not an Aerospike device but not erased - check config or erase device
> ```
>
> AKO supports only `dd` or `blkdiscard`; EBS does not reliably support discard/TRIM, so use `dd`.

> ℹ️ This cluster runs **without** Aerospike security, so `absctl` connects without credentials. With
> security enabled, add `--user`/`--password` to the `absctl` invocations in the blueprint.

### 2.5 Deploy the keeper

```bash
kubectl apply -f abs-keeper.yaml
kubectl rollout status statefulset/aerospike-backup-keeper -n aerospike-test
```

[abs-keeper.yaml](abs-keeper.yaml) creates a headless Service and a 1-replica **StatefulSet** running
`aerospike/absctl:v1.1.0`, with a `volumeClaimTemplate` for the dump volume. Notes:

- **StatefulSet, not Deployment** — the pod name is deterministic (`aerospike-backup-keeper-0`), so
  the blueprint never has to discover it, and the same shape scales to the partitioned variant.
- `securityContext.fsGroup: 65532` — absctl runs as `uid=65532(abtuser) gid=65532(abtgroup)`.
- The image ENTRYPOINT is `absctl` itself, so `command` overrides it with a sleep. The image is
  Alpine — **`/bin/sh` only, no bash**, and no `jq`, `curl` or `kando`.
- The PVC holds exactly **one** dump (`--remove-files` overwrites in place), so size for the dump,
  not for a retention chain.
- The volumeClaimTemplate is labelled `app=aerospike-backup-keeper`, deliberately **not**
  `app=aerospike-cluster`, which is what the policy excludes.
- `ABSCTL_INCREMENTALS_BEFORE_FULL` controls the chain length — see
  [Incremental chains](#incremental-chains). Each PVC must hold a whole chain, not one dump.
- `replicas` controls the number of partition backup-shards — see [Partition backup-shards](#backup-shards).
  Each replica gets its own PVC from the `volumeClaimTemplate`, so a backup-shard needs roughly
  `dataset / replicas` per chain member. Changing `replicas` **wipes every volume and forces a full**.
- `ABSCTL_PARALLEL_PER_BACKUP_SHARD` x `replicas` is the total scan concurrency hitting the database.
  If you ask for more than the database can serve, backup-shards **fail** — they do not simply stop
  getting faster.

### 2.6 Create test data

```bash
./test-data.sh load       # key1..key10 in test.demo  (~1KB)
./test-data.sh verify
./test-data.sh count      # per-node object counts
```

Deliberately tiny for a fast development loop. Step 6 needs a much larger dataset — see there.

### 2.7 Removing everything

```bash
kubectl delete -f policy.yaml -f blueprintbinding.yaml -f blueprint.yaml
kubectl delete -f abs-keeper.yaml
kubectl delete pvc -l app=aerospike-backup-keeper -n aerospike-test
kubectl delete -f aerospike-cluster.yaml
kubectl delete secret aerospike-secret -n aerospike-test
kubectl delete -f aerospike-rbac.yaml

helm uninstall aerospike-kubernetes-operator -n aerospike-operator
kubectl delete crd aerospikeclusters.asdb.aerospike.com \
  aerospikebackups.asdb.aerospike.com \
  aerospikebackupservices.asdb.aerospike.com \
  aerospikerestores.asdb.aerospike.com

kubectl delete ns aerospike-test aerospike-operator
kubectl delete restorepointcontent -l k10.kasten.io/appNamespace=aerospike-test
```

---

## Step 3 — Validate the workflow without a blueprint

```bash
SEED=aerocluster.aerospike-test.svc.cluster.local
POD=aerospike-backup-keeper-0

# Which namespaces exist?
kubectl exec -n aerospike-test aerocluster-0-0 -c aerospike-server -- asinfo -v namespaces

# Dump one namespace
kubectl exec -n aerospike-test $POD -c absctl -- \
  absctl backup --host $SEED --port 3000 -n test -d /backup/test --remove-files --parallel 4

# CRITICAL: flush the page cache before snapshotting (see the warning in Step 4)
kubectl exec -n aerospike-test $POD -c absctl -- sync

# Inspect
kubectl exec -n aerospike-test $POD -c absctl -- sh -c 'ls -l /backup/test; du -sk /backup/test'

# Truncate and restore
kubectl exec -n aerospike-test aerocluster-0-0 -c aerospike-server -- \
  asinfo -v "truncate-namespace:namespace=test"
kubectl exec -n aerospike-test $POD -c absctl -- \
  absctl restore --host $SEED --port 3000 -n test -d /backup/test --parallel 4
```

Emulate what Kasten does to the keeper PVC with a CSI snapshot, following
[this example](https://github.com/michaelcourcy/test-csi-snapshot), and wait for
`readyToUse=true` before testing anything downstream.

---

## Step 4 — The blueprint

```bash
kubectl apply -f blueprint.yaml
kubectl apply -f blueprintbinding.yaml
```

### Blueprint actions

| Action | Phase | What it does |
|---|---|---|
| `backupPrehook` | `absctlBackup` | Reads `replicas` from the StatefulSet and derives each backup-shard's partition range. Discovers the Aerospike server pod (`app=aerospike-cluster`), the cluster name (`aerospike.com/cr` → headless Service = seed host) and the namespace list (`asinfo -v namespaces`). Reads `ABSCTL_INCREMENTALS_BEFORE_FULL`, `ABSCTL_PARALLEL_PER_BACKUP_SHARD` and `ABSCTL_SOCKET_TIMEOUT_MS` from the running keeper. Detects a **replica-count change** via the `.partition` marker and, if found, wipes every backup-shard volume and forces a full. Derives chain state from directory names, then **launches all backup-shards concurrently** and waits for them together, echoing `ALL_LAUNCHED`/`ALL_FINISHED` timing markers. Writes the `.partition` marker, **`sync`**s every backup-shard, verifies absctl's reported bytes landed on each device, and **prunes superseded chain members on every backup-shard after a full**. |
| `restorePosthook` | `absctlRestore` | Waits for every restored keeper pod. Enumerates namespaces from backup-shard 0 and, for each, **reads the `.partition` markers and validates that the backup-shard ranges tile 0–4095 exactly once** — aborting if the restore point was taken under a different replica count. Resolves the chain (newest full + later incrementals) and rejects zero-length `.asb` files. **All of this happens before any truncate.** Then per namespace: `truncate-namespace`, then replays each chain member **oldest first**, with **all backup-shards in parallel** within a member. |
| `delete` | `absctlDelete` | Logs the artifact values. Nothing external to delete. |

There is deliberately **no `backupPosthook`**: the keeper PVC is permanent, so there is nothing to
unquiesce and no temporary PVC to remove.

### 🛑 The `sync` is not optional

```yaml
kubectl exec ... -- sync
```

A CSI/EBS snapshot captures the **block device** and bypasses the OS page cache. `absctl` returns as
soon as its writes are in cache, so **without this sync the snapshot captures `.asb` files as zero
length**. The result is a restore point that looks perfectly healthy, exports almost nothing, and
restores no data at all — silent, total data loss. Observed directly during development:

```
0        0_test_4.asb
0        1_test_1.asb
7159224  2_test_2.asb
0        3_test_3.asb
```

Counting files is not enough to detect this, because the (empty) files exist. The blueprint compares
**absctl's own reported byte count against the on-device sum after the sync**, which detects the
un-synced case precisely without misreading a legitimately empty incremental (0 changed records → 0
files) as a failure.

### Pre-flight validation must precede the truncate

The restore truncates each namespace before repopulating it. If a dump problem were discovered
mid-loop, the already-truncated namespaces would be left **empty** — turning a failed restore into
data loss. This happened during development and destroyed a test namespace. The blueprint therefore
validates every namespace's dump up front and aborts before a single record is deleted.

### Design notes

**Everything is discovered, nothing is configured.** No ConfigMap, no annotation, no hardcoded
hostname:

| Needed | Discovered from |
|---|---|
| keeper pod | `<StatefulSet name>-0` — deterministic |
| Aerospike server pod | label `app=aerospike-cluster` in the same namespace |
| seed host | `aerospike.com/cr` label → AerospikeCluster name → headless Service |
| namespaces (backup) | `asinfo -v namespaces` on the live cluster |
| namespaces (restore) | the directories present on the keeper PVC |

**One image, via `kubectl exec`.** Phases are `KubeTask` in `kasten-io` using
`kasten-tools:9.0.4` (kubectl, jq, kando) and reach `absctl` in the keeper and `asinfo` in the
server pods with `kubectl exec`. Running in `kasten-io` is required: that is where the Kanister
service account with cross-namespace `pods/exec` lives. No custom image is built.

**`absctl restore` requires `--namespace`.** The help text presents it as remapping only
(`source-ns,destination-ns`), which reads as optional. Omitting it fails with
`failed to validate config: namespace is required`. Passing the bare namespace restores it to
itself.

---

## Step 5 — Kasten policy and end-to-end test

```bash
kubectl apply -f policy.yaml
kubectl get policy aerospike-absctl-backup -n kasten-io -o jsonpath='{.status.validation}'
```

[policy.yaml](policy.yaml) has three mandatory properties:

**1. A Location profile, not an Infra profile** — only a Location profile is recorded in the
`RestorePointContent` in a Kanister-compatible form.

```bash
kubectl get profile <name> -n kasten-io -o jsonpath='{.spec.type}'   # must print "Location"
```

**2. The Aerospike PVCs must be excluded.**

```yaml
filters:
  excludeResources:
    - resource: persistentvolumeclaims
      matchLabels:
        app: aerospike-cluster
```

AKO's PVCs (`nsdata-*`, `workdir-*`) all carry `app=aerospike-cluster`; the keeper PVC carries
`app=aerospike-backup-keeper` and stays in the snapshot. Confirm exactly one snapshot per run:

```bash
kubectl get volumesnapshot -n aerospike-test \
  -o custom-columns='NAME:.metadata.name,PVC:.spec.source.persistentVolumeClaimName,READY:.status.readyToUse'
```

**3. An export action** — required for Step 6, and what makes restore points durable beyond the
cluster. No `migrationToken` is needed in the manifest; Kasten generates one on admission.

### Run a backup

Trigger through a Kasten `RunAction` — **never create a Kanister ActionSet by hand**, since Kasten
sets the ActionSet context up differently and template variables silently resolve to empty strings.

```bash
kubectl create -f - --validate=false <<EOF
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RunAction
metadata:
  generateName: run-aerospike-
  namespace: kasten-io
spec:
  subject:
    kind: Policy
    name: aerospike-absctl-backup
    namespace: kasten-io
EOF
```

### Restore

```bash
kubectl get restorepoint -n aerospike-test

kubectl create -f - --validate=false <<EOF
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RestoreAction
metadata:
  generateName: restore-
  namespace: aerospike-test
spec:
  subject:
    apiVersion: apps.kio.kasten.io/v1alpha1
    kind: RestorePoint
    name: <RESTORE_POINT_NAME>
    namespace: aerospike-test
  targetNamespace: aerospike-test
EOF
```

No `profile` is needed: Kasten extracts the location profile from the `RestorePointContent`.
Verified on Kasten `9.0.4` restoring from a **remote** (exported) restore point with no `profile`
field — the action completed and `restorePosthook` ran normally.

> ⚠️ **On Kasten 8.x you must add `spec.profile` explicitly.** There, extraction was unreliable: the
> restore fell back to looking for a default profile literally named `kanister-profile` and, when it
> did not exist, failed *after* the volumes had been restored, with `restorePosthook` never running:
>
> ```
> profiles.config.kio.kasten.io "kanister-profile" not found
> Could not fetch a location profile. Location profile must be specified in action or policy parameters
> ```
>
> Observed on `8.5.13`, fixed in `9.0.4`. The failure mode is deceptive for a truncate-then-restore
> blueprint: the volume restore succeeds and reports 100% progress, so it looks like a late failure
> when in fact the data-side hook never executed. Workaround on 8.x:
>
> ```yaml
>   profile:
>     name: <LOCATION_PROFILE>
>     namespace: kasten-io
> ```

**Manual preparation for disaster recovery.** For an in-place rollback nothing is needed — the
blueprint truncates and restores. For a restore into an empty namespace or new cluster, the
`AerospikeCluster` must be **running and ready** first, because its device PVCs are excluded from the
restore point. The blueprint waits up to 10 minutes for the cluster to answer `asinfo`.

### Verified end-to-end result

Two Aerospike namespaces (`test` with 100k records plus a `demo` set, and `bar`).

**Chain behaviour** — five consecutive policy runs with `ABSCTL_INCREMENTALS_BEFORE_FULL=3`
produced exactly `full, incr, incr, incr, full`, and the fifth run pruned every earlier member. See
[Export cost — measured](#export-cost--measured).

**Multiple Aerospike namespaces, each with its own chain** — this is the case worth testing, because
every namespace maintains an independent chain and is truncated and replayed independently. Both
namespaces were given a versioned key in each of two incrementals, then corrupted differently:

| Namespace | full | incr 1 | incr 2 |
|---|---|---|---|
| `bar` | 1,389 B | 166 B | 166 B |
| `test` | 28,701,821 B | 168 B | 168 B |

| | Expected | Actual after restore |
|---|---|---|
| `test.keyMULTI` | `v2` — newest of `test`'s two incrementals | ✅ `v2` |
| `bar.keyMULTI` | `w2` — newest of `bar`'s two incrementals | ✅ `w2` |
| `keyJUNKT` (added to `test`, never backed up) | removed | ✅ absent |
| `keyJUNKB` (added to `bar`, never backed up) | removed | ✅ absent |

Note `bar`'s incrementals are only 166 bytes while `test`'s full is 28 MB — the chains are resolved
per namespace, so a small namespace does not have to wait for a large one.

**Chain replay ordering (single namespace)** — a key was given a different value in each of two
incrementals, then deleted, with an extra record added that was never backed up:

```bash
# incremental 1
INSERT INTO test.demo (PK, id, name, city) VALUES ('keyCHAIN', 1, 'chain', 'v1')   # run -> incr
# incremental 2
INSERT INTO test.demo (PK, id, name, city) VALUES ('keyCHAIN', 2, 'chain', 'v2')   # run -> incr
# corrupt
DELETE FROM test.demo WHERE PK='keyCHAIN'
./test-data.sh add-one keyBOGUS        # a record that is NOT in any backup
```

Chain on the PVC: `full-104218Z`, `incr-104714Z`, `incr-105003Z`.

| | Expected | Actual after restore |
|---|---|---|
| `keyCHAIN` | `v2` — newest of the two incrementals | ✅ `v2` |
| `keyBOGUS` | removed (never backed up) | ✅ absent |
| `test.demo` | key1–key10 + keyCHAIN + keyINC1/2 | ✅ |
| `bar.demo` | key1–key10 | ✅ |

`keyCHAIN` resolving to `v2` proves the chain is replayed oldest-first so the newest version of a
record wins. `keyBOGUS` being removed proves the truncate phase runs — an additive restore alone
would have left it behind.

## Step 6 — Export cost measurement

The procedure is defined in
[Step 6 of AGENTS.md](../AGENTS.md#step-6--measure-export-deduplication-efficiency);
[measure-dedup.sh](measure-dedup.sh) implements it for this blueprint.

```bash
./measure-dedup.sh size            # current Kopia repo size on the object store
./measure-dedup.sh dump            # current chain size on the keeper PVC
./measure-dedup.sh run <name>      # backup + export, wait for both
./measure-dedup.sh churn <n>       # rewrite n records with fresh random values
```

Four things that make or break the measurement:

- **The dataset must be large enough.** The Step 2 dataset (~1KB) is useless; Kopia metadata
  dominates. Load ~100k records with `asbench` first:
  ```bash
  asbench --hosts <seed> --port 3000 --namespace test --set bench \
          --keys 100000 --object-spec S200 --workload I --threads 16 --random
  ```
- **`--random` is mandatory.** Without it `asbench` writes the *same* value to every record, which
  deduplicates ~200:1 and makes the result meaningless. This cost real debugging time.
- **Sum both export prefixes.** The namespace repository holds the bulk, with a smaller per-policy
  companion:
  ```
  k10/<cluster-uid>/migration/repo/<namespace-uid>/   <- the exported volume data
  k10/<cluster-uid>/migration/<policy-name>/kopia/    <- smaller companion repo
  ```
- **`transferredBytes` cannot be read after the fact.** Kasten discards
  `status.progressDetails` when an action completes, so the repository growth on the object store is
  the retained, verifiable figure — that is what the results table uses.

Results are in [Export cost — measured](#export-cost--measured).

## Troubleshooting

```bash
# Read a failed action's real cause — it is nested inside status.error.cause
kubectl get restoreaction <name> -n aerospike-test -o jsonpath='{.status.error}'
kubectl get runaction <name> -n kasten-io -o jsonpath='{.status.error}'

kubectl logs -n kasten-io -l component=executor --tail=10000 -f
kubectl logs -n aerospike-test aerocluster-0-0 -c aerospike-server --tail=200
kubectl logs -n aerospike-test aerocluster-0-0 -c aerospike-init --tail=60
```

| Symptom | Cause |
|---|---|
| `profiles... "kanister-profile" not found` and `restorePosthook` never ran | Kasten 8.x only; fixed in 9.0.4. On 8.x set `spec.profile` on the `RestoreAction`. |
| `nodes is forbidden ... at the cluster scope` in `aerospike-init` | A `RoleBinding` was used instead of a `ClusterRoleBinding`. Apply [aerospike-rbac.yaml](aerospike-rbac.yaml). |
| `unable to open file /etc/aerospike/features.conf` | Relying on the image's bundled key under AKO, which overlays `/etc/aerospike`. Mount your own key. |
| `not an Aerospike device but not erased` | `blockVolumePolicy.initMethod` is `none`. Use `dd`. |
| `cannot update replication-factor ... alongside any other spec change` | The AKO webhook requires that change on its own; easiest during development is to delete and recreate the CR. |
| `failed to validate config: namespace is required` | `absctl restore` needs `--namespace`, despite the help text. |
| `.asb` files are zero length in a restore point | The backup ran without `sync`. See [the sync warning](#-the-sync-is-not-optional). |
| Restore fails and a namespace is left empty | A pre-flight failure after truncate. The current blueprint validates before truncating; older revisions did not. |
| `Kanister artifact did not contain expected size field` (error level) | Benign. Kasten looks for a `size` key for repository accounting; no blueprint in this repo emits one. |

Test commands before changing YAML:

```bash
kubectl run debug -n aerospike-test --image=aerospike/aerospike-tools:13.0.2 --restart=Never --rm -it -- bash
```

---

## Files

| File | Purpose |
|---|---|
| [aerospike-rbac.yaml](aerospike-rbac.yaml) | ServiceAccount + **ClusterRoleBinding** for the Aerospike pods |
| [aerospike-cluster.yaml](aerospike-cluster.yaml) | 3-node `AerospikeCluster` CR |
| [abs-keeper.yaml](abs-keeper.yaml) | Keeper headless Service + StatefulSet with the dump PVC |
| [blueprint.yaml](blueprint.yaml) | The blueprint |
| [blueprintbinding.yaml](blueprintbinding.yaml) | Binds the blueprint to the keeper StatefulSet |
| [policy.yaml](policy.yaml) | Kasten policy: PVC exclusion + export |
| [test-data.sh](test-data.sh) | Load / verify / mutate the test dataset |
| [measure-dedup.sh](measure-dedup.sh) | Step 6 deduplication measurement |

---

## Initial prompt

> Can you build a blueprint for Aerospike?
>
> How could this approach complement or compare with Aerospike Backup Service and its incremental
> backup capabilities, considering there will be also other applications to backup on the same
> cluster such as Kafka?
>
> [after a review with an Aerospike solution architect] Using the ABS routine and policy is
> unadapted because Kasten is in charge of the backup lifecycle — we cannot ride both horses. The
> image contains an `absctl` utility that can do all the primitive operations. Implement the simple
> approach with absctl: full backup each time, but with an honest Kopia deduplication measurement.
>
> [after that measurement showed a full dump exports the whole dataset every run] Now work with
> incremental mode. Put the number of incrementals before a full in an env variable on the keeper —
> discovering how many incrementals were done should just be reading the filesystem in the PVC.
>
> [next round] Now the parallel version with partitions. One blueprint, not two — simple just means
> a StatefulSet of 1. Keep incremental mode. Test a full with N=1 and N=4, make sure the members run
> in parallel and not sequentially, and log when all launches finish and when all members finish so
> the two timestamps can be compared independently of the snapshot time.
