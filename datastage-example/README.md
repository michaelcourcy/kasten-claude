# DataStage on Cloud Pak for Data — engine-state protection with Kasten

Protect the **DataStage parallel-engine state** inside an existing Cloud Pak for Data (IBM Software
Hub) installation: custom libraries, ODBC/JDBC driver configuration, compiled job artifacts, run
history, Data Set descriptors and Data Set data. Kasten snapshots the DataStage engine volumes; the
blueprint runs IBM's own health precheck, flushes the filesystems before the snapshot, and takes the
operator out of maintenance mode after a restore.

> **This blueprint deliberately does not protect DataStage flows and jobs.** Those are CP4D
> **project assets**, not engine state, and a project export/import restores them — verified, see
> [What DataStage stores, and where](#what-datastage-stores-and-where). Use
> [cp4d-example](../cp4d-example/) for that half. The two blueprints are complementary and are
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
freeze, because IBM's own **online** backup hooks for DataStage are empty: the ConfigMap
`datastage-maint-aux-ckpt-cm`, shipped by the install, declares `pre-hooks`, `post-hooks` and
`checkpoint` hooks as `exec-rules: []`. The only requirement it states is a precheck that the
`DataStage` and `PXRuntime` custom resources report `dsStatus == Completed`, with `on-error: Fail`.
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

Read them on your own cluster before adapting anything — they are versioned (`version: 5.2.2` here)
and IBM can change them between releases:

```bash
kubectl get cm datastage-maint-aux-qu-cm   -n cpd -o yaml   # quiesce / unquiesce
kubectl get cm datastage-maint-aux-br-cm   -n cpd -o yaml   # OFFLINE backup and restore
kubectl get cm datastage-maint-aux-ckpt-cm -n cpd -o yaml   # ONLINE (checkpoint) backup
```

### The mapping, rule by rule

| IBM rule, and where it is written | What it says | Where it is in this blueprint |
|---|---|---|
| `ckpt-cm` → `precheck-meta.backup-hooks` | `check-condition {$.status.dsStatus} == {"Completed"}` on `DataStage` and on `PXRuntime`, **`on-error: Fail`** | `backupPrehook` step 1. Fails the backup before anything is snapshotted |
| The same rule's `PXRuntime` entry has **no `name:` field** (the `DataStage` entry has `name: datastage`) | It applies to **every** `PXRuntime` CR in the namespace, not a named one | `backupPrehook` loops over `kubectl get pxruntime` rather than assuming `ds-px-default` |
| `ckpt-cm` → `backup-meta.pre-hooks`, `backup-meta.post-hooks`, `checkpoint-meta.exec-hooks` | All three are `exec-rules: []` — **empty** | There is no quiesce phase at backup time. IBM requires nothing beyond the precheck for an online backup |
| `ckpt-cm` → `aux-meta.managed-resources: []` | The online path scales nothing down | The backup does not touch the workloads |
| `br-cm` → `restore-meta.post-hooks` | `cpdbr.cpd.ibm.com/disable-maint` on `DataStage` and `PXRuntime`, `statusFieldName: dsStatus`, `timeout: 1800s` | `restorePosthook`: sets `ignoreForMaintenance: false` on both, then waits for `dsStatus == Completed`. The 1800 s timeout in the blueprint is IBM's number, not a guess |
| `br-cm` → `aux-meta.managed-resources` | `deployment app.kubernetes.io/name=datastage`, `deployment app.kubernetes.io/component=px-runtime`, `statefulset app.kubernetes.io/component=px-compute` | The three workload selectors in [policy.yaml](policy.yaml) are copied from here |
| `br-cm` → `backup-validation-meta` | The backup must contain the ConfigMap `datastage-ibm-datastage-config-cm` | Included in [policy.yaml](policy.yaml) — see the note below |
| `br-cm` / `ckpt-cm` → `plan-meta.groups` | The resources worth capturing are the `ds-config` ConfigMaps and the `DataStage` + `PXRuntime` CRs | Both are in the policy's include filter |
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

This is also why [Restore](#restore) starts with a **manual** `enable-maint` step: IBM's offline
backup hook would have done it, and the online backup this blueprint performs does not.

> **A note on the `ds-config` ConfigMap.** IBM validates that
> `datastage-ibm-datastage-config-cm` is present in the backup, and its restore plan puts it back in
> the `restore-pre-operators` phase — before the operators run. It holds three keys
> (`pxDefaultInstanceID`, `startup-env.sh`, `import_license.sh`), so it costs nothing to capture.
> The policy includes it for the same reason the `DataStage` and `PXRuntime` CRs are included: as
> **reference material**. The documented restore procedure here restores volumes only, and a
> reinstall recreates this ConfigMap — but if you ever need to know what the original install was
> configured with, it is in the restore point rather than lost.

### Where the sync comes from — nowhere

One thing in this blueprint has **no** counterpart in IBM's hooks: the `sync` in `backupPrehook`.
IBM's specification is written for `cpdbr`, which performs its own volume copy, so a page-cache
flush is not its concern. Kasten takes a **CSI snapshot of the block device** without `fsfreeze`, so
data the writer has acknowledged but the kernel has not flushed is simply absent — the file appears
in the restore point with the right name and zero bytes. That requirement comes from Kasten's
mechanics, not IBM's, which is why it is not in the table above. See
[AGENTS.md](../AGENTS.md#always-sync-the-filesystem-before-kasten-snapshots-the-pvc).

---

## Versions

Detected on the cluster where this blueprint was developed and tested.

| Component | Version |
|---|---|
| Kubernetes | 1.31.6 |
| OpenShift | 4.18.6 |
| Kasten | 9.0.5 (Helm `k10-9.0.5`) |
| Cloud Pak for Data / IBM Software Hub | 5.2.2 (`ibmcpd-cr`, ZenService 6.2.2) |
| DataStage | 5.2.2 — component `datastage_ent`, operator `ibm-cpd-datastage-operator.v8.2.0` |
| Common Core Services (prerequisite of DataStage) | 11.2.0 |
| `cpdctl` | 1.10.3 |
| `cpd-cli` | 14.2.2 (EE) |
| Tool image | `ghcr.io/kastenhq/blueprint-ai/kasten-tools:9.0.5` ([Dockerfile](../images/kasten-tools/Dockerfile)) |

Detect your Kasten version:

```bash
helm ls -n kasten-io
# or (OpenShift OLM): kubectl get csv -n kasten-io -o jsonpath='{.items[?(@.spec.displayName=="Kasten K10")].spec.version}'
```

> **Tool image note.** The blueprint references `kasten-tools:9.0.5`, matching the Kasten version
> above; CI publishes a tag for every referenced version on merge. The hooks themselves were
> executed against the already-published `9.0.4` tag, because `9.0.5` did not exist at development
> time. The image only supplies `kubectl`, `jq` and `bash`, so the difference does not affect what
> was validated — but it is a difference, and you should know about it.

---

## Repository layout

| Path | What |
|---|---|
| [install-note.md](install-note.md) | A record of the environment this blueprint was validated against: what the DataStage install creates (pods, PVCs, volume layout) and IBM's `cpdbr` maintenance ConfigMaps. **Not installation documentation — use [IBM's](https://www.ibm.com/docs/en/software-hub/5.2.x) for that** |
| [minimal-tutorial.md](minimal-tutorial.md) | What DataStage is, every object it has and **which store each one lives in**, plus one minimal ETL job with its artifacts measured on disk. The evidence behind this blueprint's scope. **Not DataStage documentation — use [IBM's](https://www.ibm.com/docs/en/software-hub/5.2.x)** |
| [blueprint.yaml](blueprint.yaml) | The blueprint: `backupPrehook` (precheck + flush), `restorePosthook` (leave maintenance) |
| [blueprintbinding.yaml](blueprintbinding.yaml) | Binds it to the single DataStage `runtime` Deployment |
| [policy.yaml](policy.yaml) | Kasten policy on the `cpd` namespace, filtered to DataStage objects |
| [prepare-restore.sh](prepare-restore.sh) | Restore Step 1: parks the operator and blocks until `dsStatus == InMaintenance`, then checks for in-flight jobs, the PVC label and the restore point |
| [fixture/make-flow.py](fixture/make-flow.py) | Generates the example DataStage flow (and documents the flow-JSON format) |
| [fixture/customers-etl.json](fixture/customers-etl.json) | The generated flow |

---

## What DataStage stores, and where

Three stores, measured on the cluster rather than taken from documentation.

| # | Store | What is in it | Protected by |
|---|---|---|---|
| 1 | **CP4D project assets** | flows, jobs, connections, parameter sets, table definitions, project settings, runtime environments, Data Set *assets* | [cp4d-example](../cp4d-example/) — a project export/import |
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

One case is genuinely mixed and was **not** measured: CP4D has project asset types for some library
kinds (`function_library`, `custom_stage_library`, `ds_routine`), so a library added *as an asset*
travels in the export, while one copied into `/ds-storage/user-lib` does not. Check which route your
deployment uses before relying on either.

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
> Project assets are project-scoped; the engine storage they address is shared by every project on
> that runtime instance. On a different cluster the data files are simply absent until a job
> rewrites them, which is the expected outcome of a restore.

---

## Two things about Kasten filters that will cost you a backup

Both were found the hard way on this blueprint, and both fail **silently** — the backup reports
`Complete`.

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
`Complete`, PVC `Bound`, volume empty. The workaround is to include the **workloads** in the filter,
which is what [policy.yaml](policy.yaml) does.

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

At restore time the workloads matter too, for a different and legitimate reason: a `RestoreAction`
filtered to PVCs only **fails**, because Kasten has no workload to scale down and cannot delete a
PVC that eight running pods still mount:

```
Failed to delete PVC ... datastage-ibm-datastage-ds-storage-pvc
Timeout while polling (1m59s)
```

That one fails loudly, which is the right behaviour. Include the workloads in the restore filter —
both [policy.yaml](policy.yaml) and the restore command below already do.

---

## Prerequisites

### 1. DataStage installed in the CP4D instance namespace

This blueprint assumes CP4D is already installed and DataStage added on top
(`cpd-cli manage apply-olm --components=datastage_ent`, then `apply-cr`). That creates 11 pods
(6.5 vCPU / 24.5 GiB of requests) and 4 ReadWriteMany PVCs in the `cpd` namespace.

Install DataStage by following **IBM's official documentation**:
<https://www.ibm.com/docs/en/software-hub/5.2.x> → *Installing* → *Services* → *DataStage*. IBM
owns the supported procedure and keeps it current.

[install-note.md](install-note.md) records what our install produced — the resulting pods and PVCs,
what is on each volume, and IBM's `cpdbr` maintenance ConfigMaps. It is there so you can compare
your installation with the one this blueprint was validated against and see where yours differs,
**not** as an install procedure to follow.

> **Storage class**: DataStage requires an **RWX** class for `/ds-storage` and `/px-storage`. This
> cluster uses `nfs-csi`, which has a matching `VolumeSnapshotClass` (`csi-nfs-snapclass`)
> registered with Kasten. Replace it with an RWX class that supports CSI snapshots on your cluster
> (for example `ocs-storagecluster-cephfs` on ODF, `efs-nfs-client` on AWS, `ontap-nas` on NetApp
> Trident). Do **not** use legacy in-tree classes — they do not support CSI snapshots.

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

# exactly two snapshots should appear
kubectl get volumesnapshot -n cpd
```

Check the hook actually ran — do not rely on the action being green (see the filter warnings above):

```bash
kubectl logs -n kasten-io -l app=kanister-svc --tail=2000 | grep -E "dsStatus|synced|flushed"
```

---

## Blueprint actions

| Action | Trigger | What it does |
|---|---|---|
| `backupPrehook` | Before Kasten snapshots the volumes | Runs IBM's precheck — the `DataStage` CR and **every** `PXRuntime` CR in the namespace must report `.status.dsStatus == Completed`, otherwise the backup fails before anything is captured. Discovers every running pod that mounts a PVC in the backup — the union of the PVCs carrying `blueprint-ai/datastage-engine=true` (so **user volumes are covered**, and the flushed set cannot drift from the snapshotted set) and anything matching `*-ds-storage-pvc` / `*-px-storage-pvc` (a safety net if the label was forgotten, and it picks up extra PXRuntime instances with no configuration). Warns if no PVC carries the label, and names any labelled PVC that no running pod mounts, since that one will be snapshotted unflushed. Warns if any job run is in flight. Finally runs `sync` in each of those pods — **last**, so nothing dirties the page cache afterwards. |
| `restorePosthook` | After Kasten restores the volumes | Sets `spec.ignoreForMaintenance: false` on the `DataStage` CR and every `PXRuntime` CR (idempotent), then waits up to 1800 s for all of them to report `dsStatus == Completed`, so the restore action does not report success while the engine is still assembling itself. Prints the recompile reminder. |

There is no `backupPosthook`: the backup path quiesces nothing, so there is nothing to undo.

---

## Restore

### Before you start

| Prerequisite | Why |
|---|---|
| Same DataStage / CP4D version as the backup | Compiled artifacts are engine-version specific. A Data Set descriptor embeds the engine build string (`$Version: X86_64 Torrent 2_3 2025/09/18 …`) |
| DataStage installed and reconciled | Nothing restores into a DataStage that is not installed. The install recreates the empty volumes this restore then replaces |
| The external sources behind your connections exist | A connection restores as a definition; the data behind it does not |
| No copy of the same project running its jobs | See the Data Set warning above |

### Step 1 — prepare the engine (manual, required)

Kasten replaces the PVCs. The DataStage operator owns them and will fight that unless it is parked
first. `ignoreForMaintenance` pauses reconciliation; it does **not** stop pods — verified on 5.2.2.

Run [prepare-restore.sh](prepare-restore.sh). It parks the operator **and blocks until DataStage is
actually ready**, then tells you what to expect from the restore:

```bash
./prepare-restore.sh <RESTORE_POINT_NAME>      # NS=<namespace> to override the default, cpd
```

```
1/5  parking the operator (ignoreForMaintenance=true) in namespace cpd
2/5  waiting for dsStatus=InMaintenance (IBM's own gate, timeout 1800s)
       datastage/datastage = InMaintenance
       pxruntime/ds-px-default = InMaintenance
3/5  checking for job runs in flight
       none
4/5  checking the PVC label the restore filter selects on
       datastage-ibm-datastage-ds-storage-pvc
       ds-px-default-ibm-datastage-px-storage-pvc
5/5  restore point
       scheduled-4jmdc found

READY — launch the RestoreAction now (Step 2).
```

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

> **Two things during the restore that look like failures and are not.** The script prints these too.
>
> 1. **Several minutes of PVC churn.** Because `ignoreForMaintenance` does not stop pods, Kasten has
>    to scale the workloads down and wait for every mount holder to release before it can delete
>    the RWX PVCs. Measured here: **~7 minutes** from `RestoreAction` start to all 11 engine pods
>    recreated. **You do not need to scale anything down yourself** — Kasten does it, because the
>    restore includes the workloads.
> 2. **A plateau at ~94%.** That is `restorePosthook` clearing maintenance and waiting for
>    `dsStatus` to return to `Completed`. The action withholds success on purpose while the engine
>    reassembles.

### Step 2 — restore the volumes **and the workloads**

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
      - group: apps
        resource: deployments
        matchLabels:
          app.kubernetes.io/component: px-runtime
      - group: apps
        resource: statefulsets
        matchLabels:
          app.kubernetes.io/component: px-compute
EOF
```

The workloads are what let Kasten scale the pods down, replace the volumes and scale them back up.
Without them the action fails on `Failed to delete PVC`.

`restorePosthook` then takes the operator out of maintenance and waits for `dsStatus == Completed`.

> On Kasten 8.x, add `profile: {name: <LOCATION_PROFILE>, namespace: kasten-io}` to the
> `RestoreAction`: extraction of the profile from the `RestorePointContent` is unreliable there and
> the action fails **after** the volumes are restored, with `restorePosthook` never running. Fixed
> in 9.0 — this blueprint was tested on 9.0.5 with no `profile` field.

### Step 3 — recompile and smoke-test

```bash
export CPDCTL_ENABLE_DSJOB=true
./bin/cpdctl dsjob compile --project <project>              # no --name compiles every flow
./bin/cpdctl dsjob run     --project <project> --job <job> --wait 300
```

### Where this fits in a full rebuild

| Scenario | What to do |
|---|---|
| **A — a project or a flow was lost.** Engine is healthy | This blueprint is not involved. Restore the project with [cp4d-example](../cp4d-example/), then recompile if `list-flows --with-compiled` says so |
| **B — engine storage damaged, project assets intact** | Steps 1–3 above |
| **C — rebuild from nothing** | 1. cluster + CP4D control plane + `ccs` (fresh install or `cpdbr`) → 2. **install DataStage at the same version** → 3. **engine state first**, Steps 1–3 above → 4. **project assets second**, via cp4d-example → 5. recompile, re-run jobs to rebuild Data Sets → 6. verify connections against the external sources |

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
TAG=$(gh release view --repo IBM/cpdctl --json tagName -q .tagName)      # e.g. v1.10.3
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

./bin/cpdctl config user set admin --username "$ADMIN_U" --password "$ADMIN_P"
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

End-to-end on 2026-09-09, Kasten 9.0.5 / CP4D 5.2.2 / DataStage 5.2.2.

| Step | Result |
|---|---|
| `backupPrehook` through a `RunAction` | Precheck passed (`DataStage=Completed`, `PXRuntime/ds-px-default=Completed`), 8 engine-volume writer pods discovered and synced, one Kanister ActionSet per backup, 13–23 s |
| Snapshot scope | Exactly the two labelled engine PVCs; nothing else in `cpd` |
| Destruction | Custom library, ODBC file, JDBC file, `customers.txt` and all 6 Data Set data files deleted; `view-dataset` then failed |
| Restore through a `RestoreAction` | `Complete`. Pods recreated, PVCs replaced |
| `restorePosthook` | Left maintenance mode, waited through `DataStage=InProgress`, reported `Completed` |
| Data check | All four md5 checksums identical to the recorded values; 6 Data Set data files back |
| Functional check | `view-dataset` reads the five rows again; `dsjob run` completes with `status = OK` |

An earlier manual run of the same procedure without Kasten (CSI snapshot → destroy → delete and
recreate the PVCs from the snapshot → scale up) produced the same result, which is what made the
blueprint a transcription rather than a guess.

### Why there is no deduplication measurement

Step 6 of [AGENTS.md](../AGENTS.md) applies to dump-based patterns, where a blueprint rewrites a
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
