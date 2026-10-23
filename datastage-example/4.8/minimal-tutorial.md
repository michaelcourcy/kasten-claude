# DataStage in one sitting — enough to design a backup for it

> **Copied from the 5.4 folder.** The measurements in this file were taken on DataStage 5.4.7. On
> 4.8.9 the volume layout, the engine paths and the process types are the same; IBM's backup
> ConfigMaps are not (no online-backup specification), and `/ds-storage/user-lib` is not created by
> the install. See [README.md](README.md) for what was measured on 4.8.9.

> ## ⚠️ This is not DataStage documentation — use IBM's
>
> **To learn or use DataStage, go to IBM's official documentation, not this file.**
> IBM Software Hub docs: <https://www.ibm.com/docs/en/software-hub/5.4.x>
>
> This is the crash course written *while developing this blueprint*, by someone learning the
> product in order to protect it. It is deliberately partial: it covers what a backup designer
> needs and skips most of DataStage. Anything here may be wrong, out of date, or true only of the
> version a section says it was measured on (5.4.7 for §4, 5.2.2 for §5).
>
> ### So why keep the file?
>
> **Because designing a backup for DataStage is mostly a question of where each object is
> stored**, and that question is not answered anywhere in one place. §2 lists every DataStage
> object and the store it lives in; §4 builds a small ETL job between two database tables and then
> **measures** where each artifact landed, including the connection; §5 adds the measurements made
> with the README's self-contained test flow. Those sections are the evidence behind the scope
> decisions in [README.md](README.md) — in particular why a Data Set needs two volumes and a
> project export to be whole.
>
> The README's Kasten test uses its own, simpler flow ([§4](#4-hands-on-an-etl-job-from-one-table-to-another)
> explains why there are two), so you do not need this file to run the example.

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
| **Libraries** | `custom_stage_library`, `data_intg_parallel_function`, `data_intg_java_library`, `ds_xml_schema_library`, `ds_routine` | User code the flows call |
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
| **Data Set** (`.ds`) / **File Set** | The parallel engine's own persistent file formats, written by the Data Set / File Set stage | **Three records, no copies.** An *asset* entry in the project store (so the UI can list it); a *descriptor* file on `/ds-storage` — schema, node config and the **absolute paths** of the data files, about 3.4 KB for the examples (§4.6, §5.1) and not one row of data; the *rows* themselves once, on `/px-storage/pds_files/node*`. Snapshotting `/ds-storage` alone gives you a descriptor addressing files that are not in the restore point |
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

Measured contents of store 2 after one compile and one run: §4.6. Two more PVCs exist,
`ds-temp` (20 Gi) and `ds-migration` (5 Gi), used as working space by the assets and migration
services.

> Store 3 is the same boundary the `cp4d-example` blueprint documents: a connection travels as a
> *definition*, the data behind it does not. §4.8 shows it on a real database.

---

## 3. Setting up the CLI

`cpdctl dsjob` is the DataStage CLI. It is a plug-in inside `cpdctl` and must be switched on with an
environment variable — without it, `cpdctl dsjob` simply does not appear.

```bash
cd datastage-example/5.4
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

## 4. Hands-on: an ETL job from one table to another

This repository uses **two** example flows, each for one job:

| Flow | Used by | Why this shape |
|---|---|---|
| `customers-by-country` — [fixture/customers-by-country.json](fixture/customers-by-country.json) | this section | It reads from a real database and writes back to it, so it shows **connections** and where the DataStage backup stops |
| `customers-etl` — [fixture/customers-etl.json](fixture/customers-etl.json) | the README's Kasten test, and the measurements in [§5](#5-measurements-made-with-the-self-contained-test-flow) | Self-contained: a Row Generator source, writing only to the engine volumes the blueprint protects. A test of the blueprint should not depend on a database the blueprint does not protect |

Everything in this section was run on 2026-10-20 against CP4D 5.4.0 Patch 7 / DataStage 5.4.7,
with CloudNativePG 1.30.1 and PostgreSQL 18.6. The flow is imported **from a JSON file through the
CLI**, not drawn on the canvas, so the whole example can be recreated by a script. Drawing a flow
in the canvas is worth doing once to understand the tool, but a UI walkthrough cannot be replayed
by a test.

```
 namespace datastage-demo-db                        namespace cpd — DataStage
 ┌───────────────────────────┐                     ┌──────────────────────────────────────────────┐
 │ PostgreSQL "customers-db" │                     │                                              │
 │                           │  connection         │  customers ─▶ by_country ─▶ Copy_1 ─┬─▶ customers_by_country
 │  customers  (6 rows)  ────┼────────────────────▶│  (read)       (Aggregator)          │   (write, truncate first)
 │  customers_by_country ◀───┼─────────────────────┤                                     └─▶ customers_by_country.ds
 │                           │                     │                                         (Data Set, engine volumes)
 └───────────────────────────┘                     └──────────────────────────────────────────────┘
```

### 4.1 The database — outside DataStage

Any PostgreSQL that the DataStage pods can reach will do. This example uses
[CloudNativePG](https://cloudnative-pg.io/) in its own namespace, because it is one manifest and the
repository already has a blueprint for it.

Make sure the CloudNativePG operator is installed and watches the `datastage-demo-db` namespace.

```bash
kubectl create namespace datastage-demo-db

kubectl apply -f - <<'EOF'
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: customers-db
  namespace: datastage-demo-db
spec:
  instances: 1
  storage:
    size: 1Gi
    storageClass: ocs-storagecluster-ceph-rbd
  bootstrap:
    initdb:
      database: customers
      owner: etl
EOF
kubectl wait --for=condition=Ready clusters.postgresql.cnpg.io/customers-db \
  -n datastage-demo-db --timeout=300s
```

> **Storage class**: `ocs-storagecluster-ceph-rbd` is the OpenShift Data Foundation class of our test
> cluster. Replace it with a storage class that supports CSI snapshots on your cluster (for example,
> `managed-csi` on AKS, `standard-rwo` on GKE, `ebs-sc` on EKS, your custom class on bare metal).
> This tutorial never snapshots the database, but the backup of it that §4.8 points to does.

> **Write the resource name in full.** On a cluster with Kasten, `kubectl wait cluster/customers-db`
> resolves `cluster` to Kasten's own `clusters.dist.kio.kasten.io` and answers *NotFound*.

Create the source table with six rows, and the empty target table:

```bash
kubectl exec -n datastage-demo-db customers-db-1 -c postgres -- \
  psql -U postgres -d customers -v ON_ERROR_STOP=1 -c "
SET ROLE etl;
CREATE TABLE customers (
  id        integer      PRIMARY KEY,
  cust_name varchar(20)  NOT NULL,
  country   char(2)      NOT NULL,
  amount    numeric(8,2) NOT NULL);
INSERT INTO customers VALUES
  (1,'Alice','FR',120.50), (2,'Bruno','FR',80.00), (3,'Chen','CN',200.00),
  (4,'Dana','US',45.25),   (5,'Emeka','NG',310.00), (6,'Farah','US',99.75);
CREATE TABLE customers_by_country (
  country        char(2)       PRIMARY KEY,
  customer_count integer       NOT NULL,
  total_amount   numeric(10,2) NOT NULL);"
```

Check that the DataStage engine can reach the database. CP4D sets NetworkPolicies in its own
namespace, so this is worth a second before you debug a connector:

```bash
POD=$(kubectl get pods -n cpd -o name | grep px-runtime | head -1)
kubectl exec -n cpd ${POD#pod/} -c px-runtime -- timeout 5 \
  bash -c 'echo > /dev/tcp/customers-db-rw.datastage-demo-db.svc/5432 && echo reachable'
```

### 4.2 A project and a connection

```bash
./bin/cpdctl project create --name datastage-pg-demo --type cpd --storage-type assetfiles
PID=$(./bin/cpdctl project list --output json \
  | jq -r '.resources[] | select(.entity.name=="datastage-pg-demo") | .metadata.guid')
```

A **connection** is the project asset that holds the endpoint and the credentials. The password
comes from the Secret CloudNativePG generated, so it never appears on the command line or in git:

```bash
DB_PASS=$(kubectl get secret -n datastage-demo-db customers-db-app -o jsonpath='{.data.password}' | base64 -d)
jq -n --arg p "$DB_PASS" '{host: "customers-db-rw.datastage-demo-db.svc", port: "5432",
       database: "customers", username: "etl", password: $p, ssl: "false"}' > /tmp/conn.json
./bin/cpdctl dsjob create-connection --project-id "$PID" --name customers-db \
    --datasource-type PostgreSQL --property-file /tmp/conn.json
rm /tmp/conn.json

CONN_ID=$(./bin/cpdctl dsjob list-connections --project-id "$PID" --with-id \
  | awk -F'|' '$1 ~ /^customers-db *$/ {gsub(/ /,"",$2); print $2}')
```

`create-connection` **tests the connection** before saving it (unless you pass `--no-test`), so a
success here already proves the hostname, the port and the password.

### 4.3 The flow

| Stage | Type | What it does |
|---|---|---|
| `customers` | PostgreSQL connector | reads the table `public.customers` |
| `by_country` | Aggregator | groups by `country`: `total_amount` = sum of `amount`, `customer_count` = number of rows |
| `Copy_1` | Copy | sends the same rows to two targets |
| `customers_by_country` | PostgreSQL connector | **truncates** the table, then inserts — running the job twice gives the same table |
| `customers_by_country_ds` | Data Set | writes `customers_by_country.ds`, the parallel engine's own format |

The Data Set target is there on purpose. The database rows are outside DataStage, so without it
the run would leave almost nothing on the engine volumes, and §4.6 would have little to measure.

The flow file refers to its connection **only by id** — no hostname, no password. It ships with two
placeholders, which you replace with your own project and connection ids:

```bash
export CPDCTL_ENABLE_DSJOB=true
sed -e "s/__PROJECT_ID__/$PID/" -e "s/__CONNECTION_ID__/$CONN_ID/" \
    fixture/customers-by-country.json > /tmp/customers-by-country.json
./bin/cpdctl dsjob create-flow --project-id "$PID" --name customers-by-country \
    --pipeline-file /tmp/customers-by-country.json
./bin/cpdctl dsjob compile --project-id "$PID" --name customers-by-country
# customers-by-country compiled successfully in 2 seconds.
```

You do not need to read the flow file. It was produced with IBM's watsonx.data integration Python
SDK by [fixture/build-flow.py](fixture/build-flow.py), which also explains how to change it.

**Compile is a real build step**, not validation: it writes `OshScript.osh` plus a `lib/`
directory on `/ds-storage` (§4.6) — and, as §4.7 shows, a copy of the connection. A flow that is
imported but never compiled cannot run.

### 4.4 The job and the run

A *flow* is not runnable — a **job** is. The job binds a flow to parameters and a runtime
environment:

```bash
./bin/cpdctl dsjob create-job --project-id "$PID" --flow customers-by-country --name customers-by-country-job
./bin/cpdctl dsjob run        --project-id "$PID" --job customers-by-country-job --wait 300
```

The run log is the parallel engine's own log, and it is worth reading once (shortened):

```
<customers> CDICO0004I: Connection properties: {proxy=0, database=customers, password=(masked),
            port=5432, host=customers-db-rw.datastage-demo-db.svc, ssl=0, username=(credentials)}.
<customers,0> Output 0 produced 6 records.
<customers,1> Output 0 produced 0 records.
<APT_HashedGroup2Operator in by_country,0> Input 0 consumed 4 records.
<APT_HashedGroup2Operator in by_country,0> Output 0 produced 3 records.
<APT_HashedGroup2Operator in by_country,1> Input 0 consumed 2 records.
<APT_HashedGroup2Operator in by_country,1> Output 0 produced 1 records.
Current Job Status: Completed
```

The `,0` and `,1` suffixes are **partition numbers**: two compute pods, so two players per stage.
The log shows how the rows moved:

- The source read all 6 rows on partition 0; partition 1 read none. A table read is not split
  across partitions unless you configure it.
- The engine then **hash-partitioned the rows by `country`** for the Aggregator, so that all the
  rows of one country reach the same player: partition 0 received 4 rows and produced 3 groups
  (CN, FR, NG), partition 1 received the 2 US rows and produced 1 group.

### 4.5 Verifying the output

```bash
kubectl exec -n datastage-demo-db customers-db-1 -c postgres -- \
  psql -U postgres -d customers -c "SELECT * FROM customers_by_country ORDER BY country;"
```

```
 country | customer_count | total_amount
---------+----------------+--------------
 CN      |              1 |       200.00
 FR      |              2 |       200.50
 NG      |              1 |       310.00
 US      |              2 |       145.00
```

FR is 120.50 + 80.00, US is 45.25 + 99.75. Run the job a second time and the table still has 4
rows, because the target truncates before it inserts. The Data Set holds the same 4 rows:

```bash
./bin/cpdctl dsjob view-dataset --project-id "$PID" --name customers_by_country.ds
```

### 4.6 Where everything landed — measured, not assumed

After exactly one compile and one run of this flow, plus one `view-dataset`:

| What | Where | Store ([§2.3](#23-the-persistence-map--where-the-bytes-are)) |
|---|---|---|
| The **connection** (host, user, password), the flow, the job, the Data Set **asset**, the project settings, the runtime environment | project asset store | 1 — CP4D metadata |
| Compiled flow: `flows/<flow-id>/scripts/OshScript.osh`, `lib/`, `<flow-id>.zip` — **including a copy of the connection** (§4.7) | `/ds-storage/PXRuntime/Projects/<project-id>/` | 2 — `datastage-ibm-datastage-ds-storage-pvc` |
| Compiled job and run history: `jobs/<job-id>/scripts/OshScript.osh`, `runs/<run-id>/job.log`, … — the OSH holds the same connection copy, the log records host and port | same | 2 |
| Data Set **descriptor**: `customers_by_country.ds` (3460 bytes), `customers_by_country.ds.schema` (80 bytes) | same | 2 |
| Data Set **preview**: `sample_data/ds_data_<uuid>.txt` (338 bytes) — the rows `view-dataset` displayed, **left behind on the volume** | same | 2 |
| Data Set **data**: `customers_by_country.ds.<uid>.<ip>.…`, one file per partition — 82 bytes (3 rows) on `node2`, 52 bytes (1 row) on `node3` | `/px-storage/pds_files/node2`, `node3` | 2 — `ds-px-default-…-px-storage-pvc` |
| The rows of `customers` and `customers_by_country` | the PostgreSQL volume in `datastage-demo-db` | **3 — neither DataStage nor the DataStage blueprint** |

One logical object can therefore straddle three stores: the Data Set is an **asset** in the project,
a **descriptor** on `/ds-storage`, and **partitioned data** on `/px-storage`. The connection is
similar: a project asset, plus a compiled copy on `/ds-storage`.

> **Engine storage accumulates orphans.** Deleting a flow or a job through the API removes the
> asset but leaves its directory under `/ds-storage/PXRuntime/Projects/<project-id>/`. After
> deleting and recreating a flow twice, three `flows/<id>/` and three `jobs/<id>/` directories were
> present for one flow and one job (5.2.2). Seen again on 5.4.7: one job deleted and recreated left
> two `jobs/<id>/` directories. So the volume is not a faithful mirror of the project — it is a
> superset that only grows. Deleting the **whole project** is different: that does remove
> `Projects/<project-id>/` (§4.9).

### 4.7 The job runs with a copy of the connection, not with the connection

Compiling copies **every property of the connection** into the OSH script on `/ds-storage`, in an
XML `<Connection>` block: `host`, `port`, `database`, `username`, `ssl`, `proxy`, and the `password`
as a `protectedstring` (encrypted, not plain text). The flow and the job each get their own copy.

Which copy does a run use? Measured by breaking the project connection on purpose:

| Step | Result |
|---|---|
| Change the connection's `host` to a name that does not exist (`dsjob update-connection --property host=…`), then run the job **without recompiling** | **Completed**, all 4 rows rewritten. The run used the compiled copy and never read the project connection |
| Recompile, then run | **Failed**: `Error establishing socket. Unknown host: does-not-exist…`. Compiling copied the new value in |

So the project and the engine volume each hold a version of the connection, and **only compiling
makes them agree**. Three consequences:

1. **After a restore, recompile** before you trust a job. If the project and the engine volumes were
   restored from different points in time, a job can run with credentials that nobody sees in the
   UI.
2. **After moving to a cluster where the database has another hostname**, editing the connection is
   not enough: the job keeps connecting to the old host until you recompile.
3. **Snapshots of `/ds-storage` contain database credentials**, encrypted. Protect the restore
   points as you would protect those credentials.

### 4.8 What the DataStage backup does not protect

Two things in this example belong to **other** backups.

**The database.** The rows of `customers` and `customers_by_country` live on the PostgreSQL volume
in `datastage-demo-db`. Neither the DataStage blueprint nor cp4d-example touches them. Back the
database up with a blueprint made for it: [cnpg-example](../../cnpg-example/) for CloudNativePG,
[edb-example](../../edb-example/) for EDB Postgres for Kubernetes. When you plan it, note which table
really needs protecting: `customers` is source data, while `customers_by_country` is **derived** —
re-running the job rebuilds it.

Two backups also mean **two points in time**. If a job ran between the database backup and the
DataStage backup, the restored tables and the restored jobs do not describe the same moment. The
simple fix after restoring both is to re-run the jobs that write derived tables.

**The connection belongs to the CP4D project backup.** A connection is a project asset, so
[cp4d-example](../../cp4d-example/4.8/) protects it, with the flows and jobs. Measured on this project,
`cpdctl asset export start --assets-all-assets` exported one each of `connection`,
`data_intg_flow`, `job`, `data_intg_data_set`, `data_intg_project_settings` and `environment`, and
the connection was in it **twice**:

| In the export | Connection details |
|---|---|
| `assets/.METADATA/connection.<id>.json` | host, user, and the **password in clear text** — the export was made without `--encryption-key` |
| the flow's `px_executables` attachment (the compiled zip) | the compiled copy of §4.7: host, and the password encrypted |

The DataStage blueprint also captures the compiled copy, as a side effect of snapshotting
`/ds-storage`. That copy is **not** the source of truth: the project is. Restore the project with
cp4d-example, then recompile (§4.7). For the clear-text password, see cp4d-example
[DESIGN.md § 6](../../cp4d-example/DESIGN.md#6-the-data-and-credential-boundary-validated-2026-07-10):
export with an encryption key.

### 4.9 Clean up

```bash
./bin/cpdctl project delete --project-id "$PID"
# If it answers "Cannot delete the project due to failure retrieving active jobs and runtimes",
# run it again: we saw this once, and the second attempt succeeded.
# The delete removes /ds-storage/PXRuntime/Projects/<PID>/, Data Set descriptor included, but NOT the
# Data Set data on /px-storage — measured on 5.4.7. Remove it by hand,
# only if no other project writes a Data Set with this name:
for P in $(kubectl get pods -n cpd -o name | grep -E 'px-(runtime|compute)'); do
  kubectl exec -n cpd ${P#pod/} -- bash -c 'rm -f /px-storage/pds_files/node*/customers_by_country.ds.*'
done

kubectl delete namespace datastage-demo-db
```

---

## 5. Measurements made with the self-contained test flow

The measurements below were made with the **other** example flow, `customers-etl`, the one the
README's Kasten test uses, on CP4D 5.2.2 / DataStage 5.2.2 on 2026-09-09. It needs no database:

```
Row_Generator_1 ──▶ Copy_1 ──┬──▶ customers_txt   (Sequential File → /px-storage/data/customers.txt)
   (5 records)               └──▶ customers_ds    (Data Set        → customers.ds)
```

Its two targets write to the engine volumes only — an ordinary file and a Data Set — which is what
these measurements need. To build it, follow
[README.md § Create the example data](README.md#create-the-example-data).

The measurements that led to design decisions — what each project export contains, whether an
imported project runs, and why a cloned project can damage the original — are in the README:
[Why this blueprint does not use the export commands](README.md#why-this-blueprint-does-not-use-the-export-commands).

### 5.1 What a Data Set descriptor actually contains

`customers.ds` on `/ds-storage` is **3431 bytes and holds no rows at all**. It is closer to a
database catalog entry than to a data file. Measured by byte offset:

| Offset | Section | Content |
|---|---|---|
| 0 | header | `Torrent Orchestrate dataset descriptor`, IBM copyright, and the exact engine build string (`$Version: X86_64 Torrent 2_3 2025/09/18 … liborchx86_64.a`) |
| 60 | `DESCRIPT` | the **schema**, as serialized C++ objects — `APT_SchemaFieldListRep`, `APT_BasicStringDescriptor<APT_String>`, the field names |
| ~800 | APT config | the **node configuration as it was at write time**: `node1`/`node2`/`node3`, their `fastname`s (the actual pod hostnames), and each node's `resource disk` |
| 2395 | `SEGMENT` | `APT_DMSegment` → one `APT_DMFilePartition` per partition, each carrying the node name, the fastname, and the **absolute path** of its data file |
| 3357 | statistics | per partition: **3 records / 84 bytes**, then **2 records / 56 bytes** |

The companion `customers.ds.schema` is the only part that really is just columns, and it is plain
text (76 bytes):

```
record
( cust_name: string[20];
  country: string[2];
  amount: string[6];
)
```

Three things follow, and each one matters for backup:

1. **The descriptor is 31× larger than the data it describes** — 3431 bytes of metadata for 111
   bytes of rows. That ratio inverts on a real Data Set, because the descriptor scales with the
   number of *partitions*, not the number of records.
2. **Those statistics are what `describe-dataset` reports.** 3 + 2 = 5 rows, 84 + 56 = 140 bytes —
   exactly the `DataSet Row Count: 5` / `DataSet Size: 140` it prints. Nothing re-validates them
   against the filesystem, which is why they survive the data being deleted.
3. **The recorded byte counts are logical, not on-disk.** 84 = 3 × 28 and 56 = 2 × 28 (the record
   width is 20+2+6), while the files on disk are 58 and 53 bytes — the Data Set format compresses.
   Visible in the raw bytes as run-length runs of the generator's fill characters.

### 5.2 Why `customers.ds` is browsable in the UI and `customers.txt` is not

Both are written by the same flow to the same volume set, yet only one appears under **Data sets**
in the project. The difference is a single stage property:

```
customers_ds     op=PxDataSet          registerDataAsset=True   dataAssetName=customers.ds
customers_txt    op=PxSequentialFile    registerDataAsset=False  dataAssetName=-
```

`registerDataAsset` is what creates the project asset, and therefore what decides UI visibility.
`customers.txt` exists on `/px-storage/data/` and is fully protected by the backup, but nothing in
CP4D points at it, so it cannot be browsed or previewed. Set the flag to `true` on that stage and it
would register an asset too.

**The preview is a live read, not a stored sample.** Viewing a Data Set needs all three of the
descriptor on `/ds-storage`, the data files on `/px-storage`, and a **running** PXRuntime pod. The
three menu entries are therefore three different levels of proof:

| Menu entry | Reads | Survives losing `/px-storage`? |
|---|---|---|
| **View relationships** | CP4D metadata only | yes |
| **View schema** | the descriptor | **yes — and reports the old row count as if nothing were wrong** |
| **View data** | descriptor + data files + a live engine | **no — the only honest one** |

---

## 6. CLI cheat sheet

The commands that come up constantly when developing or verifying a backup:

```bash
P=datastage-pg-demo                    # or --project-id <PID>

# design objects
cpdctl dsjob list-flows        --project $P --with-id --with-compiled
cpdctl dsjob get-flow          --project $P --name customers-by-country --output json --file-name flow.json
cpdctl dsjob create-flow       --project $P --name customers-by-country --pipeline-file flow.json
cpdctl dsjob compile           --project $P --name customers-by-country
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

## 7. Reading list

- `dsjob` CLI reference for the exact release — pin the version:
  <https://github.com/IBM/DataStage/blob/main/dsjob/dsjob.5.2.2.md>
- IBM: DataStage runtime storage layout (`/ds-storage`, `/px-storage`)
- IBM: High availability and disaster recovery in DataStage — IBM's own guidance is *"back up your
  projects by exporting them"*, which is the logical-export path above.

---

## Appendix A — The flow JSON format, for whoever changes `customers-etl.json`

You need this appendix only to modify [fixture/customers-etl.json](fixture/customers-etl.json), the
README's test flow, which [fixture/make-flow.py](fixture/make-flow.py) writes by hand. The §4 flow
is built differently — through IBM's SDK, by [fixture/build-flow.py](fixture/build-flow.py) —
because its connector stages are not practical to write by hand.

The `json_schema` field names the format — IBM's *pipeline-flow v3* — in the same way `apiVersion`
names the format of a Kubernetes manifest. Nothing validates the file against that URL. The flow is
a graph of the canvas drawing:

```
Row_Generator_1 ──Link_1──▶ Copy_1 ──┬──Link_2──▶ customers_txt
                                     └──Link_3──▶ customers_ds
```

| Part of the file | What it holds |
|---|---|
| `pipelines[0].nodes` | One object per stage (box). `op` is the stage type: `PxRowGenerator`, `PxCopy`, `PxSequentialFile`, `PxDataSet`. Each node has input and output **ports**, each with an `id` |
| `inputs[].links[]` on a node | The arrows. A link is stored by the **receiving** stage, as a reference back to the sender: `node_id_ref` (upstream node) + `port_id_ref` (its output port). The sending stage does not list where its output goes — like a Pod that names a ConfigMap, while the ConfigMap names no Pod |
| `schemas` (top level) | The column lists, each with an `id`. A port says which list flows through it with `schema_ref` |
| `app_data.datastage` | DataStage-specific settings. Everything under `ui_data` is canvas layout only (positions, icons, labels) |

Two traps, both of which compile successfully and fail only when the job runs:

1. **`metadata.item_index` is the column's nesting level, not its position.** DataStage supports
   nested records (`subrec`), and `item_index` says how deep a column is nested inside the previous
   one. Numbering the columns 0, 1, 2 therefore means "`country` inside `cust_name`, `amount` inside
   `country`": the compiler emits `cust_name:subrec ( country:subrec ( amount:string[6] ) )` and the
   job aborts with
   `schema contains a field "cust_name" that is a subrec or tagged; only top-level fields are accepted`.
   In a flat table every column is level `0`.
2. **Each stage stores its own copy of the columns** in `parameters.inputcolProperties`
   (`ColumnName`, `DataType`, `Length`, …), in addition to the `schemas` entry its port references.
   The canvas keeps the two in step; a hand-written file must do it itself, and nothing checks it.
   `make-flow.py` builds both from the single `COLS` list for this reason.

When a run fails and the log does not explain why, read the **OSH script**. Compiling translates
the JSON into OSH (Orchestrate Shell), the program the parallel engine actually runs — the
relationship is the one between a Helm values file and the manifest `helm template` renders. It
shows exactly what DataStage understood from the JSON:

```bash
kubectl exec -n cpd <px-runtime-pod> -- cat /ds-storage/PXRuntime/Projects/<pid>/flows/<flow-id>/scripts/OshScript.osh
```
