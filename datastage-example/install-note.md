# DataStage install note — the environment this blueprint was validated against

DataStage Enterprise 5.2.2 added to an existing CP4D / IBM Software Hub 5.2.2 instance.

> ## ⚠️ This is not installation documentation — use IBM's
>
> **To install DataStage, follow IBM's official documentation, not this file.**
> IBM Software Hub 5.2.x docs → *Installing* → *Services* → *DataStage*:
> <https://www.ibm.com/docs/en/software-hub/5.2.x>
>
> IBM owns the supported procedure, keeps it current, and covers the prerequisites, sizing and
> platform variations that this note does not. The commands below are **a record of one install on
> one cluster on one date** — the versions are pinned to 5.2.2, and the storage classes, namespaces
> and cluster URL are specific to our test environment. Following them instead of IBM's
> documentation will give you a DataStage that IBM does not support.
>
> ### So why keep the file at all?
>
> **Because you cannot judge the scope of the backup without it.** What this blueprint protects,
> and what it deliberately leaves alone, follows directly from what the DataStage install actually
> creates:
>
> - [§5.2](#52-pvcs--four-all-rwx) — which PVCs exist, and which hold data rather than scratch space
> - [§5.3](#53-what-is-on-those-volumes-after-one-compiled-flow-and-one-job-run) — what is written on each volume
> - [§5.4](#54-ibms-own-backup-hooks--read-them-they-are-a-specification) — IBM's own `cpdbr` hooks, which specify what must happen around a snapshot
>
> Read those three sections and the scope decisions in [README.md](README.md) stop looking
> arbitrary. That is this file's only purpose: to record **what the blueprint was validated
> against**, so you can compare it with your own installation and see where yours differs.

The CP4D control plane, Watson Studio (`ws`), Common Core Services (`ccs`) and OpenSearch were
already installed on this cluster; that part is not covered here — follow IBM's documentation for
the control-plane install. **DataStage is added on top; nothing in the existing install is
modified.**

---

## 1. What is being installed

| | |
|---|---|
| Component (cpd-cli name) | `datastage_ent` — *DataStage Enterprise* |
| Alternative | `datastage_ent_plus` — *DataStage Enterprise Plus* (adds data quality: standardization rules, match specifications, Data Quality rules) |
| CASE package | `ibm-datastage-enterprise` 10.2.0 |
| Operator CSV | `ibm-cpd-datastage-operator.v8.2.0` |
| Custom resource | `ds.cpd.ibm.com/v1` kind `DataStage`, name `datastage`, version 5.2.2 |
| Declared dependencies | `opencontent_opensearch` + `ccs` — **both already present**, so this install adds only DataStage |

Source of those facts (authoritative, shipped with the CLI, no guessing):

```bash
grep -i datastage \
  <cpd-cli-dir>/cpd-cli-workspace/olm-utils-workspace/work/components.csv
```

`datastage_ent` was chosen over `datastage_ent_plus`: the extra Enterprise Plus assets are data
quality artifacts, which change nothing about how DataStage persists or exports its assets. If the
customer runs Enterprise Plus, the blueprint applies unchanged — only the asset list in an export
grows.

---

## 2. Prerequisites already satisfied on this cluster

| Prerequisite | State | How checked |
|---|---|---|
| CP4D control plane | `ibmcpd-cr` 5.2.2, `Completed` | `kubectl get ibmcpd -n cpd` |
| Common Core Services | `ccs-cr` 11.2.0, `Completed` | `kubectl get ccs -n cpd` |
| OpenSearch operand | `elasticsearch-master`, `Available` | `kubectl get clusters.opensearch.cloudpackopen.ibm.com -n cpd` |
| IBM entitlement key in the global pull secret | done at CP4D install time | `cpd-cli manage add-icr-cred-to-global-pull-secret` |
| RWX storage class | `nfs-csi` (**supports CSI snapshots** — `csi-nfs-snapclass`) | `kubectl get sc`, `kubectl get volumesnapshotclass` |
| RWO storage class | `managed-csi` (Azure disk, `csi-azuredisk-vsc`) | same |
| Free capacity | 6 workers × 16 vCPU / 64 GiB, ~30 % requested | `kubectl describe nodes` |

DataStage needs **RWX** storage for both of its volumes (see §5), which on this cluster is
`nfs-csi`. That class is snapshottable here, which is what makes any PVC-level protection story
possible at all.

---

## 3. Environment

These are the values used on our cluster. `cpd-env.sh` came from the earlier CP4D control-plane
install (a separate repository, not published here) and was reused so the values could not drift.
**Substitute your own** — the storage classes and the API URL are specific to our test environment:

```bash
export PATH=$PWD/cpd-cli-darwin-EE-14.2.2-2727:$PATH

export VERSION=5.2.2
export PROJECT_CPD_INST_OPERATORS=cpd-operators
export PROJECT_CPD_INST_OPERANDS=cpd
export STG_CLASS_BLOCK=managed-csi          # cluster-specific
export STG_CLASS_FILE=nfs-csi               # cluster-specific; must be RWX (DataStage requires it)
export OC_URL=https://api.<your-cluster>:6443

export DATASTAGE_TYPE=datastage_ent
```

### Two gotchas with `cpd-cli` on macOS

1. **`cpd-cli` runs its `olm-utils` container with a TTY**, so it fails from a non-interactive
   shell (an agent session, CI, `nohup`) with:
   `cannot attach stdin to a TTY-enabled container because stdin is not a terminal`.
   Wrap every `cpd-cli manage` call in `script`, which allocates a pty:

   ```bash
   script -q /dev/null cpd-cli manage <subcommand> ...
   ```

2. **Docker must be running** — the `manage` plugin is an Ansible playbook inside the `olm-utils`
   image.

Log in first; every other `manage` command depends on it:

```bash
script -q /dev/null cpd-cli manage login-to-ocp --server=${OC_URL} --token=$(oc whoami -t)
```

---

## 4. The install — two commands

### 4.1 OLM objects (operator), in the operators project

```bash
script -q /dev/null cpd-cli manage apply-olm \
  --release=${VERSION} \
  --cpd_operator_ns=${PROJECT_CPD_INST_OPERATORS} \
  --components=${DATASTAGE_TYPE}
```

Took **~6 minutes** here. It downloads the CASE packages, creates the catalog source
`ibm-cpd-datastage-operator-catalog` and the subscription `ibm-cpd-datastage-operator`. Wait for
`[SUCCESS] ... apply-olm command ran successfully`, then confirm:

```bash
kubectl get csv -n cpd-operators | grep -i datastage
# ibm-cpd-datastage-operator.v8.2.0   IBM DataStage   8.2.0   Succeeded
```

### 4.2 The custom resource (operand), in the instance project

```bash
script -q /dev/null cpd-cli manage apply-cr \
  --components=${DATASTAGE_TYPE} \
  --release=${VERSION} \
  --cpd_instance_ns=${PROJECT_CPD_INST_OPERANDS} \
  --block_storage_class=${STG_CLASS_BLOCK} \
  --file_storage_class=${STG_CLASS_FILE} \
  --license_acceptance=true
```

> `apply-cr` returning `[SUCCESS]` only means the CR was **created**. Reconciliation continues in
> the background — although for DataStage it turned out to be quick.

Watch it:

```bash
kubectl get datastage -n cpd
# NAME        VERSION   RECONCILED   STATUS      AGE
# datastage   5.2.2     5.2.2        Completed   7m15s

kubectl get datastage -n cpd \
  -o jsonpath='{.items[0].status.progress} {.items[0].status.progressMessage}{"\n"}'
```

Observed wall-clock on this cluster (2026-09-09):

| Step | Duration |
|---|---|
| `apply-olm` | ~6 min (CASE download + subscription + CSV) |
| `apply-cr` → `dsStatus: Completed` | **7 min 15 s** |
| PX runtime pods (`ds-px-default-*`) pulling their images and becoming `1/1` | ~12 min more |

So budget **~25 minutes** from nothing to a DataStage you can actually run a job on. The CR reports
`Completed` **before** the parallel-engine pods are ready — do not take `Completed` as "I can run a
job now"; check the pods.

---

## 5. What the install creates — verified on the cluster

### 5.1 Pods and footprint

11 pods, all in the `cpd` namespace:

| Pod | vCPU request | Memory request |
|---|---|---|
| `datastage-ibm-datastage-assets` | 500m | 4 Gi |
| `datastage-ibm-datastage-caslite` | 500m | 4 Gi |
| `datastage-ibm-datastage-canvas` | 500m | 2 Gi |
| `datastage-ibm-datastage-flows` | 500m | 2 Gi |
| `datastage-ibm-datastage-metrics` | 500m | 2 Gi |
| `datastage-ibm-datastage-migration` | 500m | 2 Gi |
| `datastage-ibm-datastage-runtime` | 500m | 2 Gi |
| `datastage-ibm-datastage-ds-nginx` | 500m | 500 Mi |
| `ds-px-default-ibm-datastage-px-runtime` (conductor) | 500m | 2 Gi |
| `ds-px-default-ibm-datastage-px-compute-0/1` (StatefulSet, 2 replicas) | 1000m each | 2 Gi each |
| **Total requests** | **6.5 vCPU** | **24.5 GiB** |

`ds-px-default` is the **PXRuntime instance** — the parallel engine that actually runs jobs. It is a
separate CR (`kubectl get pxruntime -n cpd`), applied by the installer alongside the `DataStage` CR:
it is a peer, not a child, and carries no `ownerReferences` back to it. Both CRs are reconciled by
the same DataStage operator and both point at the same Zen service instance
(`spec.zenServiceInstanceId`).

One `DataStage` service instance can own **several** `PXRuntime` instances in the same namespace.
Teams add a second one to isolate workloads — a different `scaleConfig`, a different set of database
drivers, or simply so one team's jobs cannot starve another team's. The projects and compiled flows
stay shared, because they live on the single `ds-storage` PVC created by the `DataStage` CR; only
the runtime pods and their `px-storage` PVC are per-instance. That is why the blueprint iterates
over every `PXRuntime` CR it finds instead of assuming the single `ds-px-default`.

#### The three process types — the vocabulary the rest of this file uses

A job run is not one process. The parallel engine starts a tree of three kinds, and knowing which
kind writes data is what makes the volume layout in §5.3 readable:

| | How many | What it does | Writes data? |
|---|---|---|---|
| **Conductor** (CP4D: *head node*) | one per job run, on the `px-runtime` pod | Reads the compiled `OshScript.osh`, builds the **score** (the execution plan: how many processes, on which nodes, with which partitioning), starts one section leader per node, and collects every message into the single `job.log` | **No** |
| **Section leader** | one per processing node | The conductor's local agent on that node: starts the node's players, watches them, relays their messages back up | **No** |
| **Player** | one per stage, per node | Executes one stage's logic on **one partition** of the rows | **Yes — this is the only one** |

```
conductor (px-runtime) --> section leader (compute-0) --> players: read / transform / write
                       \-> section leader (compute-1) --> players: read / transform / write
```

A "**node**" here means a **parallel processing node** declared in the engine's APT configuration
file — *not* a Kubernetes node. On CP4D one node maps to one **pod**, so `scaleConfig: small` gives
3 nodes: the conductor plus 2 compute.

Two pool concepts share the word "pool" and are easy to confuse. A **node pool** decides where
operators run; a **disk pool** decides which disks a Data Set may allocate from. The conductor is
declared in the node pool `conductor` only, so it is not in the default pool `""` and therefore
never receives a player — which is why nothing is ever written to its disk directory.

### 5.2 PVCs — four, all RWX

```bash
kubectl get pvc -n cpd | grep -E "ds-storage|ds-temp|ds-migration|px-storage"
```

| PVC | Size | Class | Mount | Mounted by |
|---|---|---|---|---|
| `datastage-ibm-datastage-ds-storage-pvc` | 100 Gi | `nfs-csi` RWX | `/ds-storage` | assets, caslite, flows, migration, runtime, **and all 3 PX pods** |
| `datastage-ibm-datastage-ds-temp-pvc` | 20 Gi | `nfs-csi` RWX | `/ds-temp` | assets |
| `datastage-ibm-datastage-ds-migration-pvc` | 5 Gi | `nfs-csi` RWX | `/ds-migration` | migration |
| `ds-px-default-ibm-datastage-px-storage-pvc` | 10 Gi | `nfs-csi` RWX | `/px-storage` (+ `/user-home/_global_/dbdrivers`) | the 3 `ds-px-default` pods |

135 GiB requested in total. The px-storage PVC belongs to the **runtime instance**, so a second
PXRuntime instance would bring its own.

### 5.3 What is on those volumes (after one compiled flow and one job run)

```
/ds-storage/
├── PXRuntime/Projects/<project-id>/
│   ├── flows/<flow-id>/{scripts/OshScript.osh, lib/, .compiled, <flow-id>.zip}   compiled flow
│   ├── jobs/<job-id>/{scripts/OshScript.osh, runs/<run-id>/{job.log, perf.out,
│   │                  dynamic_config.apt, generated_config.apt, mon.log}}        compiled job + run history
│   ├── customers.ds, customers.ds.schema                                          Data Set DESCRIPTOR
│   └── datasets/
├── user-lib/           custom libraries a flow can call
├── connectors/odbc/    ODBC driver configuration
├── rule-set/  avi/  snc/  utilityScripts/  service_log_archive/
└── ds-runtime/active-jobs/

/px-storage/
├── pds_files/node1 … node65/   Data Set / File Set DATA — one directory per parallel node
│      └── customers.ds.<uid>.<ip>.0000.0000.…            (the actual partitioned bytes)
│      (all 65 pre-created by the install; with scaleConfig small only node2 and node3
│       fill up. node1 is the conductor's and stays empty at any scale — see below)
├── data/                        files a flow writes with a file-system path
├── config/{dynamic_config.apt.template, wlm/, jdbc/isjdbc.config, odbc/, log_retention/}
├── PXRuntime/{queuedJobs, runningJobs, WLM, pods/}   live engine state
├── Datasets/  db2/  tools/  certs/  dbdrivers/
```

**The split that matters:** a Data Set's *descriptor* is written to `/ds-storage` (and registered as
a project asset), while its *data* is written to `/px-storage/pds_files/node*`. Two volumes, one
logical object.

**Why 65 directories and only two in use.** The install creates all of them at once, so scaling out
never has to create directories at runtime. `node1` belongs to the conductor and stays empty no
matter how far you scale: only *players* write Data Set partitions, and the conductor's node pool
(`pools "conductor"`) excludes it from the default pool, so no player is ever placed there. Its
`resource disk` is declared only because the configuration format expects every node to have one.
For backup this means the number of non-empty directories tracks the number of compute pods, not
the 65 slots.

Plus, optionally created later by an administrator:

| Object | What |
|---|---|
| **user volumes** | Extra PVCs mounted into the DataStage runtime so flows can read and write ordinary files. `cpdctl dsjob list-volumes` / `upload-volume-files`. Any customer flow doing file I/O probably uses one. **They are not created by the install, so nothing protects them unless you label them** — see [Prerequisites §2 in README.md](README.md#2-label-the-pvcs-to-protect). |

### 5.4 IBM's own backup hooks — read them, they are a specification

The install ships the ConfigMaps `cpdbr` uses to protect DataStage. They state, in IBM's own words,
what has to happen around a snapshot. Summarised here because it is part of what the install gives
you; the **rule-by-rule mapping onto the blueprint** is in
[README.md → Where this design comes from](README.md#where-this-design-comes-from--ibms-own-backup-hooks).

```bash
kubectl get cm datastage-maint-aux-qu-cm -n cpd -o yaml    # quiesce / unquiesce
kubectl get cm datastage-maint-aux-br-cm -n cpd -o yaml    # offline backup/restore
kubectl get cm datastage-maint-aux-ckpt-cm -n cpd -o yaml  # online (checkpoint) backup
```

What they say:

- **Quiesce = put the `DataStage` and `PXRuntime` CRs into maintenance mode**
  (`cpdbr.cpd.ibm.com/enable-maint`, waiting on `.status.dsStatus`; timeout 1800 s for `DataStage`,
  **600 s for `PXRuntime`**), and **unquiesce = `disable-maint`** (1800 s for both). Nothing else —
  no database flush, no application-level freeze.
- **Precheck**: both CRs must report `.status.dsStatus == Completed` before a backup starts,
  `on-error: Fail`.
- **Managed resources**: `deployment app.kubernetes.io/name=datastage`,
  `deployment app.kubernetes.io/component=px-runtime`,
  `statefulset app.kubernetes.io/component=px-compute`.
- The component metadata shipped with `cpd-cli`
  (`plugins/config/cpdbr_metadata.yml`) records for `datastage`:
  `offline_support: true`, `online_support: true`, **`volume_snapshots_support: true`**,
  `restore_to_diff_namespace_support: false`.

---

## 6. Post-install: give a user access to the service instance

Even when the service shows *Ready to use*, a user cannot open DataStage until they are a member of
a DataStage service instance and of a project. Web UI → **Services → Instances**, and project →
**Access control**.

---

## 7. Uninstall

Reverse order of §4. Deleting the CR deletes the DataStage pods; the PVCs and their data are what
you must decide about explicitly.

```bash
script -q /dev/null cpd-cli manage delete-cr \
  --components=${DATASTAGE_TYPE} --release=${VERSION} \
  --cpd_instance_ns=${PROJECT_CPD_INST_OPERANDS}

script -q /dev/null cpd-cli manage delete-olm \
  --components=${DATASTAGE_TYPE} --release=${VERSION} \
  --cpd_operator_ns=${PROJECT_CPD_INST_OPERATORS}

# check nothing DataStage-shaped is left holding storage
kubectl get pvc -n cpd | grep -i -E "ds-|px-"
```

The test project and its assets are separate — delete those with
`cpdctl project delete --project-id <PID>`.
