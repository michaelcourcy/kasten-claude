#!/usr/bin/env python3
"""Concept deck for cass-operator-example. Content only — the slide machinery lives in
the shared toolkit at ../../ppt-tools/veeam_deck.py.

    python3 ppt-tools/build.py cass-operator-example
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'ppt-tools'))
from veeam_deck import Deck  # noqa: E402

d = Deck('cass-operator', 'cass-operator-example')

d.title("APACHE CASSANDRA WITH CASS-OPERATOR",
        "Kasten snapshots every node; the blueprint flushes before and repairs after")

d.agenda([
    "The workload and the pattern",
    "What the backup protects, and what it does not",
    "Three Kasten behaviours that shaped the design",
    "Blueprint actions and the restore",
    "Proof under heavy load",
])

d.hero(
    "CASSANDRA ALREADY SURVIVES", "A CRASH ON EACH NODE",
    body="Every write goes to the commit log before it changes the in-memory table. A node "
         "that starts\nfrom a crash-consistent volume replays that log. So a CSI snapshot is "
         "restorable without any dump.",
    pills=["Commit log", "Memtable", "SSTables"],
    icon='database')

d.flow(
    "Pattern 2 — quiesce, in the light form Cassandra allows",
    "Kasten is the data mover; the blueprint never copies data",
    [
        ('checkbox', "DATACENTER READY", "The operator reports the datacenter Ready and not stopped"),
        ('loop', "FLUSH EVERY NODE", "nodetool flush writes memtables out as SSTables, which are never modified again"),
        ('lock', "SYNC — LAST", "The SSTables are on disk, not in the page cache, when the snapshot is cut"),
        ('cloud', "KASTEN SNAPSHOTS", "One CSI snapshot per node PVC; traffic never stops"),
    ],
    note="nodetool drain would stop writes too — but the node then refuses writes until the pod "
         "restarts. That is an outage, not a quiesce.")

d.cards(
    "What the backup protects, and what it does not",
    "Crash-consistent per node, eventually consistent across nodes",
    [
        ('shield', "PROTECTED", [
            "Every write acknowledged at QUORUM before the backup started. It was on at least "
            "2 of 3 nodes when they were flushed, so it is in the snapshots.",
        ]),
        ('time', "BEST EFFORT", [
            "Writes acknowledged while the nodes are snapshotted. Kasten does not cut every "
            "volume at the same instant; the restore repair makes replicas agree again.",
        ]),
        ('close_circle', "NOT PROVIDED", [
            "One point in time across the whole cluster, or restore of a single keyspace or "
            "table. The unit of restore is the datacenter.",
        ]),
    ])

d.table_slide(
    "Three Kasten behaviours that shaped the design",
    "Each made a backup or a restore fail during development — two of them silently",
    ["Behaviour", "What happens", "So the blueprint…"],
    [
        ("A built-in blueprint for every CassandraDatacenter",
         "It creates a MedusaBackupJob, so it assumes K8ssandra with Medusa. With plain "
         "cass-operator every backup fails.",
         "Binds its own blueprint to the CR, which replaces the built-in one"),
        ("Only the topmost owner's blueprint runs — found from a workload",
         "A blueprint on the StatefulSets never runs. Exclude the StatefulSets from the "
         "backup and no hook runs at all: nothing is flushed, the backup still succeeds.",
         "Lives on the CR; the policy keeps the StatefulSets"),
        ("Workloads are restored before custom resources",
         "Pods wait for the operator, the operator waits for the CR, Kasten waits for the "
         "pods: the restore stops at about 94%.",
         "Restore excludes the StatefulSets; the operator recreates them"),
    ],
    col_widths=[3.2, 5.6, 3.5],
    row_heights=[0.5, 1.1, 1.3, 1.1],
    body_size=10)

d.table_slide(
    "Blueprint actions",
    "Bound to the CassandraDatacenter; runs once per datacenter",
    ["Action", "What it does"],
    [
        ("backupPrehook",
         "Waits for the datacenter to be Ready. Lists the datacenter's PVCs — the ones Kasten "
         "snapshots — and the running pods that mount them; a PVC with no running pod fails "
         "the backup. Runs nodetool flush, then sync, on every node in parallel."),
        ("restorePosthook",
         "Waits for the datacenter to be Ready, checks every node is Up/Normal, then runs "
         "nodetool repair --full -pr on each node in turn: -pr repairs each token range "
         "exactly once across the ring."),
    ],
    col_widths=[2.6, 9.7],
    row_heights=[0.5, 1.5, 1.3])

d.two_dark(
    "What a restore involves",
    "One manual step, one filter",
    [
        ("01", "DELETE THE DATACENTER FIRST", "The one manual step",
         "The operator deletes its pods and PVCs. Kasten then restores the PVCs, the "
         "superuser Secret and the CR into a clean namespace. Nodes keep their identity from "
         "the volume, even though every pod gets a new IP."),
        ("02", "EXCLUDE THE STATEFULSETS", "The one restore filter",
         "The operator creates them from the restored CR, they bind to the restored PVCs by "
         "name, each node replays its commit log, and the posthook repairs the ring."),
    ])

d.hero(
    "A NODE THAT CANNOT FLUSH", "FAILS THE BACKUP",
    body="Observed, not assumed: one node ran out of heap and stopped answering. The prehook "
         "reported\nflush failed on at least one node, the backup failed, and no restore point "
         "was created.",
    pills=["No silent partial backup", "Size the heap for the write rate"],
    icon='error_warning', polarity='neg')

d.table_slide(
    "Proof under heavy load",
    "6 load pods writing numbered, checksummed rows at QUORUM; verified at CONSISTENCY ALL after restore",
    ["Measurement", "Run 1 — 1 GB heap", "Run 3 — 2 GB heap"],
    [
        ("Load during the backup", "fell from 39,000 to 9,000 writes/s", "steady at 37,000 writes/s"),
        ("Flush, slowest node", "143 s", "9 s"),
        ("Rows acknowledged before the backup", "2,184,195", "2,458,025"),
        ("Missing / bad checksum after restore", "0 / 0", "0 / 0"),
        ("Export growth after a 1% change", "—", "about 8 MiB on a 60 MiB repository"),
    ],
    col_widths=[4.4, 3.9, 4.0],
    row_heights=[0.5, 0.55, 0.55, 0.55, 0.55, 0.55])

d.split_dark(
    "Before you use it",
    "Adapt, then\n", "re-validate.",
    [
        ("01", "TIME BUDGET",
         "Kasten stops a hook after timeout.blueprintResourceHooks minutes (default 20). "
         "The repair grows with data: raise the timeout, or repair outside Kasten."),
        ("02", "ONE DATACENTER PER NAMESPACE",
         "The policy captures the whole namespace. Any other PVC in it is snapshotted "
         "without a flush; the prehook warns about it."),
        ("03", "RAISE system_auth REPLICATION",
         "It was 1 here, so a login can fail when one node is down. Set it to 3."),
    ],
    left_body="The directory is suffixed -example on purpose: it encodes this operator "
              "version, this CR layout, this storage class and this consistency level.")

d.ending("Flush, snapshot, repair.",
         "Kasten moves the data. The blueprint makes each snapshot cheap to recover from, "
         "and makes the ring agree again after a restore.")

d.save()
