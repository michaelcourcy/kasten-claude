# DataStage on Cloud Pak for Data — engine-state protection with Kasten

> **CP4D 5.4.** This is the 5.4 version of the blueprint. Other versions are listed in [../README.md](../README.md).

Protect the **DataStage parallel-engine state** inside an existing Cloud Pak for Data (IBM Software
Hub) installation: custom libraries, ODBC/JDBC driver configuration, compiled job artifacts, run
history, Data Set descriptors and Data Set data. Kasten snapshots the DataStage engine volumes; the
blueprint runs IBM's own health precheck, flushes the filesystems before the snapshot, and takes the
operator out of maintenance mode after a restore.

> **This blueprint deliberately does not protect DataStage flows and jobs.** Those are CP4D
> **project assets**, not engine state, and a project export/import restores them — verified, see
> [What DataStage stores, and where](#what-datastage-stores-and-where). Use
> [cp4d-example](../../cp4d-example/5.4/) for that half. The two blueprints are complementary and are
> deployed independently; [Restore](#restore) shows how they combine in a full rebuild.

---

## Architecture

```
                    cpd namespace (CP4D instance)
   ┌──────────────────────────────────────────────────────────────--─┐
   │  8 DataStage service Deployments      PXRuntime "ds-px-default" │
   │  (assets, flows, runtime, canvas,     ┌──────────────────────-┐ │
   │   caslite, migration, metrics,        │ px-runtime (conductor)│ │
   │   ds-nginx)                           │ px-compute-0/1        │ │
   │            │                          └───────────┬──────────-┘ │
   │            ▼                                      ▼             │
   │   /ds-storage  (100Gi RWX)                /px-storage (10Gi RWX)│
   │   compiled flows+jobs, run logs,          Data Set/File Set DATA│
   │   Data Set descriptors, user-lib,         (pds_files/node*),    │
   │   connectors/odbc                         engine + WLM config   │
   └───────────────────┬───────────────────────────────┬────────────-┘
                       │  Kasten snapshots these two PVCs (labelled)
        backupPrehook  │                               │  restorePosthook
   precheck dsStatus + │                               │  leave maintenance,
   sync every writer   │                               │  wait for Completed
                       ▼                               ▼
                    Kasten policy on the cpd namespace, filtered
```

> **Engine vocabulary.** *Conductor*, *player*, *node* and *score* are parallel-engine terms, and
> "node" in DataStage means a parallel processing node, not a Kubernetes node. They are defined in
> [install-note.md → The three process types](install-note.md#the-three-process-types--the-vocabulary-the-rest-of-this-file-uses).
> The short version: the conductor coordinates and writes no data, the players do the work and are
> the only processes that write to the volumes this blueprint protects.

**Pattern:** *pattern 2 — quiesce*, in the light form IBM itself specifies. There is no application
freeze, because IBM's own **online** backup hooks for DataStage ask for almost nothing. The
ConfigMap `datastage-maint-aux-ckpt-cm`, shipped by the install, requires a precheck that the
`DataStage` and `PXRuntime` custom resources report `dsStatus == Completed`, with `on-error: Fail`.
Since 5.4 it also asks for a **backup lock**: a file created before the snapshot and removed after
it (see [The backup lock](#the-backup-lock--new-in-54)). On 5.2.2 its backup hooks were empty.
Maintenance mode belongs to the **offline** path and to restores, not to a routine backup.

That ConfigMap is one of three that together specify how DataStage must be protected, and this
blueprint is a transcription of them. The full rule-by-rule mapping — including the one place the
blueprint deliberately departs from IBM, and the one requirement that comes from Kasten rather than
IBM — is in [Where this design comes from](#where-this-design-comes-from--ibms-own-backup-hooks).
**Read that section before adapting this blueprint.**

---

## Where this design comes from — IBM's own backup hooks

**Almost nothing in this blueprint was invented.** The DataStage install ships three ConfigMaps that
IBM's own backup tool, `cpdbr`, reads to protect DataStage. They are a machine-readable
specification of what must happen around a snapshot, written by the people who wrote DataStage. This
blueprint is a transcription of them into Kanister.

Read them on your own cluster before adapting anything — they are versioned (`version: 5.4.7` here)
and IBM **does** change them between releases — see the 5.2.2 → 5.4.7 differences below:

```bash
kubectl get cm datastage-maint-aux-qu-cm   -n cpd -o yaml   # quiesce / unquiesce
kubectl get cm datastage-maint-aux-br-cm   -n cpd -o yaml   # OFFLINE backup and restore
kubectl get cm datastage-maint-aux-ckpt-cm -n cpd -o yaml   # ONLINE (checkpoint) backup
```

### The mapping, rule by rule

| IBM rule, and where it is written | What it says | Where it is in this blueprint |
|---|---|---|
| `ckpt-cm` → `precheck-meta.backup-hooks` | `check-condition {$.status.dsStatus} == {"Completed"}` on `DataStage` and on `PXRuntime`, **`on-error: Fail`** | `backupPrehook` step 1. Fails the backup before anything is snapshotted |
| The same rule's `PXRuntime` entry has **no `name:` field** but a label, `icpdsupport/addOnId=datastage` (the `DataStage` entry has `name: datastage`). 5.2.2 had no label either | It applies to **every** `PXRuntime` CR carrying that label, not a named one | `backupPrehook`, `restorePosthook` and [prepare-restore.sh](prepare-restore.sh) loop over `kubectl get pxruntime -l icpdsupport/addOnId=datastage` rather than assuming `ds-px-default` |
| `ckpt-cm` → `backup-meta.pre-hooks` (**new in 5.4**) | In the `flows` pod, container `flows`: `mkdir -p /ds-storage/PXRuntime/Projects && touch /ds-storage/PXRuntime/Projects/.backup_lock` | `backupPrehook` step 5, the same command in the same pod and container, **before** the flush |
| `ckpt-cm` → `backup-meta.post-hooks` (**new in 5.4**) | In the same container: `rm -f /ds-storage/PXRuntime/Projects/.backup_lock` | `backupPosthook` |
| `ckpt-cm` → `checkpoint-meta.exec-hooks` | `exec-rules: []` — **empty** (on 5.2.2 the pre- and post-hooks were empty too) | There is no quiesce phase at backup time beyond the lock |
| `ckpt-cm` → `aux-meta.managed-resources: []` | The online path scales nothing down | The backup does not touch the workloads |
| `br-cm` → `restore-meta.post-hooks` | `cpdbr.cpd.ibm.com/disable-maint` on `DataStage` and `PXRuntime`, `statusFieldName: dsStatus`, `timeout: 1800s` | `restorePosthook`: sets `ignoreForMaintenance: false` on both, then waits for `dsStatus == Completed`. The 1800 s timeout in the blueprint is IBM's number, not a guess |
| `br-cm` → `aux-meta.managed-resources` | `deployment app.kubernetes.io/name=datastage`, `deployment app.kubernetes.io/component=px-runtime`, `statefulset app.kubernetes.io/component=px-compute` | [prepare-restore.sh](prepare-restore.sh) scales these three selectors down before the restore, as `cpdbr` does. They are **not** in the policy filter — see [Three things about Kasten filters](#three-things-about-kasten-filters-that-will-cost-you-a-backup) |
| `br-cm` → `backup-validation-meta` | The backup must contain the ConfigMap `datastage-ibm-datastage-config-cm` | **Not captured** — see the note below |
| `br-cm` / `ckpt-cm` → `plan-meta.groups` | The resources worth capturing are the `ds-config` ConfigMaps and the `DataStage` + `PXRuntime` CRs. Since 5.4 also `datastage-operator-resources`: the operator's Deployments, Services, ServiceAccounts and RBAC (`component-id,icpdsupport/addOnId=datastage`), because the operator is now installed by Helm in the instance, not by OLM | **Not captured** — see the note below |
| `qu-cm` → `quiesce-meta` / `unquiesce-meta` | Quiesce **is** `enable-maint` and nothing else: no database flush, no application-level freeze. Unquiesce is `disable-maint` | Confirms that "quiesce" here costs nothing at the data layer, which is why pattern 2 is cheap for DataStage |

### The one place this blueprint deliberately differs

IBM splits backup into two modes; this blueprint takes the backup from one and the restore from the
other:

| | IBM's online path (`ckpt-cm`) | IBM's offline path (`br-cm`) | This blueprint |
|---|---|---|---|
| Backup | precheck only, no maintenance mode | `enable-maint` before, `disable-maint` after | **online** — precheck only |
| Restore | `restore-meta.post-hooks` is **empty** | `disable-maint` + wait for `Completed` | **offline** — `disable-maint` + wait |

The reason is what a Kasten restore actually does: it **replaces the PersistentVolumeClaims**. The
workloads must be down while that happens, which is the offline situation, so the offline path's
restore hook is the correct one. IBM's online restore hook is empty because `cpdbr`'s checkpoint
restore does not replace volumes. Taking the empty one would leave the operator parked in
maintenance mode after every restore.

This is also why [Restore](#restore) starts with a **manual** step that does what IBM's offline path
does: `enable-maint`, then scale down the `managed-resources` listed in `br-cm`. IBM's offline backup
hook would have done both, and the online backup this blueprint performs does neither.

> **Why the `ds-config` ConfigMap and the custom resources are not captured.** IBM validates that
> `datastage-ibm-datastage-config-cm` is present in a `cpdbr` backup, and its restore plan puts it
> back in the `restore-pre-operators` phase, before the operators run. That is right for `cpdbr`,
> which restores the whole service. This blueprint restores **volumes only**, into a DataStage that
> is already installed (see [Restore](#restore)), so:
>
> - The ConfigMap (three keys: `pxDefaultInstanceID`, `startup-env.sh`, `import_license.sh`) is
>   created and kept up to date by the operator. A restore would bring nothing the install does not
>   already have.
> - The operator resources (new in 5.4) are the operator itself. A restore into an installed
>   DataStage already has them, at the right version.
> - The `DataStage` and `PXRuntime` CRs would be **harmful** to restore. If DataStage was upgraded
>   after the backup, applying the old `DataStage` spec can ask the operator to go back to the old
>   version.
>
> An earlier version of the policy captured all three "for reference" and never restored them.
> That made the restore point look more complete than it was, and anyone restoring it without the
> documented filter would have applied the CRs over the live install.

### The backup lock — new in 5.4

On 5.4.7, IBM's online backup creates `/ds-storage/PXRuntime/Projects/.backup_lock` before the
snapshot and removes it afterwards. The blueprint does the same. What the file actually does was
read from the 5.4.7 `flows` service code (`OnlineBackupManager`, `BackupSyncFilter` in
`ds-common-library-utils` and `ds-flows`), because IBM's ConfigMap only says *what* to run:

| Question | Answer |
|---|---|
| Who reads it | The `flows` service, on each request to `data_intg_flows` |
| What it blocks | Every request that is not a `GET` — creating, saving or deleting a flow — fails with `HTTP 409 "Online Backup is in progress, please wait for a while and retry."`. `GET` requests pass. The filter is on the flows API only; nothing in it touches the job runtime |
| When it is active | **Only when the `flows` container runs with `ENABLE_BACKUP_DETECT=true`.** Nothing sets that variable on a default 5.4.7 install (checked on every Deployment, StatefulSet and ConfigMap in the namespace), so there the lock is created and removed and nothing reads it |
| What happens if it is never removed | The `flows` service deletes a lock older than **4 hours**, checking every 30 minutes. A backup that dies between `backupPrehook` and `backupPosthook` therefore blocks flow edits for at most about 4.5 hours, and only where detection is enabled |

Three consequences for the blueprint:

1. **The lock is taken before the flush.** It is a file on `/ds-storage`, so writing it after the
   `sync` would dirty the page cache again.
2. **The restore point contains the lock.** The snapshot is taken while it exists.
   `restorePosthook` deletes it once the `flows` pod is back; otherwise, with detection enabled,
   flows would stay read-only for up to 4 hours after every restore.
3. **The flows pod must be running for a backup.** `backupPrehook` fails if it is not, because it
   cannot take the lock. The precheck already requires `DataStage` to be `Completed`, so a backup
   that passes the precheck has a running `flows` pod.

### Where the sync comes from — nowhere

One thing in this blueprint has **no** counterpart in IBM's hooks: the `sync` in `backupPrehook`.
IBM's specification is written for `cpdbr`, which performs its own volume copy, so a page-cache
flush is not its concern. Kasten takes a **CSI snapshot of the block device** without `fsfreeze`, so
data the writer has acknowledged but the kernel has not flushed is simply absent — the file appears
in the restore point with the right name and zero bytes. That requirement comes from Kasten's
mechanics, not IBM's, which is why it is not in the table above. See
[AGENTS.md](../../AGENTS.md#always-sync-the-filesystem-before-kasten-snapshots-the-pvc).

---

## Versions

Detected on the cluster where this blueprint was last tested (2026-10-16), after upgrading CP4D
from 5.2.2 to 5.4.0 Patch 7. It was first developed on OpenShift 4.18.6 / Kubernetes 1.31.6 /
Kasten 9.0.5 / CP4D 5.2.2 with the `nfs-csi` storage class. The last revision validated on CP4D 5.2
is the git tag [`cp4d-5.2`](https://github.com/kastenhq/blueprint-ai/tree/cp4d-5.2).

| Component | Version |
|---|---|
| Kubernetes | 1.34.9 |
| OpenShift | 4.21.30 |
| Kasten | 9.0.7 (OLM operator `k10-kasten-operator-rhmp.v9.0.7`) |
| Storage | ODF `ocs-storagecluster-cephfs`, snapshot class `ocs-storagecluster-cephfsplugin-snapclass` |
| Cloud Pak for Data / IBM Software Hub | 5.4.0 Patch 7 (`ibmcpd-cr` 5.10.7, ZenService 6.10.7) |
| DataStage | 5.4.7 — component `datastage_ent`, Helm chart `datastage-ent` 12.0.7 (on 5.4 the operator is installed by Helm, no longer by OLM) |
| Common Core Services (prerequisite of DataStage) | 13.0.6 |
| `cpdctl` | 1.10.34 |
| `cpd-cli` | 14.4.0 (EE) |
| Tool image | `ghcr.io/kastenhq/blueprint-ai/kasten-tools:9.0.7` ([Dockerfile](../../images/kasten-tools/Dockerfile)) |

Detect your Kasten version:

```bash
helm ls -n kasten-io
# or (OpenShift OLM): kubectl get csv -n kasten-io -o jsonpath='{.items[?(@.spec.displayName=="Kasten K10")].spec.version}'
```

> **Tool image note.** The blueprint references `kasten-tools:9.0.7`, matching the Kasten version
> above, and the 2026-10-16 test ran with exactly that image. (The 2026-10-15 test had to use
> `9.0.5`, because `9.0.7` was not published yet.) CI publishes a tag for every referenced
> version, but only on a push to the default branch — the workflow sets
> `push: ${{ github.event_name != 'pull_request' }}` — so while a blueprint is still in review its
> tag builds green **without being pushed**, and a `backupPrehook` referencing it fails with
> `ImagePullBackOff`. If you are developing a blueprint against a version CI has not published yet,
> run it against the newest published tag until yours merges.

---

## Repository layout

| Path | What |
|---|---|
| [install-note.md](install-note.md) | A record of the environment this blueprint was validated against: what the DataStage install creates (pods, PVCs, volume layout) and IBM's `cpdbr` maintenance ConfigMaps. **Not installation documentation — use [IBM's](https://www.ibm.com/docs/en/software-hub/5.4.x) for that** |
| [minimal-tutorial.md](minimal-tutorial.md) | What DataStage is, every object it has and **which store each one lives in**, plus a small ETL job between two PostgreSQL tables with its artifacts — and the copies of its connection — measured on disk. The evidence behind this blueprint's scope. **Not DataStage documentation — use [IBM's](https://www.ibm.com/docs/en/software-hub/5.4.x)** |
| [blueprint.yaml](blueprint.yaml) | The blueprint: `backupPrehook` (precheck + backup lock + flush), `backupPosthook` (release the lock), `restorePosthook` (leave maintenance, remove the restored lock) |
| [blueprintbinding.yaml](blueprintbinding.yaml) | Binds it to the single DataStage `runtime` Deployment |
| [policy.yaml](policy.yaml) | Kasten policy on the `cpd` namespace, filtered to the labelled PVCs and the bound `runtime` Deployment |
| [prepare-restore.sh](prepare-restore.sh) | Restore Step 1: parks the operator and blocks until `dsStatus == InMaintenance`, checks for in-flight jobs, the PVC label and the restore point, then scales the DataStage workloads down and waits until no pod mounts a labelled PVC |
| [fixture/make-flow.py](fixture/make-flow.py) | Generates the example DataStage flow this README's Kasten test uses (and documents the flow-JSON format) |
| [fixture/customers-etl.json](fixture/customers-etl.json) | The generated flow: self-contained, writes only to the engine volumes |
| [fixture/customers-by-country.json](fixture/customers-by-country.json) | The tutorial's flow: PostgreSQL table → Aggregator → PostgreSQL table + Data Set, to show connections and what lies outside this backup |
| [fixture/build-flow.py](fixture/build-flow.py) | How that flow was built, with IBM's watsonx.data integration Python SDK |

---

## What DataStage stores, and where

Three stores, measured on the cluster rather than taken from documentation.

| # | Store | What is in it | Protected by |
|---|---|---|---|
| 1 | **CP4D project assets** | flows, jobs, connections, parameter sets, table definitions, project settings, runtime environments, Data Set *assets* | [cp4d-example](../../cp4d-example/5.4/) — a project export/import |
| 2 | **DataStage engine PVCs** | `/ds-storage` (100 Gi RWX): compiled flows and jobs, run history and logs, Data Set **descriptors**, `user-lib` custom libraries, `connectors/odbc`. `/px-storage` (10 Gi RWX, one per runtime instance): Data Set / File Set **data** in `pds_files/node*`, files written to file-system paths, engine and workload-manager configuration. Plus any **user volume** (`volumes-<name>-pvc`) an administrator mounted into the runtime for flows doing plain file I/O | **this blueprint**, for every PVC you label — see [Prerequisites §2](#2-label-the-pvcs-to-protect) |
| 3 | **External data sources** | the databases and files the flows read and write, behind connections | whoever owns that database |

A single Data Set called `customers.ds` exists in stores 1 **and** 2 at the same time: an asset in
the project, a descriptor on `/ds-storage`, and partitioned data files under
`/px-storage/pds_files/node2` and `node3`. One logical object, three places.

### What has no second copy — why this backup matters

The table above says where things live. This one says what is **lost forever** if the engine volumes
go, and what merely has to be re-created. Measured on the cluster, with paths.

**Only on the engine volumes. No project export contains any of it.**

| Content | Path | Why nothing else can produce it |
|---|---|---|
| ODBC data sources | `/ds-storage/connectors/odbc/config/odbc.ini` | hand-maintained; there is no project asset type for it |
| JDBC configuration | `/px-storage/config/jdbc/isjdbc.config` | same |
| Database driver binaries | `/px-storage/dbdrivers/` | where proprietary JDBC/ODBC drivers are placed |
| Parallel-engine certificates | `/px-storage/certs/` — `pxeCA.crt`, `pxeCA.key`, `pxe.crt`, `pxe.key` | the engine's own CA and keypair |
| Instance-level user code | `/ds-storage/user-lib/`, `/ds-storage/snc/`, `/ds-storage/utilityScripts/` (49 files here) | copied onto the volume, not uploaded as assets |
| Run history | `/ds-storage/PXRuntime/Projects/*/jobs/*/runs/` (12 runs on this fixture), `/px-storage/config/wlm/metrics/` | `job.log`, `perf.out`, `mon.log` — audit material, not design |
| Data Set / File Set **data** | `/px-storage/pds_files/node*` | verified absent from **both** export types |
| Files a flow wrote to a path | `/px-storage/data/` (`customers.txt` here), and any user volume | Sequential File stage output; verified absent from both exports |

**Duplicated — a project export also carries it**, so losing the volumes is recoverable:
the compiled artifacts (the `px_executables` attachment on the flow asset *and*
`/ds-storage/…/flows/<id>/`) and the Data Set **descriptors**
(`assets/data_intg_data_set/ds-storage/…/customers.ds` in the export).

#### The ranking is the opposite of what it looks like

The obvious answer is that the **Data Set data** is the precious part, because it is the only actual
data on the volumes. In most deployments it is not, and this blueprint's own behaviour reflects that:

- **Data Sets are intermediate ETL data.** `backupPrehook` *warns* about a job in flight instead of
  failing, precisely because a re-run reproduces them. If the sources are connectors and still
  reachable, every Data Set can be rebuilt.
- **The configuration cannot be rebuilt by anything.** You cannot re-run a job to produce
  `odbc.ini`, `isjdbc.config`, a driver `.jar` or `pxeCA.key`. And without those the connectors do
  not work — so you cannot re-run at all. A 32 KB `connectors/` directory and a 148 KB `config/`
  directory gate the recovery of everything else on the volume.

So the least interesting-looking content is the irreplaceable content. Data Set data becomes
irreplaceable only when a Data Set is treated as a **system of record**, or the upstream source is
gone. That is a property of your flows, not of DataStage — and it is exactly the case in which you
should change that warning to `exit 1` (the blueprint says where).

> **"My sources and targets are all connectors, so the volumes only hold libraries and compiled
> flows."** Two reasons that is not safe to assume:
>
> 1. Even then the volumes still hold the connectivity configuration, the drivers, the
>    certificates, the WLM metrics and the run history — the irreplaceable half of the list above.
> 2. Connectors at the edges does not mean no Data Sets. They are commonly used to stage data
>    **between** flows while the ultimate source and target are databases. A Sequential File stage
>    writing to a path lands on `/px-storage/data` or a user volume no matter what the sources are.

A library created as a project asset travels with the project export and works after import
(verified on 5.4.0 with a parallel function: the imported job ran without recompiling). Only files
copied directly into `/ds-storage/user-lib` need this blueprint.

### Why this blueprint does not use the export commands

CP4D has two commands that export a project. We measured both on the example project
(`customers-etl`, CP4D 5.2.2), and neither can do this blueprint's job:

| Command | What it captured | Use |
|---|---|---|
| `cpdctl dsjob export-project` | DataStage components only (5.1 KB): the flow, the job, and the compiled `px_executables` with `--include-binaries`. **Not** the Data Set asset, the project settings or the runtime environment, even with `--include-data-assets`: it follows the dependency graph outward from the flows, and leaves out anything the flows do not reference | None. It is not a project backup |
| `cpdctl asset export start --assets-all-assets` | The whole project (160 KB, 94 files): every asset's metadata, the bytes of uploaded files, the compiled binaries, the Data Set **descriptor**, and the connections — with their passwords in clear text unless you pass `--encryption-key` (seen again on 5.4.7, [tutorial §4.8](minimal-tutorial.md#48-what-the-datastage-backup-does-not-protect)) | The **project** half. [cp4d-example](../../cp4d-example/5.4/) uses it |

Neither export contains what exists only on the engine volumes: the Data Set **data** in
`/px-storage/pds_files`, files a flow wrote to a path (`customers.txt`), and the instance-level
configuration listed in [What has no second copy](#what-has-no-second-copy--why-this-backup-matters).
That gap is why this blueprint snapshots the volumes instead of calling an export. The export
protects the project; the snapshots protect the engine.

Two further measurements worth knowing:

- **A whole-project export/import restores a working DataStage project.** A project exported with
  `cpdctl asset export start --assets-all-assets` and imported into a new empty project came back
  with its flow reporting `Compiled: true / Need Compilation: false` — the compiled `px_executables`
  travel in the bundle — and **the restored job ran without being recompiled**.
- **Engine storage only grows.** Deleting a flow or a job removes the asset but leaves its directory
  under `/ds-storage/PXRuntime/Projects/<project-id>/`.

> **A Data Set is restored as a pointer, not a copy — correct for a restore, dangerous for a
> clone.** In a real recovery the original is gone, or you are restoring onto another cluster, and a
> descriptor that addresses engine storage is exactly right. The hazard is **duplication**: putting
> a second copy of a project next to the original on the *same* runtime instance.
>
> Verified: after importing into a brand-new project, the descriptor written into that project is
> **byte-identical** to the source project's (`md5 5588c93…` on both), and `view-dataset` on the new
> project returned the five rows **without any job having run in it** — it was reading the source
> project's data files. A Data Set write deletes the existing data files before rewriting them, so
> running the cloned job destroyed the source project's Data Set: `view-dataset` on the
> **original** then failed with `Sample data could not be read from dataset …`.
>
> **Why byte-identical, mechanically.** The `data_intg_data_set` asset does not merely *describe*
> the Data Set — it **carries the descriptor as an attachment**. Three attachments, read off the
> live asset:
>
> ```
> data_intg_data_set_entity   application/json           (metadata)
> customers.ds                application/octet-stream   <- the descriptor BYTES
> customers.ds.schema         application/octet-stream
> object_key: data_intg_data_set/ds-storage/PXRuntime/Projects/<pid>/customers.ds/customers.ds
> ```
>
> Downloading that attachment out of project storage gives **3431 bytes, `md5
> 5588c9325fecc8bf76bb097790c61e86`** — identical to the descriptor on `/ds-storage`. So an export
> moves the descriptor verbatim and an import writes those same bytes back under the *new* project
> id, with the *original* absolute `/px-storage` paths still inside them. That is the whole
> mechanism: the clone does not compute new paths, it inherits the source's.
>
> Project assets are project-scoped; the engine storage they address is shared by every project on
> that runtime instance. On a different cluster the data files are simply absent until a job
> rewrites them, which is the expected outcome of a restore.

---

## Three things about Kasten filters that will cost you a backup

All three were found the hard way on this blueprint. The first two fail **silently** — the backup
reports `Complete`. The third puts things in the restore point that you did not ask for.

**1. A bound blueprint plus a filter that excludes its workload means no volume snapshot.** This
blueprint binds to `datastage-ibm-datastage-runtime`. When the policy filter named the engine PVCs
but not the DataStage workloads, `ds-storage` — the volume that Deployment mounts — was **not
snapshotted**, while `px-storage`, `ds-temp`, `ds-migration` and `conn-home`, mounted by workloads
with no binding, were captured even though the filter did not name them.

Isolated in a two-object test namespace (one PVC, one Deployment) and raised with Kasten
engineering. Measured there, two runs per row:

| Filter on the policy | BlueprintBinding on the workload | VolumeSnapshot |
|---|---|---|
| `includeResources`: the PVC only | **yes** | **none** — restore gives an empty volume |
| `includeResources`: the PVC only | no | 1 |
| `excludeResources`: the Deployment | **yes** | **none** |
| `excludeResources`: the Deployment | no | 1 |
| no filter | yes | 1 |
| no filter | no | 1 |

Every action stays green in the failing row: backup `Complete`, restore point created, restore
`Complete`, PVC `Bound`, volume empty. The workaround is to include the **bound workload** in the
filter — `datastage-ibm-datastage-runtime`, and only that one (see point 3). That is what
[policy.yaml](policy.yaml) does.

`ds-storage` is mounted by seven other DataStage Deployments and by `px-runtime` and `px-compute`,
none of which are in the filter. That does not trigger the defect: verified on 2026-10-15, the
restore point contains `ds-storage`.

**2. If the filter excludes every workload, your hooks never run.** The BlueprintBinding matches a
Deployment. Filter the Deployments out and nothing matches, so `backupPrehook` is never executed —
no precheck, no flush — and the backup still reports success. The first four test runs here were
exactly that: green backups with no hook execution at all. This one is **not** a product defect: a
hook is bound to a workload, and a workload that is not in the backup has nothing to run against.
It is listed here because the consequence for a blueprint author is the same as a bug — you lose
your quiesce and your flush, silently — and because nothing warns you.

A related trap when testing: **a BlueprintBinding selector is not scoped to a namespace.** A binding
on a common label attaches to matching workloads anywhere in the cluster, which is easy to do by
accident and changes the result of unrelated backups.

**3. An included workload brings in every PVC it mounts, labelled or not.** The entries in
`includeResources` add to each other; a PVC label entry cannot remove a PVC that a workload entry
brought in. An earlier version of this policy included all the DataStage workloads (the three
`managed-resources` selectors from IBM's `br-cm`). Every restore point then held **five** volume
snapshots instead of two:

| PVC | Labelled | Brought in by | Problem |
|---|---|---|---|
| `datastage-ibm-datastage-ds-temp-pvc` (20 Gi) | no | Deployment `…-assets` | Scratch space, snapshotted for nothing |
| `datastage-ibm-datastage-ds-migration-pvc` (5 Gi) | no | Deployment `…-migration` | Same |
| `conn-home-pvc` (5 Gi) | no | `px-runtime`, `px-compute` | **Not DataStage's.** It belongs to Common Core Services (`icpdsupport/addOnId=ccs`) and is also mounted by `wdp-connect-connection`, `wdp-connect-connector` and `wdp-connect-flight`. A restore that replaced it would either fail, because those pods still mount it, or put another service's data back in time without stopping that service |

It also made the label claim in [Prerequisites §2](#2-label-the-pvcs-to-protect) false, and the
prehook did not flush those three PVCs on purpose. So the policy includes **only the bound
workload**, which mounts `ds-storage` and nothing else.

That has a consequence at restore time. Kasten can only replace a PVC once no pod mounts it, and it
scales down only the workloads it restores. A `RestoreAction` that does not stop every mount holder
**fails**, loudly:

```
Failed to delete PVC ... datastage-ibm-datastage-ds-storage-pvc
Timeout while polling (1m59s)
```

The restore point holds one workload, so [prepare-restore.sh](prepare-restore.sh) scales the others
down first, with the selectors IBM's `br-cm` lists for exactly this purpose. The operator is parked
at that point, so it does not scale them back up; it does so itself once `restorePosthook` takes it
out of maintenance.

---

## Prerequisites

### 1. DataStage installed in the CP4D instance namespace

This blueprint assumes CP4D is already installed and DataStage added on top
(`cpd-cli manage apply-olm --components=datastage_ent`, then `apply-cr`). That creates 11 pods
(6.5 vCPU / 24.5 GiB of requests) and 4 ReadWriteMany PVCs in the `cpd` namespace.

Install DataStage by following **IBM's official documentation**:
<https://www.ibm.com/docs/en/software-hub/5.4.x> → *Installing* → *Services* → *DataStage*. IBM
owns the supported procedure and keeps it current.

[install-note.md](install-note.md) records what our install produced — the resulting pods and PVCs,
what is on each volume, and IBM's `cpdbr` maintenance ConfigMaps. It is there so you can compare
your installation with the one this blueprint was validated against and see where yours differs,
**not** as an install procedure to follow.

> **Storage class**: DataStage requires an **RWX** class for `/ds-storage` and `/px-storage`. This
> blueprint was tested with `ocs-storagecluster-cephfs` on ODF (snapshot class
> `ocs-storagecluster-cephfsplugin-snapclass`) and, earlier, with `nfs-csi` (`csi-nfs-snapclass`),
> each with a matching `VolumeSnapshotClass` registered with Kasten. Replace it with an RWX class
> that supports CSI snapshots on your cluster (for example `efs-nfs-client` on AWS, `ontap-nas` on
> NetApp Trident). Do **not** use legacy in-tree classes — they do not support CSI snapshots.

### 2. Label the PVCs to protect

The two volumes that matter carry **no labels** on DataStage 5.2.2, while the two scratch volumes
(`ds-temp`, `ds-migration`) do. So the policy selects them by a label you add yourself:

```bash
kubectl label pvc -n cpd \
  datastage-ibm-datastage-ds-storage-pvc \
  ds-px-default-ibm-datastage-px-storage-pvc \
  blueprint-ai/datastage-engine=true --overwrite
```

**This label is the single source of truth.** The policy selects PVCs with it, and `backupPrehook`
reads the same label to decide which pods to flush. Anything you do not label is neither
snapshotted nor flushed.

That holds only because the policy's one workload entry, the `runtime` Deployment, mounts
`ds-storage` and nothing else. If you add a workload to the policy filter, every PVC it mounts is
snapshotted too, labelled or not — see
[point 3 of the filter section](#three-things-about-kasten-filters-that-will-cost-you-a-backup).
After any change to the filter, check that a backup produces one volume snapshot per labelled PVC
and no more.

Two cases where the default two lines are not enough — **check both on your cluster**:

```bash
# a) One px-storage PVC per PXRuntime instance. Add every instance you have:
for px in $(kubectl get pxruntime -n cpd -o jsonpath='{.items[*].metadata.name}'); do
  kubectl label pvc -n cpd "${px}-ibm-datastage-px-storage-pvc" \
    blueprint-ai/datastage-engine=true --overwrite
done

# b) USER VOLUMES. Extra PVCs an administrator mounted into the runtime so flows can read and
#    write ordinary files. They hold customer data, they are NOT created by the DataStage install,
#    and they do not follow the engine naming pattern. List them, then label the ones DataStage uses:
cpdctl dsjob list-volumes                          # what DataStage knows about
kubectl get pvc -n cpd | grep '^volumes-'          # how they appear as PVCs (volumes-<name>-pvc)
```

> ⚠️ **User volumes are easy to miss and they are where a customer's flat files live.** A flow doing
> any plain file I/O probably writes to one. Note that `volumes-*-pvc` PVCs in the `cpd` namespace
> are not necessarily DataStage's — other CP4D services create them too (for example
> `volumes-datarefinerylibvol-pvc` belongs to Data Refinery). Label the ones `cpdctl dsjob
> list-volumes` reports, not every `volumes-*` PVC in the namespace.

The prehook tells you when the labelling is wrong, rather than failing quietly:

```
PVCs carrying blueprint-ai/datastage-engine=true: datastage-…-ds-storage-pvc ds-px-default-…-px-storage-pvc
WARNING: no PVC carries the label. The policy selects on it, so this backup
         probably captures NO volume at all. See Prerequisites in README.md.
WARNING: PVC volumes-myfiles-pvc is labelled for backup but no running pod mounts it; it cannot be flushed
```

The second warning matters: a labelled PVC that no running pod mounts gets snapshotted **without a
flush**, which is exactly the 0-byte-file hazard the flush exists to prevent. Either start the pod
that consumes it, or accept the risk knowingly.

Verified: the label survives both an operator reconciliation and a Kasten restore of the PVC.

### 3. A Location profile

The blueprint's hooks need a **Location** profile (S3, GCS, Azure Blob — not Infra):

```bash
kubectl get profile <name> -n kasten-io -o jsonpath='{.spec.type}'   # must print: Location
```

---

## Deploy

```bash
kubectl apply -f blueprint.yaml
kubectl apply -f blueprintbinding.yaml

# edit the profile name first
kubectl apply -f policy.yaml

kubectl get policy datastage-engine-backup -n kasten-io -o jsonpath='{.status.validation}{"\n"}'   # Success
```

Run it on demand:

```bash
kubectl create -f - <<'EOF'
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RunAction
metadata: { generateName: run-datastage-, namespace: kasten-io }
spec:
  subject: { apiVersion: config.kio.kasten.io/v1alpha1, kind: Policy, name: datastage-engine-backup, namespace: kasten-io }
EOF

# one snapshot per labelled PVC should appear — two on a default install
kubectl get volumesnapshot -n cpd
```

Check the hook actually ran — do not rely on the action being green (see the filter warnings above):

```bash
kubectl logs -n kasten-io -l run=kanister-svc --tail=2000 | grep -E "dsStatus|synced|flushed"
```

---

## Blueprint actions

| Action | Trigger | What it does |
|---|---|---|
| `backupPrehook` | Before Kasten snapshots the volumes | Runs IBM's precheck — the `DataStage` CR and **every** `PXRuntime` CR labelled `icpdsupport/addOnId=datastage` must report `.status.dsStatus == Completed`, otherwise the backup fails before anything is captured. Discovers every running pod that mounts a PVC in the backup — the union of the PVCs carrying `blueprint-ai/datastage-engine=true` (so **user volumes are covered**, and the flushed set cannot drift from the snapshotted set) and anything matching `*-ds-storage-pvc` / `*-px-storage-pvc` (a safety net if the label was forgotten, and it picks up extra PXRuntime instances with no configuration). Warns if no PVC carries the label, and names any labelled PVC that no running pod mounts, since that one will be snapshotted unflushed. Warns if any job run is in flight. Takes IBM's backup lock (5.4) in the `flows` pod, and fails if no `flows` pod is running. Finally runs `sync` in each of those pods — **last**, so nothing dirties the page cache afterwards. |
| `backupPosthook` | After the volume snapshots are ready | Releases IBM's backup lock (`rm -f /ds-storage/PXRuntime/Projects/.backup_lock` in the `flows` pod), as IBM's 5.4 `ckpt-cm` post-hook does. Idempotent. If no `flows` pod is running it only warns: the `flows` service deletes a lock older than 4 hours by itself. |
| `restorePosthook` | After Kasten restores the volumes | Sets `spec.ignoreForMaintenance: false` on the `DataStage` CR and every `PXRuntime` CR (idempotent), then waits up to 1800 s for all of them to report `dsStatus == Completed`, so the restore action does not report success while the engine is still assembling itself. Then deletes the backup lock that the restored `/ds-storage` contains (the snapshot was taken while it existed). Prints the recompile reminder. |

The `backupPosthook` undoes only the lock: the backup path quiesces nothing else. On 5.2.2, where
IBM's backup hooks were empty, the blueprint had no `backupPosthook` at all.

---

## Restore

### Before you start

| Prerequisite | Why |
|---|---|
| Same DataStage / CP4D version as the backup | Compiled artifacts are engine-version specific. A Data Set descriptor embeds the engine build string (`$Version: X86_64 Torrent 2_3 2025/09/18 …`) |
| DataStage installed and reconciled | Nothing restores into a DataStage that is not installed. The install recreates the empty volumes this restore then replaces |
| The external sources behind your connections exist | A connection restores as a definition; the data behind it does not. That data needs its own backup — for example [cnpg-example](../../cnpg-example/) for a CloudNativePG database |
| No copy of the same project running its jobs | See the Data Set warning above |

### Step 1 — prepare the engine (manual, required)

Kasten replaces the PVCs, and two things stand in its way:

- **The DataStage operator** owns the workloads and will fight the restore unless it is parked
  first. `ignoreForMaintenance` pauses reconciliation; it does **not** stop pods — verified on 5.2.2.
- **The pods that mount the PVCs.** Kasten can only delete a PVC once no pod mounts it, and it
  scales down only the workloads it restores. The restore point holds one workload (`runtime`), so
  the other nine must be stopped by hand.

Run [prepare-restore.sh](prepare-restore.sh) (it needs `kubectl` and `jq`). It parks the operator
**and blocks until DataStage is actually ready**, runs its checks, and only then scales the DataStage
workloads down and waits for every mount to be released:

```bash
./prepare-restore.sh <RESTORE_POINT_NAME>      # NS=<namespace> to override the default, cpd
```

```
1/6  parking the operator (ignoreForMaintenance=true) in namespace cpd
2/6  waiting for dsStatus=InMaintenance (IBM's own gate, timeout 1800s)
       datastage/datastage = InMaintenance
       pxruntime/ds-px-default = InMaintenance
3/6  checking for job runs in flight
       none
4/6  checking the PVC label the restore filter selects on
       datastage-ibm-datastage-ds-storage-pvc
       ds-px-default-ibm-datastage-px-storage-pvc
5/6  restore point
       scheduled-848cw found
6/6  scaling down the DataStage workloads (IBM's br-cm managed-resources)
       no pod mounts the labelled PVCs

READY — launch the RestoreAction now (Step 2).
```

From step 6 on, **DataStage is down**. To abandon the restore at that point, set
`ignoreForMaintenance: false` on the `DataStage` CR and on every `PXRuntime` CR; the operator then
scales the workloads back up itself.

**Why a script and not two `kubectl patch` commands.** Patching and waiting have to be one
operation. If they are two, there is a window in which you can park the operator and launch the
`RestoreAction` before the operator has actually stopped reconciling — and it then fights Kasten
over the very PVCs being replaced. The script closes that window, and it exits non-zero rather than
letting you proceed when something is wrong.

What each check is for:

| Check | What it prevents |
|---|---|
| `dsStatus == InMaintenance` | The real gate. **There is no useful time to wait** — on an idle engine this returns in seconds, mid-reconcile it does not. This is the same field IBM's own `enable-maint` builtin polls (`params.statusFieldName: dsStatus`), so the 1800 s / 600 s timeouts in `datastage-maint-aux-qu-cm` are IBM's numbers, not ours |
| job runs in flight | A job that keeps writing to volumes about to be replaced. `backupPrehook` only warns about this; at restore time it matters more |
| the PVC label | A `RestoreAction` whose filter matches no PVC **succeeds and restores nothing** — the same silent-success trap described above for backups |
| the restore point exists | Catches a typo *before* the operator is parked, not after |
| the scale-down, and no pod mounting a labelled PVC | The three selectors are IBM's `br-cm` `managed-resources`. If a pod outside them still mounts a labelled PVC after 600 s — typically a user volume shared with another service — the script names it and stops, because the `RestoreAction` would otherwise fail on `Failed to delete PVC` |

> **What to expect during the restore.** The script prints this too. Measured on 2026-10-15
> (5.2.2) and 2026-10-16 (5.4.7):
>
> 1. **The volume restore is quick:** about 1.5 minutes from `RestoreAction` start to ~92%. The
>    PVCs are already free, so Kasten does not wait for pods to release them.
> 2. **Then a long plateau at ~92%:** about 15 minutes on 5.2.2, about **27 minutes on 5.4.7**. That is `restorePosthook` taking the
>    operator out of maintenance and waiting for `dsStatus` to return to `Completed`, while the
>    operator scales every workload back up to its original replica count. The action withholds
>    success on purpose while the engine reassembles. **Do not scale anything up yourself.**

### Step 2 — restore the volumes and the bound workload

```bash
kubectl get restorepoint -n cpd

kubectl create -f - --validate=false <<'EOF'
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RestoreAction
metadata:
  generateName: restore-datastage-
  namespace: cpd
spec:
  subject:
    apiVersion: apps.kio.kasten.io/v1alpha1
    kind: RestorePoint
    name: <RESTORE_POINT_NAME>
    namespace: cpd
  targetNamespace: cpd
  filters:
    includeResources:
      - resource: persistentvolumeclaims
        matchLabels:
          blueprint-ai/datastage-engine: "true"
      - group: apps
        resource: deployments
        matchLabels:
          app.kubernetes.io/name: datastage
          app.kubernetes.io/component: runtime
EOF
```

The filter matches the backup policy's filter, and the `runtime` Deployment must be in it:
`restorePosthook` is bound to that Deployment, so without it the hook does not run and the operator
stays parked in maintenance mode (the same mechanism as point 2 of the filter section; not tested
separately for restore).

`restorePosthook` then takes the operator out of maintenance and waits for `dsStatus == Completed`.

> On Kasten 8.x, add `profile: {name: <LOCATION_PROFILE>, namespace: kasten-io}` to the
> `RestoreAction`: extraction of the profile from the `RestorePointContent` is unreliable there and
> the action fails **after** the volumes are restored, with `restorePosthook` never running. Fixed
> in 9.0 — this blueprint was tested on 9.0.5 and 9.0.7 with no `profile` field.

### Step 3 — verify the data actually came back

**Do this before anything else, and do it with `view-dataset`.** A Data Set restores as three
independent things, and only a live read exercises all of them.

```bash
export CPDCTL_ENABLE_DSJOB=true
./bin/cpdctl dsjob view-dataset --project-id <PID> --name <dataset>     # the honest check
```

> ⚠️ **`describe-dataset` will lie to you.** It reads its figures out of the descriptor's
> statistics section, and nothing re-validates them against the filesystem. Measured: with the data
> files on `/px-storage` **deleted**, it still reported
>
> ```
> DataSet Size:      140
> DataSet Row Count: 5
> ```
>
> while `view-dataset` failed with `Sample data could not be read from dataset …`. Same for the UI:
> **View schema** succeeds on the descriptor alone, **View data** does not. So a restore that shows
> the right row count has proved nothing. This also gives a clean triage rule — *View schema works,
> View data fails* means `/ds-storage` came back and `/px-storage` did not.

**Check the data-file ids, not the row values.** If the flow is deterministic — the fixture's Row
Generator is — a re-run produces **identical rows under new file ids**. So matching rows do not
distinguish restored data from freshly regenerated data; matching ids do:

```bash
POD=$(kubectl get pod -n cpd -l app.kubernetes.io/component=px-runtime -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n cpd "$POD" -- ls /px-storage/pds_files/node2 /px-storage/pds_files/node3
# e.g. customers.ds.1000830000.10.128.5.251.0000.0000.0000.506fe.6aa720f5.0000.fec37648
#                                                            ^^^^^^^^^^^^^^ unique per WRITE
```

The id changes on every write, so seeing the pre-backup id return is what proves the restore
replaced the bytes rather than the engine rebuilding them.

### Step 4 — recompile and smoke-test

```bash
export CPDCTL_ENABLE_DSJOB=true
./bin/cpdctl dsjob compile --project <project>              # no --name compiles every flow
./bin/cpdctl dsjob run     --project <project> --job <job> --wait 300
```

### Where this fits in a full rebuild

| Scenario | What to do |
|---|---|
| **A — a project or a flow was lost.** Engine is healthy | This blueprint is not involved. Restore the project with [cp4d-example](../../cp4d-example/5.4/), then recompile if `list-flows --with-compiled` says so |
| **B — engine storage damaged, project assets intact** | Steps 1–4 above |
| **C — rebuild from nothing** | 1. cluster + CP4D control plane + `ccs` (fresh install or `cpdbr`) → 2. **install DataStage at the same version** → 3. **engine state first**, Steps 1–3 above (prepare, restore, verify — recompiling comes at 5) → 4. **project assets second**, via cp4d-example → 5. recompile, re-run jobs to rebuild Data Sets → 6. verify connections against the external sources; if you edit one (a new hostname, say), **recompile again** — a job runs with the copy of the connection compiled onto `/ds-storage`, not with the project's connection ([tutorial §4.7](minimal-tutorial.md#47-the-job-runs-with-a-copy-of-the-connection-not-with-the-connection)) |

Engine state must be restored **before** the project import: importing a project makes DataStage
write into `/ds-storage/PXRuntime/Projects/<project-id>/`, and restoring the volume afterwards would
overwrite that. Note also that a restored project receives a **new project id**, so the snapshot's
own `Projects/<old-id>/` subtrees are orphans in a rebuild. What carries value across a rebuild is
the instance-level content: `user-lib`, `connectors/odbc`, `rule-set`, `snc`,
`/px-storage/config/{jdbc,odbc,wlm}`.

---

## Create the example data

A minimum representative DataStage workload — one project, one compiled flow, one job, one
successful run — plus the engine-state artifacts this blueprint actually
protects. Under 5 KB of data. Every step is a CLI command, so the fixture can be recreated by a
script; nothing has to be drawn on the canvas.

### 1. `cpdctl` on your workstation

`cpdctl` is not vendored here (`bin/` is git-ignored). Download the **latest stable** release from
[github.com/IBM/cpdctl/releases](https://github.com/IBM/cpdctl/releases):

```bash
mkdir -p bin && cd bin
TAG=$(gh release view --repo IBM/cpdctl --json tagName -q .tagName)      # e.g. v1.10.34
gh release download "$TAG" --repo IBM/cpdctl --pattern 'cpdctl_darwin_arm64.tar.gz'
tar xzf cpdctl_darwin_arm64.tar.gz && rm cpdctl_darwin_arm64.tar.gz
xattr -d com.apple.quarantine cpdctl 2>/dev/null                         # macOS Gatekeeper
./cpdctl version && cd ..
```

Point it at the cluster. The config file holds a password — keep it in the git-ignored `bin/`:

```bash
export CPDCONFIG=$PWD/bin/.cpdctl.config.json
export CPDCTL_ENABLE_DSJOB=true          # without this, `cpdctl dsjob` does not exist

CPD_HOST=$(oc get route cpd -n cpd -o jsonpath='{.spec.host}')
ADMIN_U=$(oc get secret ibm-iam-bindinfo-platform-auth-idp-credentials -n cpd -o jsonpath='{.data.admin_username}' | base64 -d)
ADMIN_P=$(oc get secret ibm-iam-bindinfo-platform-auth-idp-credentials -n cpd -o jsonpath='{.data.admin_password}' | base64 -d)

printf '%s' "$ADMIN_P" | ./bin/cpdctl config user set admin --username "$ADMIN_U" --password-stdin
./bin/cpdctl config profile set cp4d --url "https://${CPD_HOST}" --user admin
./bin/cpdctl config profile use cp4d
./bin/cpdctl project list
```

### 2. A project, a flow, a job

```bash
./bin/cpdctl project create --name datastage-demo --type cpd --storage-type assetfiles
# -> Location   /v2/projects/<PID>
PID=<PID>

./bin/cpdctl dsjob create-flow --project-id "$PID" --name customers-etl \
    --pipeline-file fixture/customers-etl.json
./bin/cpdctl dsjob compile     --project-id "$PID" --name customers-etl
# customers-etl compiled successfully in 1 seconds.

./bin/cpdctl dsjob create-job --project-id "$PID" --flow customers-etl --name customers-etl-job
./bin/cpdctl dsjob run        --project-id "$PID" --job customers-etl-job --wait 300
# <APT_RealFileExportOperator in customers_txt,0> Export complete; 5 records exported successfully
# Current Job Status: Completed
```

[fixture/customers-etl.json](fixture/customers-etl.json) is a four-stage flow with three columns
(`cust_name`, `country`, `amount`):

```
Row_Generator_1 ──▶ Copy_1 ──┬──▶ customers_txt   Sequential File → /px-storage/data/customers.txt
   (5 records)               └──▶ customers_ds    Data Set        → customers.ds
```

The two targets are chosen on purpose: they write to the **two different engine stores**, so one run
exercises the whole persistence model. The Row Generator source keeps the flow free of any external
dependency. The JSON is generated by [fixture/make-flow.py](fixture/make-flow.py), which documents
the format and the two traps in it — one of them produces a flow that **compiles cleanly and then
fails at run time**. Regenerate with `python3 fixture/make-flow.py > fixture/customers-etl.json`;
identifiers are fixed, so the output is byte-identical every time.

Compiling is a real build step, not a validation: it writes `OshScript.osh` and a `lib/` directory
onto `/ds-storage`. A flow that is restored but never recompiled cannot run.

### 3. Engine-state artifacts — what this blueprint protects

Nothing above lives on the engine volumes except by side effect, so add the three artefacts a real
deployment would have, and record their checksums so a restore can be checked:

```bash
POD=$(kubectl get pods -n cpd -o name | grep px-runtime | head -1)
kubectl exec -n cpd ${POD#pod/} -- bash -c "
  printf 'KASTEN-FIXTURE custom parallel routine library v1\n' > /ds-storage/user-lib/libkasten_fixture.so
  printf '[kasten_fixture_dsn]\nDriver=/ds-storage/connectors/odbc/drivers/libfixture.so\n' > /ds-storage/connectors/odbc/config/odbc.ini.fixture
  printf 'CLASSPATH=/ds-storage/user-lib/kasten-fixture.jar\n' > /px-storage/config/jdbc/isjdbc.config.fixture
  md5sum /ds-storage/user-lib/libkasten_fixture.so \
         /ds-storage/connectors/odbc/config/odbc.ini.fixture \
         /px-storage/config/jdbc/isjdbc.config.fixture \
         /px-storage/data/customers.txt"
```

### 4. See where the bytes went

```bash
POD=$(kubectl get pods -n cpd -o name | grep px-runtime | head -1)
kubectl exec -n cpd ${POD#pod/} -- cat  /px-storage/data/customers.txt
kubectl exec -n cpd ${POD#pod/} -- find /px-storage/pds_files -name 'customers.ds*'
kubectl exec -n cpd ${POD#pod/} -- find /ds-storage/PXRuntime/Projects/$PID -maxdepth 3

./bin/cpdctl dsjob list-flows    --project-id "$PID" --with-id --with-compiled
./bin/cpdctl dsjob list-datasets --project-id "$PID" --with-id
./bin/cpdctl asset search        --project-id "$PID" --type-name asset --query '*:*'
```

---

## Validation

### 2026-10-16 — after upgrading CP4D from 5.2.2 to 5.4.0 Patch 7

Kasten 9.0.7 / OpenShift 4.21.30 / CP4D 5.4.0 Patch 7 / DataStage 5.4.7, storage
`ocs-storagecluster-cephfs`, tool image `kasten-tools:9.0.7`. Same policy and restore procedure as
the 2026-10-15 run below; the blueprint gained IBM's 5.4 backup lock (`backupPosthook` is new).

| Step | Result |
|---|---|
| Survived the upgrade | Both labelled PVCs, the label, the four fixture files and the Data Set data files were untouched. The flow was recompiled and the job re-run on 5.4.7 (`5 records exported`) to get a baseline written by the new engine |
| `backupPrehook` through a `RunAction` | `Complete` in 49 s. Precheck passed (`DataStage=Completed`, `PXRuntime/ds-px-default=Completed`), backup lock taken in the `flows` pod (`ENABLE_BACKUP_DETECT` unset), 8 writer pods synced |
| `backupPosthook` | Lock released; the file was gone afterwards |
| Snapshot scope | **2 volume snapshots**: `ds-storage` and `px-storage` |
| Destruction | Both Data Set data files and the custom library deleted, `view-dataset` failing; a marker file written on each volume **after** the backup |
| `prepare-restore.sh` | All 6 steps passed in 6 minutes |
| Restore through a `RestoreAction` | `Complete` in 28 min (PVCs recreated after 1 min 18 s, then `restorePosthook`) |
| PVCs replaced | Only `ds-storage` and `px-storage` got new UIDs. `ds-temp`, `ds-migration` and `conn-home-pvc` were not touched |
| Data check | All four md5 checksums identical to the baseline, the original Data Set file ids back, both marker files gone, no `.backup_lock` left on the restored volume |
| Functional check | `view-dataset` reads the five rows again; the flow compiles and the job runs (`Current Job Status: Completed`) |

### 2026-10-15 — current policy and restore procedure, on CP4D 5.2.2

Kasten 9.0.7 / OpenShift 4.21.30 / CP4D 5.2.2 / DataStage 5.2.2, storage
`ocs-storagecluster-cephfs`. Policy filtered to the labelled PVCs and the `runtime` Deployment;
restore prepared with the 6-step `prepare-restore.sh`.

| Step | Result |
|---|---|
| `backupPrehook` through a `RunAction` | `Complete` in 43 s. Precheck passed (`DataStage=Completed`, `PXRuntime/ds-px-default=Completed`), 8 writer pods synced |
| Snapshot scope | **2 volume snapshots**: `ds-storage` and `px-storage`. The previous policy produced 5 on the same cluster (see [point 3](#three-things-about-kasten-filters-that-will-cost-you-a-backup)) |
| Destruction | Both Data Set data files deleted; a marker file written on each volume **after** the backup |
| Scale-down | All mounts of the labelled PVCs released after 30 s; the parked operator did not scale anything back up |
| Restore through a `RestoreAction` | `Complete` in 17 min (volumes ~1.5 min, then `restorePosthook`) |
| PVCs replaced | Only `ds-storage` and `px-storage` got new UIDs. `ds-temp`, `ds-migration` and `conn-home-pvc` were not touched |
| `restorePosthook` | Left maintenance mode, waited through `InProgress`, reported `Completed` |
| Workloads | The operator restored all 10 to their original replica counts by itself |
| Data check | The original Data Set file ids came back (restored bytes, not regenerated ones); both marker files gone (the volumes really were replaced) |
| Functional check | `view-dataset` reads the five rows again |

### 2026-09-09 — first validation

Kasten 9.0.5 / OpenShift 4.18.6 / CP4D 5.2.2 / DataStage 5.2.2, storage `nfs-csi`. The policy then
included all the DataStage workloads, and Kasten scaled them down during the restore.

| Step | Result |
|---|---|
| `backupPrehook` through a `RunAction` | Precheck passed (`DataStage=Completed`, `PXRuntime/ds-px-default=Completed`), 8 engine-volume writer pods discovered and synced, one Kanister ActionSet per backup, 13–23 s |
| Snapshot scope | Recorded at the time as "the two labelled engine PVCs". With that filter Kasten also snapshots every PVC the included workloads mount, as the 2026-10-15 run showed, so this row was probably wrong |
| Destruction | Custom library, ODBC file, JDBC file, `customers.txt` and all 6 Data Set data files deleted; `view-dataset` then failed |
| Restore through a `RestoreAction` | `Complete`. Pods recreated, PVCs replaced |
| `restorePosthook` | Left maintenance mode, waited through `DataStage=InProgress`, reported `Completed` |
| Data check | All four md5 checksums identical to the recorded values; 6 Data Set data files back |
| Functional check | `view-dataset` reads the five rows again; `dsjob run` completes with `status = OK` |

An earlier manual run of the same procedure without Kasten (CSI snapshot → destroy → delete and
recreate the PVCs from the snapshot → scale up) produced the same result, which is what made the
blueprint a transcription rather than a guess.

### Why there is no deduplication measurement

Step 6 of [AGENTS.md](../../AGENTS.md) applies to dump-based patterns, where a blueprint rewrites a
logical dump on every run and the question is whether the export deduplicates. This blueprint writes
nothing: Kasten snapshots volumes that DataStage itself maintains, so incrementality is the ordinary
block-level behaviour of a CSI snapshot chain, with no design choice to measure.

---

## Remove everything

```bash
# blueprint, binding, policy
kubectl delete policy datastage-engine-backup -n kasten-io
kubectl delete blueprintbinding datastage-engine-binding -n kasten-io
kubectl delete blueprint datastage-engine-bp -n kasten-io

# restore-point content for this app
kubectl delete restorepointcontent -l k10.kasten.io/appNamespace=cpd

# the PVC label
kubectl label pvc -n cpd datastage-ibm-datastage-ds-storage-pvc \
  ds-px-default-ibm-datastage-px-storage-pvc blueprint-ai/datastage-engine-

# the example project
./bin/cpdctl project delete --project-id "$PID"

# the files the fixture left on the engine volumes — deleting the project does NOT remove them
POD=$(kubectl get pods -n cpd -o name | grep px-runtime | head -1)
kubectl exec -n cpd ${POD#pod/} -- bash -c "
  rm -f /ds-storage/user-lib/libkasten_fixture.so \
        /ds-storage/connectors/odbc/config/odbc.ini.fixture \
        /px-storage/config/jdbc/isjdbc.config.fixture \
        /px-storage/data/customers.txt /px-storage/data/customers.txt.schema
  rm -f /px-storage/pds_files/node*/customers.ds.*
  rm -rf /ds-storage/PXRuntime/Projects/$PID"
```

DataStage itself is removed with `cpd-cli manage delete-cr` then `delete-olm`
(`--components=datastage_ent`); the CP4D platform is out of scope here.

---

## Initial prompt

> The customer wants a blueprint for DataStage, a component deployed inside the CP4D installation we
> already have. I know nothing about DataStage except that it is an ETL, I do not know how to
> install it, and I have no idea how backup and restore of DataStage works. Study the persistence
> model carefully before deciding anything.
