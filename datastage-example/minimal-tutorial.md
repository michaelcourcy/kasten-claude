# DataStage in one sitting — enough to design a backup for it

> ## ⚠️ This is not DataStage documentation — use IBM's
>
> **To learn or use DataStage, go to IBM's official documentation, not this file.**
> IBM Software Hub 5.2.x docs: <https://www.ibm.com/docs/en/software-hub/5.2.x>
>
> This is the crash course written *while developing this blueprint*, by someone learning the
> product in order to protect it. It is deliberately partial: it covers what a backup designer
> needs and skips most of DataStage. Anything here may be wrong, out of date, or true only of
> version 5.2.2.
>
> ### So why keep the file?
>
> **Because designing a backup for DataStage is mostly a question of where each object is
> stored**, and that question is not answered anywhere in one place. §2 lists every DataStage
> object and the store it lives in; §4 builds one minimal ETL job and then **measures** where each
> artifact landed. Those two sections are the evidence behind the scope decisions in
> [README.md](README.md) — in particular why a Data Set needs two volumes and a project export to
> be whole.
>
> The dataset and the flow built in §4 are reproduced in [README.md](README.md) as the blueprint's
> example data, so you do not need this file to run the example.

---

## 1. What DataStage is

DataStage is an **ETL / ELT tool**: it extracts data from sources, transforms it in a series of
stages, and loads it into targets. You do not write the transformation as code — you **draw a
graph**. The graph is called a **DataStage flow**: boxes are *stages* (or *connectors*, at the
edges), arrows are *links* carrying rows.

Two properties explain most of its design, and both matter for backup:

1. **It is compiled, not interpreted.** A flow is a design document. Before it can run, DataStage
   **compiles** it into a program for its parallel engine (historically called *OSH*, the
   orchestrate shell), plus, when a Transformer stage is used, generated C++ compiled into a shared
   library. So there are two kinds of artifact for the same logic: the **design** (portable) and the
   **compiled output** (machine- and version-specific, and re-creatable by recompiling).
2. **It runs in parallel across processing nodes.** A job run is not one process: a *conductor*
   starts *section leaders* and *player* processes over the configured node pools. That configuration
   (how many nodes, which disks each node uses for temporary and permanent parallel data) lives in a
   configuration file on the engine's own storage, **not** in the project.

DataStage on CP4D is the same product lineage as *InfoSphere Information Server DataStage*, which is
why old vocabulary survives: `DSParams`, `dsjob`, ISX export files, `APT_*` environment variables.

---

## 2. The object model

### 2.1 Design-time objects — these are all **project assets**

A DataStage flow lives inside an ordinary CP4D **analytics project**, next to notebooks and data
assets. That is the single most important fact for backup: *DataStage's design objects are project
assets, reachable through the CP4D project/asset APIs.*

| Object | Asset type | What it is |
|---|---|---|
| **DataStage flow** | `data_intg_flow` | The canvas: stages, links, column metadata, stage properties |
| **Subflow** | `data_intg_subflow` | A reusable fragment included by flows |
| **Connection** | `connection` | Endpoint + credentials for a source or target (shared with the rest of CP4D) |
| **Data asset** | `data_asset` | A file uploaded into the project, or a reference through a connection |
| **Table definition** | `data_definition` | Column/schema metadata, importable from a source |
| **Parameter set** (+ value sets) | `parameter_set` | Named, reusable job parameters (for example one value set per environment) |
| **Environment variables / `PROJDEF`** | parameter set | Project-wide parameter defaults — the `DSParams` of classic DataStage |
| **Custom stages** | `data_intg_build_stage`, `data_intg_custom_stage`, `data_intg_wrapped_stage` | User-written stages |
| **Libraries** | `custom_stage_library`, `function_library`, `data_intg_java_library`, `ds_xml_schema_library`, `ds_routine` | User code the flows call |
| **Message handlers** | `ds_message_handler` | Rules that reclassify engine messages |
| **Data quality assets** (Enterprise Plus) | `standardization_rule`, `ds_match_specification`, `data_quality_rule`, `data_quality_definition` | Standardization / matching / rules |
| **Pipeline** (sequence job) | `orchestration_flow` | Orchestrates jobs — the modern DataStage *sequence* |
| **Test case** | test case asset | Repeatable flow test with expected output |

### 2.2 Runtime objects

| Object | What it is | Notes for backup |
|---|---|---|
| **Job** | A runnable binding of *flow + parameters + runtime environment* | Project asset; created by "compile and run", or explicitly |
| **Job run** | One execution, with its log and row statistics | Operational history, not design |
| **Runtime environment** / **hardware specification** | How much CPU/RAM a run gets and which runtime instance serves it | Project asset referencing a cluster-level runtime |
| **PXRuntime instance** | The pods that actually run jobs (default name `ds-px-default`) — a conductor plus compute pods | Cluster object, created by the install, not by the user. One DataStage service instance can have several |
| **Conductor node** (CP4D calls it the **head node**) | The single coordinating process of a run. It reads the compiled `OshScript.osh`, composes the *score*, starts one section leader per node, and collects every message into the one `job.log`. **It processes no rows.** Here it is the `px-runtime` pod | Its disk directory `/px-storage/pds_files/node1` stays empty **at any scale**: only players write Data Set partitions, and the node pool `conductor` excludes this node from the default pool, so it never receives one. The directory exists because the config format expects every declared node to carry a `resource disk`. Its live bookkeeping (`queuedJobs`, `runningJobs`, `WLM`) does live on `/px-storage` |
| **Compute node** | A node that runs a *section leader* plus the *players*. Here, one per `px-compute-N` pod | This is where Data Set bytes land: `/px-storage/pds_files/node2`, `node3`, … |
| **Section leader** | **One process per processing node**, started by the conductor at job startup. It is the conductor's local agent: it starts the player processes for its own node, watches them, and relays their log and status messages back up to the conductor. **It processes no rows either** — like the conductor, it is a control process | Nothing to back up. But it is why a job log from one run contains interleaved messages from every node: they all arrive through their section leader |
| **Player** | The process that actually executes **one stage's logic on one partition of the data**. A flow with 3 stages running on 2 compute nodes produces roughly 6 players, each seeing only its own slice of the rows. This is the only one of the three process types that touches data | Players are what write Data Set partitions into `/px-storage/pds_files/node*`, one file per player. The conductor and the section leaders write nothing there |
| **Node** (DataStage sense) | A **parallel processing node** declared in the APT configuration file — *not* a Kubernetes node. On CP4D one node maps to one pod (`fastname "$pod"`), so `scaleConfig: small` gives 3 nodes: 1 conductor + 2 compute | `pds_files/node1`…`node65` are all pre-created by the install in one batch (verified: timestamps within one second), giving headroom to scale to 64 compute pods without creating directories at runtime. With `scaleConfig: small` only `node2` and `node3` ever hold data |
| **Score** | The execution plan the conductor builds from the OSH script plus the APT configuration: how many processes run, on which nodes, with which partitioning, and which buffers sit between the stages. The relational-database word *query plan* is the closest analogy | Not persisted as a file. Set `APT_DUMP_SCORE=1` in the runtime environment and the engine prints it into `job.log` |
| **APT** | The prefix the parallel engine uses for everything it inherited from Orchestrate, the Torrent Systems product IBM acquired: the `.apt` configuration-file format (`dynamic_config.apt`), the `APT_*` environment variables (`APT_CONFIG_FILE`, `APT_DUMP_SCORE`, `APT_PM_CONDUCTOR_HOSTNAME`), and the `APT_*` class names written into Data Set descriptors (`APT_DMFilePartition`, `APT_String`). It is usually expanded as *Advanced Parallel Technology*, but IBM's own documentation rarely bothers — treat it as a namespace marker, not a meaningful acronym | You will see `APT_` in job logs, in `.apt` config files, and inside the binary Data Set descriptor |
| **Data Set** (`.ds`) / **File Set** | The parallel engine's own persistent file formats, written by the Data Set / File Set stage | **Three records, no copies.** An *asset* entry in the project store (so the UI can list it); a *descriptor* file on `/ds-storage` — schema, node config and the **absolute paths** of the data files, 3431 bytes for the fixture and not one row of data; the *rows* themselves once, on `/px-storage/pds_files/node*`. Snapshotting `/ds-storage` alone gives you a descriptor addressing files that are not in the restore point |
| **User volume** | An extra PVC mounted into the runtime so flows can read and write ordinary files | Cluster object. Any flow doing plain file I/O probably writes here |

**The process tree of one run.** Three levels, and the split explains where the bytes end up:

```
   tier 1: one per RUN          tier 2: one per NODE        tier 3: one per STAGE, per NODE
   ------------------------     --------------------        -------------------------------
   conductor (px-runtime) ----> section leader (compute-0) --> player: read CSV
                              |                             --> player: transform
                              |                             --> player: write
                              +- section leader (compute-1) --> player: read CSV
                                                            --> player: transform
                                                            --> player: write

   composes the score,          starts and watches this      executes the stage logic
   aggregates the log,          node's players, relays        on ONE partition of the rows.
   moves no rows                their messages up.            The only tier that touches data.
                                Moves no rows.
```

Each player sees only its own partition of the rows, which is what makes the engine parallel. The
`pools` field in the APT configuration is the mechanism that keeps players off the conductor:
`pools "conductor"` means the node belongs *only* to a pool of that name, so a stage with no pool
constraint is never placed there, while `pools ""` is the default pool where players land. Without
it the coordinator would compete for CPU with the work it is coordinating.

### 2.3 The persistence map — where the bytes are

This is the table the blueprint design depends on. Three distinct stores, with three different
protection stories:

| # | Store | Contains | Reached through |
|---|---|---|---|
| 1 | **CP4D project / asset store** (metadata repository + project storage) | Every design-time object in §2.1, job definitions, Data Set *assets* | CP4D APIs — `cpdctl`, `cpdctl dsjob` |
| 2 | **DataStage engine storage** — `/ds-storage` (global, RWX, 100 Gi) and `/px-storage` (per runtime instance, RWX, 10 Gi) | `/ds-storage`: per-project runtime area — compiled artifacts, job logs, run history, Data Set descriptors, custom libraries, ODBC config. `/px-storage`: parallel-engine configuration, the **resource disks** holding Data Set / File Set bytes (`pds_files/node*`), files written to file-system paths, live engine state | PVCs in the `cpd` namespace |
| 3 | **External data sources** | The actual tables and files the flows read and write, through connections | Owned by whoever owns that database; outside CP4D |

Measured contents of store 2 after one compile and one run: §4.5. Two more PVCs exist,
`ds-temp` (20 Gi) and `ds-migration` (5 Gi), used as working space by the assets and migration
services.

> Store 3 is the same boundary the `cp4d-example` blueprint documents: a connection travels as a
> *definition*, the data behind it does not.

---

## 3. Setting up the CLI

`cpdctl dsjob` is the DataStage CLI. It is a plug-in inside `cpdctl` and must be switched on with an
environment variable — without it, `cpdctl dsjob` simply does not appear.

```bash
cd datastage-example
export CPDCONFIG=$PWD/bin/.cpdctl.config.json   # keep credentials out of $HOME and out of git
export CPDCTL_ENABLE_DSJOB=true

CPD_HOST=$(oc get route cpd -n cpd -o jsonpath='{.spec.host}')
ADMIN_U=$(oc get secret ibm-iam-bindinfo-platform-auth-idp-credentials -n cpd -o jsonpath='{.data.admin_username}' | base64 -d)
ADMIN_P=$(oc get secret ibm-iam-bindinfo-platform-auth-idp-credentials -n cpd -o jsonpath='{.data.admin_password}' | base64 -d)

./bin/cpdctl config user set admin --username "$ADMIN_U" --password "$ADMIN_P"
./bin/cpdctl config profile set cp4d --url "https://${CPD_HOST}" --user admin
./bin/cpdctl config profile use cp4d
./bin/cpdctl dsjob version
```

> Passwords with `$` or `"` must be escaped for `dsjob`, per IBM's documentation.

---

## 4. Hands-on: a minimal ETL job, without touching the UI

Everything below was run against CP4D 5.2.2 / DataStage 5.2.2 on 2026-09-09 and is reproducible.
The flow is deliberately built **from a JSON file through the CLI**, not drawn on the canvas, so the
whole fixture can be recreated by a script. Drawing it in the canvas is worth doing once to
understand the tool — but a UI walkthrough cannot be replayed by a test.

### 4.1 A project

```bash
./bin/cpdctl project create --name datastage-demo --type cpd --storage-type assetfiles
# -> Location /v2/projects/<PID>
PID=<PID>
```

That is the whole setup. The flow below generates its own rows, so the fixture needs no input file
and stays far under the 5 KB the development loop wants.

> **Why no input file?** A DataStage flow cannot read a project `data_asset` directly. Measured:
> the bytes of an uploaded file live in project storage at
> `/<storage-id>/<data-asset-id>/<attachment-id>` and are reachable only through an **`assetfiles`
> connection** — and addressing the asset by id through that connection is refused
> (*"The specified data asset does not use the specified connection"*), because an asset whose bytes
> were uploaded is not a *connected* data asset. A `PxSequentialFile` stage with
> `file_location: "connection"` compiles but fails at run time, because the engine opens the path on
> the local filesystem: `Unable to open /1d495273-…: No such file or directory`. Reading through a
> connection requires a **connector node**, a shape the canvas emits and the stage palette does not
> contain. So the fixture uses a Row Generator, and the project/engine split is demonstrated by the
> Data Set instead — see §4.5.

### 4.2 The flow

`fixture/customers-etl.json` is a four-node flow:

```
Row_Generator_1 ──▶ Copy_1 ──┬──▶ customers_txt   (Sequential File → /px-storage/data/customers.txt)
   (5 records)               └──▶ customers_ds    (Data Set        → customers.ds)
```

Why these four stages, and not a prettier ETL: they exercise **both** persistence paths in one run.
The Sequential File target writes an ordinary file onto engine storage; the Data Set target writes
the parallel engine's own format, whose descriptor and data end up in **different** places (§4.5).
A Row Generator source keeps the flow free of any external dependency.

> **The flow JSON format, and the two traps in it.** A flow is the *pipeline-flow-v3* schema
> (`json_schema: …/pipeline-flow/pipeline-flow-v3-schema.json`) with DataStage specifics under
> `app_data.datastage`: one entry per stage in `pipelines[0].nodes` (`op: PxRowGenerator`, `PxCopy`,
> `PxSequentialFile`, `PxDataSet`), links declared on the **input** ports
> (`inputs[].links[].node_id_ref` + `port_id_ref`, pointing back at the upstream node and port), and
> column metadata in the top-level `schemas` array, referenced by `schema_ref`. The file here is
> generated by [fixture/make-flow.py](fixture/make-flow.py) so it can be read and changed.
>
> 1. **`metadata.item_index` is the column's nesting level, not its position.** Numbering columns
>    0, 1, 2 looks obviously right and is wrong: the compiler then emits
>    `cust_name:subrec ( country:subrec ( amount:string[6] ) )` and the *compile still succeeds*.
>    The job aborts at run time with
>    `schema contains a field "cust_name" that is a subrec or tagged; only top-level fields are accepted`.
>    Flat columns are all level `0`.
> 2. **Each stage carries its own copy of the columns** in `parameters.inputcolProperties`
>    (`ColumnName`, `DataType`, `Length`, …). It must agree with the `schemas` entry the port
>    references, and nothing checks that for you.
>
> Read the generated OSH when a run fails in a way the log does not explain — it is the actual
> program, and it is on the volume:
> `kubectl exec -n cpd <px-runtime-pod> -- cat /ds-storage/PXRuntime/Projects/<pid>/flows/<flow-id>/scripts/OshScript.osh`

```bash
export CPDCTL_ENABLE_DSJOB=true
./bin/cpdctl dsjob create-flow --project-id "$PID" --name customers-etl \
    --pipeline-file fixture/customers-etl.json
./bin/cpdctl dsjob compile --project-id "$PID" --name customers-etl
# customers-etl compiled successfully in 13 seconds.
```

**Compile is a real build step**, not validation: it produces `OshScript.osh` plus a `lib/`
directory on `/ds-storage` (§4.5). A flow that is imported but never compiled cannot run.

### 4.3 The job and the run

A *flow* is not runnable — a **job** is. The job binds a flow to parameters and a runtime
environment:

```bash
./bin/cpdctl dsjob create-job --project-id "$PID" --flow customers-etl --name customers-etl-job
./bin/cpdctl dsjob run        --project-id "$PID" --job customers-etl-job --wait 300
```

The run log is the parallel engine's own log, which is worth reading once — it names each operator
and each partition:

```
<Row_Generator_1,0> Output 0 produced 5 records.
<Copy_1,1> Input 0 consumed 2 records.          <- partition 1 got 2 rows
<Copy_1,0> Input 0 consumed 3 records.          <- partition 0 got 3 rows
<APT_RealFileExportOperator in customers_txt,0> Export complete; 5 records exported successfully, 0 rejected.
Step execution finished with status = OK.
Current Job Status: Completed
```

The `,0` and `,1` suffixes are **partition numbers**: two compute pods, so the five rows were split
across two players. Fine to know before reading any DataStage log in anger.

### 4.4 Verifying the output

```bash
POD=$(kubectl get pods -n cpd -o name | grep px-runtime | head -1)
kubectl exec -n cpd ${POD#pod/} -- cat /px-storage/data/customers.txt
kubectl exec -n cpd ${POD#pod/} -- find /px-storage/pds_files -name 'customers.ds*'
./bin/cpdctl dsjob list-datasets --project-id "$PID" --with-id
./bin/cpdctl dsjob view-dataset  --project-id "$PID" --name customers.ds
```

```
"aaaaaaaaaaaaaaaaaaaa","aa","aaaaaa"
"cccccccccccccccccccc","cc","cccccc"
"eeeeeeeeeeeeeeeeeeee","ee","eeeeee"
"bbbbbbbbbbbbbbbbbbbb","bb","bbbbbb"
"dddddddddddddddddddd","dd","dddddd"
```

> The Row Generator fills each `CHAR` column with a default pattern, so the content is not pretty,
> and the row order reflects the two partitions being written back in whatever order they finished.
> It proves rows flowed end to end, which is all the fixture needs.

### 4.5 Where everything landed — measured, not assumed

After exactly one compile and one run of this one flow:

| What | Where | Volume |
|---|---|---|
| The flow, the job, the project settings, the runtime environment, the Data Set **asset** | project asset store | CP4D metadata (not a DataStage PVC) |
| Compiled flow: `flows/<flow-id>/scripts/OshScript.osh`, `lib/`, `<flow-id>.zip` | `/ds-storage/PXRuntime/Projects/<project-id>/` | `datastage-ibm-datastage-ds-storage-pvc` |
| Compiled job + run history: `jobs/<job-id>/runs/<run-id>/{job.log, perf.out, dynamic_config.apt, mon.log}` | same | same |
| Data Set **descriptor**: `customers.ds`, `customers.ds.schema` | same | same |
| Data Set **data**: `customers.ds.<uid>.<ip>.0000.0000.…` | `/px-storage/pds_files/node2`, `node3` | `ds-px-default-…-px-storage-pvc` |
| Sequential File output: `customers.txt` (+ `.schema`) | `/px-storage/data/` | same |
| Live engine state: `queuedJobs/`, `runningJobs/`, `WLM/` | `/px-storage/PXRuntime/` | same |

One logical object can therefore straddle three stores: the Data Set is an **asset** in the project,
a **descriptor** on `/ds-storage`, and **partitioned data** on `/px-storage`.

> **Engine storage accumulates orphans.** Deleting a flow or a job through the API removes the
> asset but leaves its directory under `/ds-storage/PXRuntime/Projects/<project-id>/`. After
> deleting and recreating the fixture flow twice, three `flows/<id>/` and three `jobs/<id>/`
> directories were present for one flow and one job. So the volume is not a faithful mirror of the
> project — it is a superset that only grows.

### 4.6 What an export actually contains — the two different exports

Two different CLIs export the same project, and they do **not** produce the same thing. Measured on
this fixture:

**A. `cpdctl dsjob export-project` — DataStage components only** (5.1 KB):

```
DataStage-README.json          manifest: project name/id, service versions, asset list
ENCRYPTED                      marker (value "INTERNAL" when no --enc-key was given)
data_intg_flow/customers-etl.json
job/customers-etl-job.json
px_executables/customers-etl/customers-etl.zip     because of --include-binaries
```

Run with `--include-data-assets --include-binaries`, it still did **not** contain the
`customers.ds` Data Set asset, the project settings or the environment. **A `dsjob export-project`
is not a project backup**: it follows the DataStage dependency graph outward from the flows, so
anything the flows do not reference is left out — including data assets, which is why
`--include-data-assets` can appear to do nothing.

**B. `cpdctl asset export start --assets-all-assets` — the whole project** (160 KB zip, 94 files):

```
assets/.METADATA/{data_intg_flow, data_intg_data_set,
                  data_intg_project_settings, environment, job}.<id>.json
assets/<storage-id>/<asset-id>/<attachment-id>            any uploaded file's BYTES
assets/data_intg_flow/…px_executables                     the compiled binaries
assets/data_intg_data_set/ds-storage/PXRuntime/Projects/<pid>/customers.ds/
        customers.ds, customers.ds.schema                 the Data Set DESCRIPTOR
assettypes/*.json                                          type definitions (the bulk of the file count)
assetrelationships.json
```

Neither export contains the Data Set's **data** from `/px-storage/pds_files`, nor the
`customers.txt` the flow wrote to a file-system path. Both exports are *design + metadata*; engine
storage is not in scope for either.

### 4.7 A full round-trip: does an imported project actually run?

The question that decides the whole blueprint. Export B was imported into a brand-new empty project
with `cpdctl asset import start --import-dir …`, changing nothing else:

| Check | Result |
|---|---|
| Assets restored | flow, job, `customers.ds` Data Set asset, project settings, runtime environment |
| `list-flows --with-compiled` | `Compiled: true`, `Need Compilation: false` — the `px_executables` round-tripped |
| `dsjob run` on the restored job, **without recompiling** | `Step execution finished with status = OK`, 5 records, new output written |
| `view-dataset` on the restored Data Set | returns the five rows |

So a whole-project export/import restores a **working** DataStage project, compiled binaries
included. That is the single most important measurement in this file.

> **And one consequence, observed in the same test.** A Data Set is restored as a **pointer, not a
> copy**. That is the right behaviour for a recovery — the original is gone, or you are on another
> cluster — but it makes **cloning** a project onto the same runtime instance dangerous. Proof,
> from a second import into a brand-new project:
>
> | Check | Result |
> |---|---|
> | `md5sum` of the descriptor in the source and in the imported project | **identical** (`5588c93…`) |
> | `view-dataset` in the imported project, **before running any job** | returns the five rows — it is reading the source project's data files |
> | Data files on `/px-storage/pds_files` | unchanged, one set, not duplicated |
>
> So the imported project addresses the **original project's data files**. When the restored job
> ran, the engine did what a Data Set write always does — it deleted the existing data files first
> (`<delete data files in delete customers.ds> Output 0 produced 1 records`) — and so **destroyed
> the source project's Data Set data**. Afterwards, `view-dataset` on the *original* project failed
> with `Sample data could not be read from dataset …`, while the restored copy read back fine.
>
> Restoring a DataStage project **next to the original, on the same cluster, then running its jobs**
> can therefore damage the original. The design assets are project-scoped, but the engine storage
> they point into is **shared by every project on that runtime instance**. Restoring into a
> different cluster does not have this problem — there the Data Set data is simply absent until a
> job rewrites it.

---

## 5. CLI cheat sheet

The commands that come up constantly when developing or verifying a backup:

```bash
P=datastage-demo                       # or --project-id <PID>

# design objects
cpdctl dsjob list-flows        --project $P --with-id --with-compiled
cpdctl dsjob get-flow          --project $P --name customers-etl --output json --file-name flow.json
cpdctl dsjob create-flow       --project $P --name customers-etl --pipeline-file flow.json
cpdctl dsjob compile           --project $P --name customers-etl
cpdctl dsjob list-connections  --project $P --with-id
cpdctl dsjob list-paramsets    --project $P
cpdctl dsjob list-datasets     --project $P --with-id
cpdctl dsjob list-dependencies --project $P --deep

# jobs and runs
cpdctl dsjob list-jobs         --project $P --with-id
cpdctl dsjob run               --project $P --name <job> --wait -1
cpdctl dsjob jobrunstat        --project $P --name <job>
cpdctl dsjob logdetail         --project $P --name <job>

# whole-project export / import — the backup-relevant pair
cpdctl dsjob export-project --project $P --file-name proj.zip --include-data-assets --include-binaries --wait -1
cpdctl dsjob import-zip     --project $P --file-name proj.zip --conflict-resolution replace --wait -1

# cluster-level objects
cpdctl dsjob list-volumes
cpdctl dsjob list-volume-files --name <vol> --path /
```

### What `export-project` captures, and the four flags that decide it

| Flag | Effect |
|---|---|
| *(none)* | All DataStage components: flows, subflows, jobs, connections, parameter sets, table definitions, libraries, message handlers |
| `--include-data-assets` | Also the project's **data assets** (uploaded files travel as bytes) |
| `--include-binaries` | Also the **compiled** artifacts, so an import does not need to recompile |
| `--exclude-datasets-filesets` | Dataset/fileset descriptors travel, **but not their bytes**. Without this flag the bytes are included |
| `--enc-key <key>` | Encrypts sensitive values (connection credentials) in the zip. **Without it, credentials are written in the clear** — same behaviour as `cpdctl project export`, see `cp4d-example/DESIGN.md § 6` |

---

## 6. Reading list

- `dsjob` CLI reference for the exact release — pin the version:
  <https://github.com/IBM/DataStage/blob/main/dsjob/dsjob.5.2.2.md>
- IBM: DataStage runtime storage layout (`/ds-storage`, `/px-storage`)
- IBM: High availability and disaster recovery in DataStage — IBM's own guidance is *"back up your
  projects by exporting them"*, which is the logical-export path above.
