# DB2u / IBM Maximo — Kasten Blueprint

Kasten blueprint for backing up and restoring IBM DB2 running under the DB2u operator inside an
IBM Maximo Application Suite (MAS) environment.

---

## Versions

| Component | Version |
|---|---|
| Kubernetes | `1.31.10` (OpenShift / OCS) |
| Kasten | `8.5.5` |
| DB2u operator | `db2u.databases.ibm.com/v1` |
| DB2 | `11.5.9.0-cn6` (image tag `s11.5.9.0-cn6`) |
| IBM Maximo Application Suite | MAS 9.x |

---

## Architecture overview

DB2 is deployed by the DB2u operator as a `Db2uCluster` CR. Each cluster creates:

| PVC | Mount | Access mode | Storage class | Kasten snapshotted |
|---|---|---|---|---|
| `c-<name>-meta` | `/mnt/blumeta0` | RWX | `ocs-storagecluster-cephfs` | Yes |
| `data-<name>-db2u-0` | `/mnt/bludata0` | RWO | `ocs-storagecluster-ceph-rbd` | Yes |
| `c-<name>-backup` | `/mnt/backup` | RWX | `ocs-storagecluster-cephfs` | Yes |
| `activelogs-<name>-db2u-0` | `/mnt/blulogs0` | RWO | `ocs-storagecluster-ceph-rbd` | Yes |
| `tempts-<name>-db2u-0` | `/mnt/blutempts0` | RWO | `ocs-storagecluster-ceph-rbd` | Yes |

The blueprint uses **Pattern 3A — database snapshot on a permanent backup PVC**. The `backupPrehook`
runs `db2 backup db BLUDB online … include logs` inside the DB2u engine pod, writing the backup
image to the backup PVC at `/mnt/backup/backup/`. Kasten then snapshots all PVCs (including the
backup PVC). The `restorePosthook` restores BLUDB from that backup image after PVC restore.

**Blueprint binding approach:** The blueprint is bound to the `Db2uCluster` CR (the top-level
resource in the DB2u operator ownership chain: `Db2uCluster → Formation → StatefulSet`). The
recommended approach is to annotate the `Db2uCluster` CR directly. A `BlueprintBinding` targeting
`db2uclusters` is also provided as a fleet automation mechanism.

> **Known Kasten issue:** Due to a `BlueprintBinding` cache invalidation bug in Kasten ≤ 8.5.5,
> the binding may not trigger reliably after initial deployment. Annotate the `Db2uCluster` CR
> with `kanister.kasten.io/blueprint=db2u-maximo-blueprint` as the primary binding mechanism.

---

## Backup strategy — encryption keystore

DB2 encrypts the database at rest and the backup image using a PKCS12 keystore stored at
`KEYSTORE_LOCATION` (typically `/mnt/blumeta0/db2/keystore/keystore.p12`). The backup image
cannot be restored without the matching keystore — `db2 restore` will fail with a decryption error
if the keystore has changed since the backup was taken.

**What the blueprint does:**

* `backupPrehook` copies `keystore.p12` and `keystore.sth` from `KEYSTORE_LOCATION` to
  `/mnt/backup/backup/` alongside the backup image *before* Kasten takes the snapshot. The backup
  PVC therefore always holds a matched pair (image + key).
* `restorePosthook` copies those files back to `KEYSTORE_LOCATION` *before* running `db2 restore`,
  guaranteeing the decryption key matches the backup image even if the meta PVC was improperly
  restored.

**SSL keystore (not backed up):** The DB2 SSL keystore (`bludb_ssl.kdb`) is stored on the meta PVC
and is intentionally *not* copied to the backup PVC. See the cross-instance restore section for
why this matters.

---

## One-time prerequisite — enabling archive logging

DB2 defaults to circular logging (`LOGARCHMETH1=OFF`). Circular logging blocks online backups.
Archive logging must be enabled via the `Db2uCluster` CR so the operator does not overwrite it
on the next reconciliation:

```bash
kubectl patch db2ucluster mas-instance1-workspace1-manage -n db2u \
  --type=merge \
  --patch '{
    "spec": {
      "environment": {
        "database": {
          "dbConfig": {
            "LOGARCHMETH1": "DISK:/mnt/backup/archivelog/"
          }
        }
      }
    }
  }'
```

> **Important:** Do NOT use `db2 update db cfg` directly — the operator will overwrite it on the
> next reconciliation. Always change `dbConfig` values through the `Db2uCluster` CR.

After the patch, verify the operator has applied it:
```bash
kubectl exec -n db2u c-mas-instance1-workspace1-manage-db2u-0 -c db2u -- \
  su - db2inst1 -c "db2 get db cfg for BLUDB | grep -i LOGARCHMETH"
```

Expected output:
```
First log archive method                 (LOGARCHMETH1) = DISK:/mnt/backup/archivelog/
```

**Mandatory initial offline backup:** After enabling archive logging for the first time, DB2
enters `BACKUP PENDING` state and refuses online operations until an offline backup is taken:

```bash
kubectl exec -n db2u c-mas-instance1-workspace1-manage-db2u-0 -c db2u -- \
  su - db2inst1 -c "
    db2 force applications all
    db2 deactivate db BLUDB
    mkdir -p /mnt/backup/backup /mnt/backup/archivelog
    db2 backup db BLUDB to /mnt/backup/backup compress
    db2 activate db BLUDB
  "
```

This one-time offline backup clears the `BACKUP PENDING` state. All subsequent backups run by
Kasten will be online backups with `INCLUDE LOGS`.

---

## Test data

The DB2 database (BLUDB) and all Maximo application data are pre-provisioned by MAS. No
installation is required. To validate backup/restore, alter a description field in the database:

```bash
kubectl exec -n db2u c-mas-instance1-workspace1-manage-db2u-0 -c db2u -- \
  su - db2inst1 -c "
    db2 connect to BLUDB
    db2 \"UPDATE MAXIMO.MAXVARS SET VARVALUE='KASTEN_TEST_MARKER' WHERE VARNAME='MAXUPG'\"
    db2 disconnect all
  "
```

Verify the change:
```bash
kubectl exec -n db2u c-mas-instance1-workspace1-manage-db2u-0 -c db2u -- \
  su - db2inst1 -c "
    db2 connect to BLUDB
    db2 \"SELECT VARVALUE FROM MAXIMO.MAXVARS WHERE VARNAME='MAXUPG'\"
    db2 disconnect all
  "
```

After restore, re-run the select and confirm `KASTEN_TEST_MARKER` is gone (original value restored).

---

## Blueprint actions

| Action | Trigger | Description |
|---|---|---|
| `backupPrehook` | Before Kasten PVC snapshots | Removes previous backup image; runs online `db2 backup … include logs` to `/mnt/backup/backup/`; copies `keystore.p12` + `keystore.sth` alongside backup image; calls `sync` |
| `backupPosthook` | After Kasten PVC snapshots | Verifies backup image and `keystore.p12` are present on the backup PVC |

---

## ⚠️ Known issue — `restorePrehook and restorePosthook in this specific situation can't be leveraged` 

`restorePrehook` is not yet triggered by Kasten ≤ 8.5.x (fix pending). The blueprint implements
it for forward compatibility but it does **not** execute automatically.

`restorePosthook` can only be used if the restoreaction is triggered through a RestoreAction that define a kanister profile. 
But building this manifest in this specific context is complex. 

---

## Deploying the blueprint

```bash
kubectl apply -f blueprint.yaml
kubectl apply -f blueprintbinding.yaml
```

Label the `Db2uCluster` CR :

```bash

#   This explicit label avoids fragile string derivation from the cluster name.
oc label db2ucluster <db2ucluster-name> -n db2u \
  manage-namespace=<maximo-manage-namespace>

```

Verify:
```bash
kubectl get db2ucluster -n db2u \
  -o jsonpath='{range .items[*]}{.metadata.name}: blueprint={.metadata.annotations.kanister\.kasten\.io/blueprint} manage-namespace={.metadata.labels.manage-namespace}{"\n"}{end}'
```

---

## Running a backup via Kasten

1. Create a Kasten backup policy for the `db2u` namespace with a **Location** profile (S3, GCS,
   Azure Blob, etc.) and run on demand.
2. Kasten discovers all PVCs attached to the `c-mas-instance1-workspace1-manage-db2u-0`
   StatefulSet, calls `backupPrehook`, takes CSI snapshots, then calls `backupPosthook`.
3. Check blueprint execution in the Kanister logs:
   ```bash
   kubectl logs -n kasten-io -l component=kanister --tail=10000 -f | grep -oP "Out\":\"\K(.*?)\""
   ```

---

## Cross-instance restore

> **Use case:** migrating BLUDB data to another Maximo instance — point-in-time migration or
> disaster recovery to a different OCP cluster.

### Why restoring all PVCs breaks client TLS on the destination

When Kasten restores **all** PVCs from the source, the **meta PVC** is replaced with the source
cluster's meta PVC. The meta PVC contains the DB2 SSL keystore (`bludb_ssl.kdb`), which holds the
**source cluster's TLS certificate**. The destination cluster's clients trust the **destination
cluster's** certificate — after overwriting the meta PVC with the source certificate, every client
TLS handshake against the destination fails and **all clients must renew their truststore**.

To avoid this, a cross-instance restore should **only transfer the backup PVC content** (the backup
image + `keystore.p12`/`keystore.sth`). The destination keeps its own meta PVC, data PVC, and
activelogs PVC intact, preserving its own SSL certificate. The `restorePosthook` already copies
the encryption keystore from the backup PVC to `KEYSTORE_LOCATION` before running `db2 restore`,
so decryption is handled correctly without touching the SSL keystore.

The procedure above guarantee that both same and cross cluster will work.

--- 

## Restore steps 

### Step 1 : scale down manage and db2u 

**Manual pre-restore steps (required until the fix ships):**

Before triggering a Kasten restore, manually scale down all workloads that hold DB2 connections.
Two namespaces must be quiesced:

1. **`mas-<instance-id>-manage`** — Maximo application pods that connect to DB2 as SYSADM. Without
   scaling these down, Maximo reconnects immediately after each `FORCE APPLICATION`, and
   `db2 deactivate db BLUDB` in `restorePosthook` stalls indefinitely.
2. **`db2u`** — operator components and ancillary services that may hold connections or attempt
   to reconcile while Kasten replaces the PVCs, leaving the restored PVCs in an inconsistent state.

```bash
# Read the manage namespace from the Db2uCluster label (same source as the blueprint)
DB2U_NS=db2u
echo "Scaling down the manage namespaces"
for db2ucluster in $(kubectl get db2ucluster -o name -n $DB2U_NS)
do 
  MANAGE_NS=$(kubectl get $db2ucluster -n $DB2U_NS \
    -o jsonpath='{.metadata.labels.manage-namespace}')

  echo "Scaling down Maximo namespace: $MANAGE_NS"
  kubectl scale deployment --all -n "$MANAGE_NS" --replicas=0
  echo "Waiting 300s for all pods in $MANAGE_NS to terminate."
  sleep 5
  kubectl delete pod --all -n "$MANAGE_NS"
  kubectl wait pod --all -n "$MANAGE_NS" --for=delete --timeout=300s 2>/dev/null || true
  echo "Pods in $MANAGE_NS terminated."
done


DB2U_NS=db2u
echo "Scaling down DB2u namespace: $DB2U_NS"
kubectl scale deployment --all -n "$DB2U_NS" --replicas=0 2>/dev/null || true
kubectl scale statefulset --all -n "$DB2U_NS" --replicas=0 2>/dev/null || true
for cronjob in $(kubectl get cronjob -o name -n "$DB2U_NS"); do kubectl patch $cronjob -n "$DB2U_NS" -p '{"spec":{"suspend":true}}'; done
kubectl delete pod --all -n $DB2U_NS
kubectl wait pod --all -n "$DB2U_NS" --for=delete --timeout=300s 2>/dev/null || true
echo "DB2u pods terminated."
```

### Step 2 : Only restore the backup pvc 

In the restorepoint unselect all (PVCs and resources) except the backup PVCs for the db2uclusters you want to restore.

Wait for completion of the restoreaction only the backup PVCs that you selected are restored.

### Step 3 : restart only the db2u namespace 

```bash 
DB2U_NS=db2u
echo "Scaling up DB2u namespace: $DB2U_NS"
kubectl scale deployment --all -n "$DB2U_NS" --replicas=1 2>/dev/null || true
kubectl scale statefulset --all -n "$DB2U_NS" --replicas=1 2>/dev/null || true
for cronjob in $(kubectl get cronjob -o name -n "$DB2U_NS"); do kubectl patch $cronjob -n "$DB2U_NS" -p '{"spec":{"suspend":false}}'; done
echo "DB2u pods re-started."
```

Wait for all pods to be ready, it can take 5 to 10 minutes, the pod `c-mas-<instance-id>-<workspace-id>-manage-db2u-0` is the last being ready, 
sometime it restarts.

### Step 4 : Execute the restoration of the database 

Connect to the first pod of the statefulset (eg: c-mas-instance1-workspace1-manage-db2u-0)

```bash
BACKUP_DIR=/mnt/backup/backup
# following the IBM documentation https://www.ibm.com/docs/en/db2/11.5.x?topic=backup-using-restore-script#db2-restore-script_11-5-8__enc
TIMESTAMP=$(su - db2inst1 -c "ls $BACKUP_DIR/BLUDB.*.001 | sort | tail -1 | xargs basename | cut -d. -f5")
echo "Restoring BLUDB from backup timestamp: $TIMESTAMP ..."
db_restore_extdb --bkp-dir ${BACKUP_DIR} --dbname BLUDB --bkp-timestamp $TIMESTAMP --replace --keystore-dir ${BACKUP_DIR}
```

### Step 5 Scale up the manage namespaces 

```bash 
DB2U_NS=db2u
for db2ucluster in $(kubectl get db2ucluster -o name -n $DB2U_NS)
do 
  MANAGE_NS=$(kubectl get $db2ucluster -n $DB2U_NS \
    -o jsonpath='{.metadata.labels.manage-namespace}')
  echo "Scaling up Maximo namespace: $MANAGE_NS"
  kubectl scale deployment --all -n "$MANAGE_NS" --replicas=1  
done
```

---

## Cleanup

Remove restore point content for the test namespace:
```bash
kubectl delete restorepointcontent -l k10.kasten.io/appNamespace=db2u
```

Remove the blueprint and binding:
```bash
kubectl delete -f blueprintbinding.yaml
kubectl delete -f blueprint.yaml
```
