# Apache Cassandra with cass-operator — Kasten Blueprint

Backup and restore for an [Apache Cassandra](https://cassandra.apache.org/) datacenter managed by
[cass-operator](https://github.com/k8ssandra/cass-operator), using **pattern 2: quiesce**.

Kasten is the data mover. It snapshots the data PVC of every Cassandra node. The blueprint only
flushes each node before the snapshot and repairs the ring after a restore.

> **Read [Three Kasten behaviours you must know](#three-kasten-behaviours-you-must-know) before
> you deploy anything.** Each of them made a backup or a restore fail — two of them silently —
> while this blueprint was being developed.

## Versions

| Component | Version |
|---|---|
| Kubernetes | `1.31.6` |
| OpenShift | `4.18.6` |
| Kasten | `9.0.6` (OLM operator `k10-kasten-operator-rhmp.v9.0.6`) |
| cass-operator | `1.23.2` (OLM, `certified-operators`, channel `stable`) |
| Apache Cassandra | `5.0.5` (image `cr.k8ssandra.io/k8ssandra/cass-management-api:5.0.5-ubi`) |
| Storage | Azure Disk CSI (`managed-csi`), snapshot class `csi-azuredisk-vsc` |
| Tool image | `ghcr.io/kastenhq/blueprint-ai/kasten-tools:9.0.6` ([Dockerfile](../images/kasten-tools/Dockerfile)) |

Detect your Kasten version:

```bash
helm ls -n kasten-io
# or (OpenShift OLM):
kubectl get csv -n kasten-io -o jsonpath='{.items[?(@.spec.displayName=="Veeam Kasten")].spec.version}'
```

> **Tool image note.** CI publishes `kasten-tools:9.0.6` when this blueprint is merged to the
> default branch. The hooks were validated with an image built from the same
> [Dockerfile](../images/kasten-tools/Dockerfile) with `--build-arg KASTEN_VERSION=9.0.6
> --build-arg KUBECTL_VERSION=1.31.6`, pushed to the cluster's internal registry. The published
> tag is built with the Dockerfile default `KUBECTL_VERSION=1.32.0`, which is within the
> supported version skew of a 1.31 cluster.

## Pattern

**Pattern 2 — Quiesce**, in the light form Cassandra allows.

Cassandra is built to survive a crash on each node. Every write goes to the **commit log** before
it changes the in-memory table (the **memtable**). A node that starts from a crash-consistent
volume replays its commit log, so a CSI snapshot of the volume is restorable without any dump.

The blueprint runs `nodetool flush` on every node before the snapshot. A flush writes the
memtables out as **SSTables** — Cassandra's data files, which are never modified after they are
written. This makes the replay after a restore short, and puts the data in files that Kopia can
deduplicate from one backup to the next. The flush does **not** stop traffic: the application
keeps reading and writing during the backup.

`nodetool drain` would stop writes completely, but the node then refuses writes until the pod
restarts. That is an outage, not a quiesce.

### What this protects, and what it does not

| | |
|---|---|
| **Protected** | Every write acknowledged at `QUORUM` before the backup started. Verified twice, with 2.18 and 2.46 million rows, under 30,000–37,000 writes/s — see [Validation](#validation). |
| **Best effort** | Writes acknowledged *while* the nodes are snapshotted. Kasten does not snapshot all volumes at the same instant, so one node can hold a write that another does not. The restore repairs the ring, which makes the replicas agree again, but a write that reached no snapshot at all is lost. In the tests, 2 of 3.3 million (run 1) and 0 of 1.4 million (run 3) such writes were missing. |
| **Not provided** | A single point in time across the cluster. This is crash-consistent per node, eventually consistent across nodes. |
| **Not provided** | Restore of a single keyspace or table. The unit of restore is the whole datacenter. |

### Patterns considered and ruled out

| Pattern | Why not |
|---|---|
| 1 — Fence and quiesce a replica | Cassandra has no primary/replica split. With replication factor below the node count, no single node holds all the data. |
| 3, 4, 5, 7, 8 — Dump to an extra PVC or to MinIO | They only add a copy step. `cqlsh COPY` does not scale, and a dump is not incremental. |
| 6 — `nodetool snapshot` on the data PVC | Cheap, because it uses hard links, and gives a cleaner point in time on each node. But a restore means moving files back into every table directory on every node. Kept as a fallback. |
| 11 — Vendor data mover (K8ssandra Medusa) | Only with the K8ssandra operator, and then Kasten is not the data mover. |

## Three Kasten behaviours you must know

All three were observed on Kasten `9.0.6`.

### 1. Kasten has a built-in blueprint for `CassandraDatacenter`, and it needs Medusa

As soon as a `CassandraDatacenter` exists, Kasten creates a blueprint named
`k10-k8ssandra-bp-<version>` (here `k10-k8ssandra-bp-0.0.4`) and applies it to the CR. Its
`backup` action creates a `MedusaBackupJob`, so it assumes the **K8ssandra operator with Medusa**.
With plain cass-operator that CRD does not exist, and every backup fails:

```
no matches for kind "MedusaBackupJob" in version "medusa.k8ssandra.io/v1alpha1"
ensure CRDs are installed first
```

**What this blueprint does:** it binds its own blueprint to the `CassandraDatacenter`. A blueprint
bound to the CR replaces the built-in one. Because this blueprint has no `backup` action, Kasten
goes back to snapshotting the PVCs itself.

**Alternative:** switch off every built-in data-service blueprint for the whole cluster with the
Helm value `kanister.managedDataServicesBlueprintsEnabled: false` (on an OLM install, in the `K10`
CR spec), then delete `k10-k8ssandra-bp-<version>`. This affects every team on the cluster,
including anyone who uses K8ssandra with Medusa on purpose. The binding does not need it.

### 2. Kasten runs only the topmost owner's blueprint — and finds it from a workload

cass-operator creates one StatefulSet per rack, owned by the `CassandraDatacenter`. Kasten finds a
hook blueprint by starting from each **workload** in the backup and walking up its owner chain to
the **topmost owner**. When both a StatefulSet and the CR carry a blueprint, it logs:

```
Multiple blueprints found in ownership chain. The topmost owner's blueprint will be used.
```

Two consequences, both silent — the backup still reports `Complete`:

| Mistake | Result |
|---|---|
| Bind the blueprint to the StatefulSets | It never runs; only the CR's blueprint (or the built-in one) does. |
| Exclude the StatefulSets from the **backup** | No workload to start from: `backupPrehook` never runs and **nothing is flushed**. |

So the blueprint is bound to the CR, and the policy **keeps** the StatefulSets.

### 3. A restore that includes the StatefulSets waits forever

Kasten restores workloads **before** custom resources, and waits for each workload to become ready.
A cass-operator pod only starts Cassandra when the operator tells it to, and the operator does
nothing until the `CassandraDatacenter` exists. The restore stops at about 94% and does not move:
the pods show `1/2` (Management API up, Cassandra not started) indefinitely.

Restored StatefulSets also come back **without an owner reference**, so the operator does not adopt
them, and deleting the CR later leaves them behind.

**So the restore excludes the StatefulSets** (see [Restore](#restore)). Kasten restores the PVCs,
the Secret and the CR; the operator then creates the StatefulSets, which bind to the restored PVCs
by name and are owned by the CR as normal.

## How it works

### Backup

1. `backupPrehook` checks the operator reports the datacenter `Ready` and not stopped.
2. It lists the PVCs labelled `cassandra.datastax.com/datacenter=<dc>` — the PVCs Kasten will
   snapshot — and the running pods that mount them. A PVC that no running pod mounts **fails the
   backup**: that node cannot be flushed.
3. It runs `nodetool flush && sync` on every node, in parallel. The `sync` is last, so that no
   flushed SSTable sits in the page cache when the snapshot is taken.
4. Kasten snapshots the PVCs.

The operator's `Ready` is a coarse signal: during testing the CR reported `Ready` while one node was
not. Step 1 is therefore not relied on as a node-health check — step 3 is. When a node could not be
flushed, the prehook failed with `ERROR: flush failed on at least one node`, the backup failed and
**no restore point was created**.

### Restore

1. **Manual:** delete the `CassandraDatacenter` and wait for its pods and PVCs to be deleted.
2. Kasten restores the PVCs, the superuser Secret and the CR, **excluding the StatefulSets**.
3. cass-operator creates the StatefulSets. Each node starts on its restored volume, replays its
   commit log, and discards any SSTable that was half-written when the snapshot was taken (the log
   shows `Unfinished transaction log, deleting …` or `Removing orphans for …` — expected).
4. `restorePosthook` waits for the datacenter to be `Ready`, checks every node is Up/Normal, then
   runs `nodetool repair --full -pr` on each node, one after the other.

Every pod gets a new IP address. Nodes keep their identity (host ID) from the restored volume, and
the cluster reforms normally.

## Blueprint actions

| Action | What it does |
|---|---|
| `backupPrehook` | Waits for the datacenter to be `Ready` and not stopped; lists the datacenter's PVCs and the pods that mount them (fails if a PVC is not mounted by a running pod, warns about other PVCs in the namespace); runs `nodetool flush` then `sync` on every node in parallel |
| `restorePosthook` | Waits for the datacenter to be `Ready`; checks every node is Up/Normal; runs `nodetool repair --full -pr` on each node in turn |

### Why `repair --full -pr`

- `-pr` repairs only the token ranges each node is **primary** for, so running it on every node
  repairs the whole ring exactly once.
- `--full` because incremental repair skips SSTables already marked as repaired, and restored
  SSTables can carry that mark from before the backup.
- One node at a time, which is the load Cassandra's own tooling assumes.

### Restore takes longer than 20 minutes

Kasten stops a hook action after **`timeout.blueprintResourceHooks`** minutes (Helm value, default
`20`; shown in the `k10-config` ConfigMap as `K10TimeoutBlueprintResourceHooks`). The older names
`timeout.blueprintHooks` and `kanister.hookTimeout` still work but are deprecated. On an OLM install
the value goes in the `K10` CR spec.

That budget covers **both** phases of `restorePosthook`: the wait for `Ready` (bounded at 15
minutes in the blueprint) **and** the repair. Measured on this cluster:

| Data per node | Time to `Ready` | Repair (3 nodes) |
|---|---|---|
| about 2 GB | about 4 min | about 3 min |

The repair grows with the amount of data, and on a large cluster it will not fit in 20 minutes.
Two choices:

- raise `timeout.blueprintResourceHooks` to cover your repair time, or
- remove the `repair` phase from the blueprint and run the repair outside Kasten after the restore
  (for example with Reaper). The data is still restored; replicas just disagree until the repair.

If the hook times out, the **restore action reports Failed even though the volumes and the CR were
restored**. Check the data before you restore again.

## Prerequisites

### 1. Install cass-operator in its own namespace

The operator lives in `cass-operator`; the datacenter in its own namespace. Kasten backs up the
datacenter namespace, never the operator.

```bash
kubectl create namespace cass-operator
kubectl apply -f - <<'EOF'
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: cass-operator
  namespace: cass-operator
spec: {}          # AllNamespaces: the operator watches every namespace
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: cass-operator
  namespace: cass-operator
spec:
  channel: stable
  name: cass-operator
  source: certified-operators
  sourceNamespace: openshift-marketplace
  installPlanApproval: Automatic
EOF
kubectl get csv -n cass-operator | grep cass-operator     # wait for Succeeded
```

On plain Kubernetes, install cass-operator with its Helm chart or kustomize instead
([instructions](https://github.com/k8ssandra/cass-operator#installing-the-operator)). It needs
cert-manager.

### 2. OpenShift only: let Cassandra run as UID 999

cass-operator runs Cassandra as UID `999` with fsGroup `999`. The default `restricted-v2` SCC
rejects that (`999 is not an allowed group`). [openshift-scc.yaml](openshift-scc.yaml) creates a
`cassandra` ServiceAccount allowed to use `nonroot-v2`, which accepts any non-root UID and is
narrower than `anyuid`. The CR names it in `spec.serviceAccount`.

> The ServiceAccount cannot be changed on an existing datacenter — the operator's webhook rejects
> it (`attempted to change serviceAccount`). Create it before the CR.

### 3. A Location profile

The policy's `backupParameters.profile` must be a **Location** profile (S3, GCS, Azure Blob), not
Infra. Only a Location profile lets `restorePosthook` run.

```bash
kubectl get profile <name> -n kasten-io -o jsonpath='{.spec.type}'   # must print: Location
```

## Deploy the test workload

```bash
kubectl create namespace cassandra-test
kubectl apply -f openshift-scc.yaml            # OpenShift only
kubectl apply -f cassandradatacenter.yaml
kubectl get cassdc dc1 -n cassandra-test -o jsonpath='{.status.cassandraOperatorProgress}'   # wait for Ready
```

[cassandradatacenter.yaml](cassandradatacenter.yaml) creates Cassandra `5.0.5` with 3 nodes: one
rack per availability zone, one node per rack. cass-operator creates **one StatefulSet per rack**
(`demo-dc1-r1-sts`, `-r2-sts`, `-r3-sts`). The operator starts the nodes one at a time; allow about
5 minutes.

> **Storage class**: `managed-csi` is the Azure Disk CSI storage class used in our test environment.
> Replace it with a storage class that supports CSI snapshots on your cluster
> (for example, `ebs-sc` on AWS, `standard-rwo` on GKE, your custom class on bare-metal, and so on).
> The class must have a matching `VolumeSnapshotClass` registered with Kasten.
> Do **not** use legacy in-tree classes (for example, `gp2` on AWS) — they do not support CSI snapshots.

> **Zones**: the racks use `nodeAffinityLabels` for the zones `francecentral-1/2/3`. Replace them
> with your zones, or remove them.

> **Heap**: 2 GB inside a 4 GiB container. 1 GB was too small under the heavy-write test: one node
> filled its heap, spent 6 seconds in each garbage collection, dropped millions of writes and could
> not finish `nodetool flush`.

## Create test data

```bash
NS=cassandra-test
U=$(kubectl get secret demo-superuser -n $NS -o jsonpath='{.data.username}' | base64 -d)
P=$(kubectl get secret demo-superuser -n $NS -o jsonpath='{.data.password}' | base64 -d)
cql() { kubectl exec -n $NS demo-dc1-r1-sts-0 -c cassandra -- cqlsh -u "$U" -p "$P" -e "$1"; }

cql "CREATE KEYSPACE IF NOT EXISTS shop WITH replication = {'class':'NetworkTopologyStrategy','dc1':3};
     CREATE TABLE IF NOT EXISTS shop.customers (id int PRIMARY KEY, name text, city text);
     CONSISTENCY QUORUM;
     INSERT INTO shop.customers (id,name,city) VALUES (1,'Alice','Paris');
     INSERT INTO shop.customers (id,name,city) VALUES (2,'Bob','Lyon');
     INSERT INTO shop.customers (id,name,city) VALUES (3,'Carol','Nantes');
     INSERT INTO shop.customers (id,name,city) VALUES (4,'David','Lille');
     INSERT INTO shop.customers (id,name,city) VALUES (5,'Eve','Nice');"
cql "SELECT * FROM shop.customers;"     # 5 rows
```

## Deploy the blueprint, the binding and the policy

```bash
kubectl apply -f blueprint.yaml -f blueprintbinding.yaml
sed 's/<your-location-profile>/my-location-profile/' policy.yaml | kubectl apply -f -
```

| File | Content |
|---|---|
| [blueprint.yaml](blueprint.yaml) | Blueprint `cassandra-cass-operator-bp` |
| [blueprintbinding.yaml](blueprintbinding.yaml) | Binds it to every `CassandraDatacenter` (the CR, not the StatefulSets) |
| [policy.yaml](policy.yaml) | On-demand policy for `cassandra-test`. Keeps the StatefulSets (see behaviour 2); excludes the OLM `ClusterServiceVersion` copies |

## Run a backup

```bash
kubectl create -f - <<'EOF'
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RunAction
metadata:
  generateName: run-cassandra-
  namespace: kasten-io
spec:
  subject:
    kind: Policy
    name: cassandra-test-backup
    namespace: kasten-io
EOF
```

Check the prehook output. Every node must report `flushed and synced`; if the lines are missing,
the prehook did not run (see behaviour 2) and the snapshots were not flushed.

```bash
kubectl logs -n kasten-io deploy/kanister-svc --since=10m \
  | jq -r 'select((.Pod_Out? // "") | test("flushed|datacenter")) | .Pod_Out'
# datacenter dc1: 3 PVC(s): server-data-demo-dc1-r1-sts-0 server-data-demo-dc1-r2-sts-0 server-data-demo-dc1-r3-sts-0
# demo-dc1-r3-sts-0: flushed and synced
# demo-dc1-r2-sts-0: flushed and synced
# demo-dc1-r1-sts-0: flushed and synced
# datacenter dc1: 3 node(s) flushed and synced; safe to snapshot
```

## Corrupt the data

```bash
cql "CONSISTENCY QUORUM; DELETE FROM shop.customers WHERE id IN (1,2,3);
     UPDATE shop.customers SET city='CORRUPTED' WHERE id=4;"
cql "SELECT * FROM shop.customers;"     # 2 rows, David in CORRUPTED
```

## Restore

### Manual pre-restore step (required)

Delete the datacenter. The operator deletes its pods **and its PVCs**, which is what lets Kasten
restore the volumes cleanly. The superuser Secret is not deleted.

```bash
kubectl delete cassdc dc1 -n cassandra-test --wait=true
kubectl get pods,pvc -n cassandra-test -l cassandra.datastax.com/datacenter=dc1   # wait until empty
```

### Restore, excluding the StatefulSets

```bash
kubectl get restorepoint -n cassandra-test
kubectl create -f - --validate=false <<'EOF'
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RestoreAction
metadata:
  generateName: restore-
  namespace: cassandra-test
spec:
  subject:
    apiVersion: apps.kio.kasten.io/v1alpha1
    kind: RestorePoint
    name: <RESTORE_POINT_NAME>
    namespace: cassandra-test
  targetNamespace: cassandra-test
  filters:
    excludeResources:
      - group: apps
        resource: statefulsets     # REQUIRED — see behaviour 3
EOF
```

In the Kasten dashboard, the same is done by deselecting the StatefulSets in the list of resources
to restore.

If you forget the filter and the restore stops at about 94%: apply
[cassandradatacenter.yaml](cassandradatacenter.yaml) by hand. The operator starts Cassandra in the
waiting pods and Kasten continues. The restored StatefulSets have no owner reference; delete them
and let the operator recreate them the next time you delete the CR.

### Verify

```bash
cql "CONSISTENCY ALL; SELECT * FROM shop.customers;"     # 5 rows, David in Lille
kubectl get sts -n cassandra-test -o jsonpath='{range .items[*]}{.metadata.name} owner={.metadata.ownerReferences[0].kind}{"\n"}{end}'
# demo-dc1-r1-sts owner=CassandraDatacenter   (and r2, r3)
kubectl logs -n kasten-io deploy/kanister-svc --since=20m \
  | jq -r 'select((.Pod_Out? // "") | test("repair complete")) | .Pod_Out'
# datacenter dc1: repair complete on 3 node(s)
```

## Validation

| Test | Result |
|---|---|
| Step 3 — flush, CSI snapshot, delete the CR, restore PVCs by hand | 5/5 rows back at `CONSISTENCY ALL`; host IDs kept across the IP change |
| Step 5 — Kasten backup and restore, 5 rows | 5/5 rows back at `CONSISTENCY ALL`; StatefulSets recreated by the operator; repair on 3 nodes |
| Backup with one node unable to flush | Prehook failed, backup `Failed`, **no restore point created** |
| [Consistency under heavy load](consistency-perf-test/), run 1 (1 GB heap) | 2,184,195 rows acknowledged before the backup; **0 missing, 0 corrupt** after restore |
| [Consistency under heavy load](consistency-perf-test/), run 3 (2 GB heap) | 2,458,025 rows acknowledged before the backup; **0 missing, 0 corrupt** after restore; 0 holes among the 1.39 million rows written during the backup |

Timings under the heavy load (6 load pods, 3 nodes):

| Phase | Run 1, 1 GB heap | Run 3, 2 GB heap |
|---|---|---|
| Load during the backup | fell from 39,000 to 9,000 writes/s | steady at about 37,000 writes/s and 2,300 reads/s |
| Flush + sync, fastest / slowest node | 3 s / 143 s | 2.5 s / 9 s |
| Whole backup | about 3 min | about 1 min |
| Whole restore, including repair | about 9 min | about 6 min |

With the 1 GB heap one node filled its heap, dropped 2.4 million incoming writes and flushed
slowly. Size the heap for your write rate: the flush is only as fast as the slowest node.

## Export deduplication (Step 6)

Measured with a dedicated bucket and an export policy (backup, then export to a Location profile).
The dataset is 200,000 rows of 256 **random** bytes in a keyspace with replication factor 3,
written with [consistency-perf-test/ct.py](consistency-perf-test/ct.py) (`fill 200000`), then 1% of
the rows rewritten with new random payloads (`change 1 200000`) — see
[consistency-perf-test/dedup-job.yaml](consistency-perf-test/dedup-job.yaml).

| Measurement | Value |
|---|---|
| Data on each node's PVC (SSTables) | 55.2 MB |
| Kopia repository after the first export | 60 MiB (+ 146 KiB companion repository) |
| Kopia repository after the 1% rewrite and a second export | 68.2 MiB (+ 271 KiB) |
| **Export growth for the second backup** | **about 8 MiB (13%)** |

Sizes are the object-store repository (`migration/repo/<namespace-uid>/` plus
`migration/<policy-name>/kopia/`). `ExportAction.status.progressDetails` was not reported on this
Kasten version, so there is no `transferredBytes` figure.

What the numbers mean:

- **Three replicas cost about one.** The 3 nodes held byte-identical SSTables (55,195,206 bytes
  each for the original data, 555,699 bytes each for the rewrite), so Kopia stored the data once.
  This holds here because every node received the same writes and nothing had compacted yet. In
  production each node compacts on its own schedule and the files diverge, so expect a smaller
  saving across replicas — but the SSTables of one node still deduplicate from one backup to the
  next.
- **The second backup is incremental.** SSTables are never modified after they are written, so the
  old 55 MB file was not sent again. The rewrite produced one new 0.56 MB SSTable per node (stored
  once). Most of the 8 MiB growth is most likely the commit log (3.4 MB per node, different on each
  node) and Kopia's indexes.
- **Compaction rewrites data.** When Cassandra merges SSTables, the merged file is new content and
  is exported again. The export cost of a backup therefore follows the compaction activity, not
  only the write rate.

## Operational notes

- **`system_auth` has replication factor 1** in this deployment (Cassandra reports it during the
  repair). With RF 1, logins fail when the one node holding a credential is down. Raise it:
  `ALTER KEYSPACE system_auth WITH replication = {'class':'NetworkTopologyStrategy','dc1':3};`
  then `nodetool repair system_auth` on each node.
- **A backup fails when a node is down or unhealthy.** That is deliberate. Cassandra itself
  tolerates a missing node, but a restore point in which one node was silently not flushed is worse
  than a visible failed backup.
- **Keep the datacenter alone in its namespace.** The policy captures the whole namespace. The
  prehook warns about any PVC that does not belong to the datacenter: it is snapshotted without a
  flush.

## Clean up

```bash
kubectl delete cassdc dc1 -n cassandra-test --wait=true
kubectl delete namespace cassandra-test
kubectl delete restorepointcontent -l k10.kasten.io/appNamespace=cassandra-test
kubectl delete policy cassandra-test-backup -n kasten-io
kubectl delete -f blueprintbinding.yaml -f blueprint.yaml
# the operator
kubectl delete subscription cass-operator -n cass-operator
kubectl delete csv -n cass-operator -l operators.coreos.com/cass-operator.cass-operator
kubectl delete namespace cass-operator
```

## Initial prompt

> Can you make a blueprint for Cassandra?

Follow-up requests during the work: *"There is a consistency test that we need to do: put the
cluster under heavy write and read operation, then do a backup and check if the cluster restores
properly."*
