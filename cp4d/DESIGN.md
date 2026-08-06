# Why this design — CP4D artifact-level backup

This document is the **"why"** behind the blueprint: what CP4D artifacts actually are, why the API
is the only path to them, how the design maps onto this project's blueprint patterns, and what was
measured about `cpdctl` export/import. The **"how"** — prerequisites, deploy, restore, teardown — is
in [README.md](README.md).

---

## 1. Where this fits

CP4D ships its own platform-level backup/restore (`cpdbr` / `cpd-cli oadp`). That path is
**whole-tenant** and coupled to a resource-backup engine plus a separate object store: it is built
for infrastructure DR of the entire CP4D installation.

This blueprint targets a different lane: **per-project, artifact-level** protection with
**granular, self-service restore** — recover one analytics project, or migrate a project to another
cluster, without touching the platform. It is a **complement** to platform DR, not a replacement:
it needs the CP4D platform to be up, and it does not protect the control plane. Position it that
way.

---

## 2. What a CP4D artifact is — and why the API is the only path

Three architectural facts drive the entire design.

**A notebook is not a file, and not a CRD.** Analytics projects and their assets (notebooks,
connections, jobs, models) are **application-level entities** living in CP4D's metadata database
plus its object storage, reachable only over REST / `cpdctl`. They are **not Kubernetes custom
resources** — `oc get` will never show them. (The only "projects" visible in the cluster are
OpenShift namespaces, which are unrelated.)

→ A Kubernetes resource backup *cannot see a notebook at all*. The API path is not merely the
cleaner option here; it is the only way to get artifact granularity.

**The unit is the analytics project, not the individual notebook.** A lone notebook stripped of its
project context — its runtime binding, its data assets, its relationship graph — is not usefully
restorable. So the project is the atom of backup and restore, which is why the design gives each
project **its own PVC**: PVC = unit of granularity = unit of multi-tenant restore.

**The tool is `cpdctl`, not `cpd-cli`.** `cpd-cli` is the platform/install CLI (the `cpdbr` world).
`cpdctl` is the runtime CLI for projects and assets — the artifact layer. Its project
export/import is an **async export-job** model (create → poll → download a zip bundle; import
mirrors it), which `--output-file` collapses into a single synchronous call.

---

## 3. How it maps onto this project's blueprint patterns

The governing rule in [CLAUDE.md](../CLAUDE.md) is **"Kasten is the data mover — blueprints only
quiesce or dump-to-PVC, never move data themselves."** A `cpdctl` export produces a bundle on a
volume, so the on-model design is **Pattern 4 — dump to a permanent keeper PVC**, combined with the
**action-hook** mechanism:

- A keeper image carrying `cpdctl` + a platform API key writes the export into a permanent PVC.
- **Kasten snapshots those PVCs.** The blueprint never transfers data, and never uses
  `KanisterBackupData` / `KanisterRestoreData`.
- There is no `Project` or `Notebook` CR to bind a workload blueprint to, so the orchestration runs
  as a **BackupAction `preHook`** (an action blueprint) rather than via BlueprintBinding.

The action hook also solves a discovery-ordering problem. Kasten's PVC discovery runs **before** the
preHook of a BackupAction but **after** a resource-bound `backupPrehook`. Since the set of projects
— and therefore the set of PVCs — is only known at run time, the orchestration must run early
enough that any newly created per-project PVC still lands in the restore point. Only the action-hook
path gives that. The trade-off is the documented one: namespace-only context, one blueprint per
namespace.

### Decisions that follow

- **Permanent, GUID-keyed PVCs** (`cp4d-<sanitized-name>-<short-guid>`) — created if missing,
  refreshed every run, pruned when the project is deleted. Permanence is what lets CSI block-level
  incremental dedup work across successive snapshots.
- **Store the export UNZIPPED.** A zip is opaque to block-level dedup: a one-cell notebook edit
  rewrites the whole archive. Unzipped, a one-cell edit rewrites one file, and the ~70 % constant
  schema mass (see §5) dedups across snapshots.
- **Restore is decoupled from Kasten restore hooks** — a standalone [restore.sh](restore.sh) runs
  the `cpdctl import`. More robust, and it fits the multi-tenant / mass-migration story where
  different users restore different projects into different namespaces.
- **Credentials never fan out.** The CP4D API key lives in a Secret in the `cpd` namespace —
  deliberately *outside* the backed-up namespace — is read cross-namespace by the keeper SA, and is
  rendered into an ephemeral tmpfs `cpdctl` config. It is therefore never captured in any restore
  point.

---

## 4. V1 scope, and the boundaries drawn on purpose

**In scope:** back up analytics projects — notebooks, jobs, file data assets, connection
definitions, the dependency graph, and the environment/runtime *definitions* the notebook needs to
run after restore.

**Deliberately deferred:**

| Out of V1 | Why it matters |
|---|---|
| **Custom runtime images** | Export captures the environment *definition*, not the image in the internal registry → a notebook on a custom runtime may not be runnable after restore. Untested (§5). |
| **External data behind connections** | Captured as a reference only — see §6. Separately protected. |
| **Version skew** | Logical export/import is version-sensitive. V1 targets same-version DR (5.2 → 5.2); cross-version migration is a separate validation — see §7. |
| **Git-based projects** | Validated only against the default COS-backed (`assetfiles`) flavour. For a git-based project the notebook *content* lives in a git repo, so the export captures the project definition, asset metadata, jobs, connections and the project→repo association, but not the notebook bytes. Check which flavour is in play — but see the caveat below: git-based does **not** mean "no backup needed". |

Connection **credentials** were initially assumed to be redacted on export. They are not — see §6.
That finding changed the design, so it is not a deferral but a requirement.

### Git-based does not remove the need to back up the metadata

It is tempting to conclude that a git-based project is already protected because "the notebooks are
in git". Do not stop there — **still back up the CP4D metadata**, for two independent reasons.

**Git is tamper-evident, not tamper-proof.** Commit hashes and signatures let you *detect* that
history was altered; they do not prevent it. A force-push, a rewritten branch, a deleted repo or a
compromised maintainer account all change the remote, and a hash mismatch tells you afterwards that
something is wrong — it does not give you the previous state back. Detection is not recovery. An
independent, immutable copy is what turns "we can prove this was tampered with" into "we can put it
back".

**Git is usually inside the blast radius.** When a real disaster or a ransomware event hits, the
whole CI/CD chain tends to go with it — the git server, its runners, the artifact registry, the
credentials that reach them. A recovery plan whose sole copy of the notebooks lives in that same
chain has a single point of failure precisely in the scenario it exists for. This is the same
reasoning as the external-datasource boundary in §6, applied to git: the git remote is *an external
datasource*, and it needs its own explicit, independent protection decision.

**Practical consequence.** For a git-based project, treat it as **two things to protect, not one**:

- **This blueprint protects the CP4D-side metadata** — the project definition, the repo association,
  jobs, connections, environment definitions, the dependency graph. Without it, a restored git repo
  is a pile of `.ipynb` files with no project to run them in, no runtime binding and no connections.
- **The git remote needs a separate, independent process** — its own backup, on its own schedule,
  ideally into storage the CI/CD chain cannot reach or overwrite.

So git-based projects are not *simpler* to protect. The split is different, and the two halves must
be recoverable independently.

---

## 5. What export/import actually produces (validated 2026-07-09)

Fixture: one project containing a notebook (2 code cells + outputs) and a job that runs it.
CP4D 5.2.x, `cpdctl` 1.8.244. Bundle: 147 KB, 76 files. Three layers:

- **Artifacts — the payload, and it is tiny.** `assets/notebook/*.ipynb` is the **real notebook,
  cells and outputs included**. The `job` asset was captured too: `--assets-all-assets` really does
  mean *all* project assets, not just notebooks.
- **Graph + project definition — small.** `assetrelationships.json` records the job→notebook "uses"
  relationship. `project.json` shows `storage.type = assetfiles`, i.e. a COS-backed (not git-based)
  project.
- **`assettypes/` — 70 files, ~70 % of the bundle.** Asset-type **schemas** (the metamodel), not
  instances. This is why a 995-byte notebook yields a 147 KB bundle: schema overhead is roughly
  **constant regardless of content**, which is precisely why storing the bundle unzipped pays off.

**Round-trip fidelity — verified:**

| Check | Result |
|---|---|
| Notebook content (cells + outputs) | **byte-for-byte identical** (`diff` → IDENTICAL) |
| Assets landed in target project | notebook + job, both `available`, fresh IDs |
| Relationship graph remapped | job `asset_ref` → the **new** notebook ID, not the source ID |
| Stock runtime remapped | job `env_id` → `rt241py-<new-project-id>` |

**One nuance worth remembering:** on import the **notebook** asset loses its explicit
`runtime.environment` field (the source had it, the imported copy does not), but the **job** that
executes the notebook keeps its runtime binding, correctly remapped to the target project's stock
runtime. Execution reproducibility therefore rides on the **job**, not on the notebook's own
environment pointer. Fine for **stock** runtimes; the **custom** runtime case is still untested.

---

## 6. The data and credential boundary (validated 2026-07-10)

Re-exporting a data-rich project (`--assets-all-assets`) and inspecting the bundle:

| Asset | In the bundle? | Evidence |
|---|---|---|
| Notebook `.ipynb` (cells + outputs) | ✅ yes | `assets/notebook/*.ipynb` |
| **File** data asset | ✅ **bytes included** | `assets/<catalog>/<asset-id>/<attachment-id>` holds the raw file |
| **Connected** data asset | ⚠️ **reference only** | attachment has `is_remote:true`, `size:0`, a `connection_path` — **no bytes** |
| Connection definition | ✅ yes | `assets/.METADATA/connection.*.json` |

### Connection credentials are exported in cleartext

This corrected an earlier assumption and is the single most important finding for anyone operating
this blueprint. Credentials in connection assets are **not redacted by default**:

```
# export WITHOUT --encryption-key:
"secret_key": "<the real secret, in clear>"

# export WITH --encryption-key 'MyS3cretKey!':
"secret_key": "GprEWcYZaF1R1GtwNQ7KEA==:twdCDnHwxVM="   # encrypted; access_key stays clear
```

Consequences, in short: the bundle is **sensitive**, so the PVCs holding it and the backup namespace
must be locked down regardless; and for any multi-tenant use the export must pass
`--encryption-key` with the restore passing the same key. Losing the key means losing the
credentials inside the restored connections. The operational guidance — when plaintext-plus-RBAC is
acceptable and when encryption becomes mandatory — is in
[README.md § Security](README.md#security--credentials-in-the-bundle-read-this).

### What `cpdctl` does not protect

The connected-data-asset row above makes the boundary concrete. Export captures the **project**;
it does not capture the **external data** a connection points at — the S3 objects, the rows in a
Db2, the files on an on-prem share. Those live outside CP4D.

This is a **deliberate boundary, not a defect**, and it is the same boundary `cpdbr` has: you
protect what the tool captures, and anything external becomes an explicit, per-connection decision.
See [README.md § The external-datasource boundary](README.md#the-external-datasource-boundary) for
how to make that decision.

---

## 7. Why "latest `cpdctl`" is the right version policy

Install the **newest stable `cpdctl`**, not a version matched to your CP4D release. `cpdctl`
versioning (1.8.x) is **decoupled** from CP4D versioning (4.x / 5.x); the `cpdctl` README states:

> *"Always use most recent IBM cpdctl version available. It is backward compatible with all
> supported Cloud Pak for Data releases."*

Two caveats worth keeping straight:

- **"all *supported* releases"** is scoped to CP4D versions still in IBM support. A long-EOL CP4D
  could fall off.
- **Client↔backend compatibility is not bundle↔bundle portability.** "Latest `cpdctl` handles all
  CP4D versions" means the *CLI can talk to* them. It does **not** promise that a bundle exported
  against 5.2.x imports cleanly into 5.3.x — the bundle format is a property of the CP4D backend
  that produced it, not of the CLI. That is the **version-skew** risk listed in §4.
