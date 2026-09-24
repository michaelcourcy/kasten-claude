# Consistency test under heavy load

This test answers one question: **if Kasten backs up the Cassandra datacenter while the cluster is
under heavy write and read load, does the restore bring back every write that was acknowledged
before the backup started?**

It runs against the datacenter and blueprint in the parent directory
([cass-operator-example](../)). Deploy those first, as described in the parent
[README.md](../README.md).

## How the test works

Several load pods run in the Cassandra namespace. Each pod is one **writer**. It writes numbered
rows (`seq` = 1, 2, 3 …) to its own part of a ledger table, with 64 writes in flight at once, at
consistency `QUORUM`. Every row carries a **CRC32 checksum** of its content.

| Term | Meaning |
|---|---|
| **acked** | For one writer, the highest `seq` such that **every** row from 1 to `seq` has been acknowledged at `QUORUM`. A failed write is retried with the same `seq` until it succeeds, so the ledger has no holes of its own. |
| **watermark** | The `acked` value of every writer, captured **just before** the backup is triggered. |

Four reader threads in each pod read random rows at or below `acked` and check their checksum while
the cluster is running. That adds read load, and catches corruption on the live cluster.

### Why the watermark is the right test

Every row at or below the watermark was acknowledged at `QUORUM` before the blueprint's
`backupPrehook` started. So it was in the memtable (or already in an SSTable) of at least 2 of the
3 nodes when `nodetool flush` ran on them. The flush writes it to an SSTable and `sync` puts that
file on disk before Kasten takes the snapshots. Every such row **must** be in the restore point.

Rows written **after** the watermark are written while the nodes are flushed and snapshotted, and
Kasten does not snapshot all volumes at the same instant. Whether each one is in the restore point
depends on when each node's snapshot was cut. The test counts them and reports holes among them,
but does not judge them.

### Pass rule

| Check, after restore and repair, read at `CONSISTENCY ALL` | Required |
|---|---|
| Rows at or below the watermark that are missing | **0** |
| Rows whose content does not match their checksum | **0** |
| Rows written after the watermark | reported |
| Holes among the rows written after the watermark | reported |

## Files

| File | Purpose |
|---|---|
| [ct.py](ct.py) | The test program. Modes: `schema`, `load`, `watermark`, `verify`; and `fill`, `change` for the deduplication measurement in the parent README. |
| [schema-job.yaml](schema-job.yaml) | Job that creates the keyspace and tables **once**, and waits until all nodes agree on the schema. |
| [load.yaml](load.yaml) | Deployment of the load pods (6 by default), label `consistency-test=load`. |
| [verify-job.yaml](verify-job.yaml) | Job that checks the restored ledger against the watermark. |
| [dedup-job.yaml](dedup-job.yaml) | Job that runs `fill` or `change` once, for the [export deduplication](../README.md#export-deduplication-step-6) measurement. |

The pods use `python:3.11-slim` and install `cassandra-driver==3.29.2` at startup, so the cluster
needs access to PyPI. Python 3.11, not 3.12: without its optional `libev` extension the driver falls
back to the `asyncore` module, which Python 3.12 removed.

Each run uses its own keyspace, set by `CT_KEYSPACE` in the three manifests. Change it before each
run. Do not drop the previous one while the cluster is busy — see [Findings](#findings).

## Run the test

```bash
NS=cassandra-test
cd consistency-perf-test

# 1. The script, then the schema — once
kubectl create configmap ct-script -n $NS --from-file=ct.py --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f schema-job.yaml
kubectl wait -n $NS --for=condition=complete job/ct-schema --timeout=300s
kubectl logs -n $NS job/ct-schema | tail -1
# schema ready: keyspace consistency_run3, tables ledger and progress, all nodes agree

# 2. The load
kubectl apply -f load.yaml
kubectl logs -n $NS -l consistency-test=load --tail=1 --prefix     # after a minute
# ... acked=356835 writes/s=5441 reads/s=369 write_errors=0 read_errors=0 read_check_failures=0

# 3. Watermark, then IMMEDIATELY the backup
POD=$(kubectl get pods -n $NS -l consistency-test=load -o jsonpath='{.items[0].metadata.name}')
KS=$(kubectl get deploy ct-load -n $NS -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="CT_KEYSPACE")].value}')
kubectl exec -n $NS $POD -- env PYTHONPATH=/tmp/pylib CT_KEYSPACE=$KS python /app/ct.py watermark > watermark.json
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

Keep the load running until the backup is `Complete`. **Watch the disks** while it runs — at about
37,000 writes/s each node's disk grows quickly, because with replication factor 3 every node holds
all the data:

```bash
for r in r1 r2 r3; do kubectl exec -n $NS demo-dc1-$r-sts-0 -c cassandra -- df --output=pcent /var/lib/cassandra | tail -1; done
```

```bash
# 4. Save the load logs BEFORE stopping the load — the logs go away with the pods
kubectl logs -n $NS -l consistency-test=load --tail=-1 --prefix > load.log
grep -c 'READ CHECK FAILED' load.log
kubectl scale deploy ct-load -n $NS --replicas=0

# 5. Restore — the manual step, then the restore without the StatefulSets and without the load
kubectl delete cassdc dc1 -n $NS --wait=true
kubectl get pods,pvc -n $NS -l cassandra.datastax.com/datacenter=dc1     # wait until empty
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
        resource: statefulsets
      - group: apps
        resource: deployments
        matchLabels:
          consistency-test: load     # new writers must not start while the cluster is repaired
EOF

# 6. Verify, once the restore is Complete (restorePosthook has repaired the ring)
kubectl create configmap ct-watermark -n $NS --from-file=watermark.json --dry-run=client -o yaml | kubectl apply -f -
kubectl delete job ct-verify -n $NS --ignore-not-found
kubectl apply -f verify-job.yaml
kubectl logs -n $NS job/ct-verify -f
```

## Results

Cassandra `5.0.5`, 3 nodes (one per availability zone), replication factor 3, Kasten `9.0.6`,
Azure Disk. 6 load pods, 64 writes in flight each.

| Run | Heap | Load during backup | Flush, fastest / slowest node | Backup | Rows below watermark | Missing | Bad checksum | Rows after watermark | Holes after watermark | Result |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 GB | fell from 39,000 to 9,000 writes/s | 3 s / 143 s | about 3 min | 2,184,195 | **0** | **0** | 3,309,160 | 2 | **PASS** |
| 2 | 1 GB | — | one node never finished | **Failed** | — | — | — | — | — | not a consistency result (see below) |
| 3 | 2 GB | steady at about 37,000 writes/s, 2,300 reads/s | 2.5 s / 9 s | about 1 min | 2,458,025 | **0** | **0** | 1,390,243 | 0 | **PASS** |

Run 3 restore: about 6 minutes in total — volumes, about 3 minutes until the datacenter was
`Ready`, then 44 s of repair on the 3 nodes. After the restore, a keyspace created on one node
appeared on all three, and dropping it from another node removed it everywhere: schema changes
propagate normally on the restored cluster.

**Run 2** is the useful negative case. One node ran out of heap (its old generation full, 6-second
garbage-collection pauses), stopped answering, and could not be flushed. The prehook reported
`ERROR: flush failed on at least one node`, the backup failed, and **no restore point was created**.
That is the behaviour the blueprint promises: a node that could not be flushed never ends up in a
restore point.

## Findings

1. **Size the heap for the write rate before you test.** With a 1 GB heap, one node spent seconds
   in each garbage collection, dropped 2.4 million incoming writes, took 143 s to flush in run 1,
   and could not flush at all in run 2. With 2 GB, the same load flushed in under 10 s on every
   node and throughput did not change during the backup.
2. **Transient read failures on the live cluster in run 1.** 14 reads — all from one writer — found
   a row that had been acknowledged missing (12) or with a wrong checksum (2); every one of those
   rows was present and correct when read again seconds later. Run 1 logged no timestamps, so they
   cannot be placed relative to the flush. They coincided with the node whose heap was exhausted.
   Run 3, with 2 GB, had none. The program now records a timestamp, the coordinator, and a re-read
   at `ALL` after 1 s and 6 s for every such failure.
3. **The restored cluster recovers exactly as a crashed one does.** On startup the nodes rolled back
   compactions that were unfinished at snapshot time (`Unfinished transaction log, deleting …`) and
   discarded a half-written SSTable (`Removing orphans for …`). Both are expected.
4. **Do not run schema changes on a cluster that is replaying hints or repairing.** After run 1 a
   `DROP KEYSPACE` issued during heavy hint replay reached only one node, and the nodes later
   disagreed about the schema. The output of that command had been discarded, so whether it
   returned a timeout is not known. This is why each run uses a new keyspace, and why the schema is
   created once by a Job rather than by every load pod.
