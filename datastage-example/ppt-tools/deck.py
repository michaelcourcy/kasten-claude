#!/usr/bin/env python3
"""Concept deck for datastage-example. Content only — the slide machinery lives in
the shared toolkit at ../../ppt-tools/veeam_deck.py.

    python3 ppt-tools/build.py datastage-example
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'ppt-tools'))
from veeam_deck import Deck  # noqa: E402

d = Deck('datastage', 'datastage-example')

d.title("DATASTAGE ON CLOUD PAK FOR DATA:\nENGINE-STATE PROTECTION",
        "A transcription of IBM's own backup hooks into Kanister")

d.agenda([
    "Three stores, and which one this protects",
    "Where the design comes from",
    "What IBM asks for at backup time, per version",
    "Deriving the flush set from the policy's own selector",
    "Shared volumes: bind every workload, coordinate with a lock",
    "What the policy captures, and how the restore frees the volumes",
    "Blueprint actions and restore prerequisites",
])

d.hero(
    "ALMOST NOTHING HERE", "WAS INVENTED",
    body="The DataStage install ships three ConfigMaps that IBM's own backup tool reads "
         "to protect DataStage.\nThey are a machine-readable specification, written by "
         "the people who wrote DataStage.",
    pills=["datastage-maint-aux-qu-cm", "-br-cm", "-ckpt-cm"],
    icon='contract_agreement')

d.table_slide(
    "Three stores, measured on the cluster rather than taken from documentation",
    "This blueprint covers the middle one — and only that one",
    ["Store", "What is in it", "Protected by"],
    [
        ("CP4D project assets",
         "Flows, jobs, connections, parameter sets, table definitions, project "
         "settings, runtime environments, Data Set assets",
         "cp4d-example — a project export and import"),
        ("DataStage engine PVCs",
         "/ds-storage: compiled flows and jobs, run history and logs, Data Set "
         "descriptors, user-lib custom libraries, connectors/odbc. /px-storage, one per "
         "runtime instance: Data Set and File Set data, files written to filesystem "
         "paths, engine and workload-manager configuration. Plus any user volume an "
         "administrator mounted into the runtime",
         "This blueprint, for every PVC you label"),
        ("External data sources",
         "The databases and files the flows read and write, behind connections",
         "Whoever owns that database"),
    ],
    col_widths=[2.5, 6.6, 3.2],
    row_heights=[0.5, 0.9, 1.9, 0.6],
    body_size=10)

d.cards(
    "One logical object can live in three places at once",
    "Which is why the two blueprints are complementary rather than alternatives",
    [
        ('file_list', "A DATA SET, THREE TIMES OVER", [
            "A single Data Set exists as an asset in the project, as a descriptor on "
            "/ds-storage, and as partitioned data files under /px-storage/pds_files.",
            "",
            "One logical object, three places — so protecting only one of them "
            "restores something that looks present and does not work.",
        ]),
        ('close_circle', "FLOWS AND JOBS ARE NOT ENGINE STATE", [
            "This blueprint deliberately does not protect DataStage flows and jobs. "
            "Those are CP4D project assets, and a project export and import restores "
            "them — verified, not assumed.",
            "",
            "Use cp4d-example for that half. The two are deployed independently and "
            "combine in a full rebuild.",
        ]),
    ])

d.split_dark(
    "What IBM asks for at backup time, per version",
    "IBM's own hooks\n", "change between releases.",
    [
        ("01", "5.2 AND 5.4: A PRECHECK, AND SINCE 5.4 A LOCK FILE",
         "datastage-maint-aux-ckpt-cm, IBM's online path, has no checkpoint hooks and "
         "scales nothing down. It requires dsStatus == Completed on the DataStage and "
         "PXRuntime custom resources, with on-error: Fail. Since 5.4 it also creates and "
         "removes a backup lock file on /ds-storage."),
        ("02", "4.8: MAINTENANCE MODE AROUND THE BACKUP",
         "4.8.9 ships no online path at all. Its only backup specification, br-cm, puts "
         "both custom resources in maintenance mode before the backup and takes them out "
         "after. That pauses the operator, not the engine: measured, every pod kept running."),
        ("03", "EVERY VERSION: THE RESTORE IS THE OFFLINE PATH",
         "A Kasten restore replaces the PersistentVolumeClaims, so the workloads must be "
         "down. That is IBM's offline situation, and its restore hook — leave maintenance, "
         "wait for Completed — is the one the blueprint uses."),
    ],
    left_body="Read the ConfigMaps on your own cluster before adapting anything. Each "
              "version has its own folder in the repository because the specification is "
              "not the same from one release to the next.")

d.hero(
    "WITHOUT A SYNC,", "THE FILE IS ZERO BYTES",
    body="A CSI snapshot captures the block device, and Kasten does not freeze the "
         "filesystem. Data the writer\nhas acknowledged but the kernel has not flushed "
         "is simply not in the snapshot.",
    pills=["Right name", "Zero bytes", "Found only at restore time"],
    icon='error_warning', polarity='neg')

d.cards(
    "Deriving the flush set from the policy's own selector",
    "If the prehook discovers writers differently from how the policy selects PVCs, the two sets drift",
    [
        ('scan_target', "READ THE SAME LABEL THE POLICY READS", [
            "The prehook resolves the union of the PVCs carrying "
            "blueprint-ai/datastage-engine=true — the same label the policy selects on "
            "— and anything matching the engine naming pattern as a safety net.",
            "",
            "So user volumes are covered, and adding a PVC to the backup flushes it "
            "automatically. The naming pattern alone would have missed them: a user "
            "volume is neither created by the install nor matched by the engine names.",
        ]),
        ('error_warning', "AND WARN ON BOTH SILENT FAILURES", [
            "It warns if no PVC carries the label at all — the backup would then "
            "capture no engine volume.",
            "",
            "And it names any labelled PVC that no running pod mounts, because that one "
            "gets snapshotted with no chance to flush. Both failures are otherwise "
            "invisible: the backup succeeds and the file is empty.",
        ]),
    ])

d.hero(
    "ONE BOUND WORKLOAD", "DOES NOT HOLD THE SNAPSHOT",
    body="The engine volumes are shared by up to seven workloads. Kasten picks one of "
         "them for each volume, and only\nthat workload's prehook holds back the "
         "snapshot. With the hook on another one, the snapshot starts at once.",
    pills=["Kasten defect, reported", "Shared RWX volumes", "Waited in 5 of 9 runs"],
    icon='error_warning', polarity='neg')

d.cards(
    "Shared volumes: bind every workload, coordinate with a lock",
    "Whichever workload Kasten picks has a prehook, and the work still runs once",
    [
        ('scan_target', "BIND AND INCLUDE EVERY DATASTAGE WORKLOAD", [
            "The BlueprintBinding and the policy select the same set: "
            "icpdsupport/addOnId=datastage, ten workloads on 4.8.9. A workload outside "
            "the policy runs no hook, so the two lists must stay identical.",
            "",
            "A ConfigMap created with kubectl create is the lock: the one prehook that "
            "creates it quiesces and flushes, the others wait until it says ready. No "
            "snapshot can start before the quiesce.",
        ]),
        ('error_warning', "SAFE AGAINST RETRIES, FAILURES AND THE DEFECT ITSELF", [
            "The lock records the running BackupAction, whose UID Kasten also puts on "
            "every snapshot of the backup. A retried hook sees the volumes are already "
            "captured; a dead lock is taken over; a failed leader fails its followers.",
            "",
            "A snapshot taken before the quiesce makes the posthook fail the backup — "
            "the silent case becomes a visible one. Validated end to end on 4.8.9.",
        ]),
    ])

d.cards(
    "What the policy captures, and how the restore frees the volumes",
    "The filter has two entries: the labelled PVCs, and the workloads the hooks are bound to",
    [
        ('scan_target', "INCLUDE EVERY BOUND WORKLOAD", [
            "Leave a bound workload out of the filter and its hooks never run, and Kasten "
            "may not snapshot the PVC it picked that workload for at all.",
            "",
            "Every included workload brings in every PVC it mounts, labelled or not: on "
            "4.8.9 that adds ds-migration and conn-home-pvc, which belongs to another CP4D "
            "service. They are snapshotted, never restored. The 5.x folders include the "
            "runtime Deployment only, and are exposed to the shared-volume defect.",
        ]),
        ('error_warning', "RESTORE: STOP WHAT IBM STOPS", [
            "Kasten can only replace a PVC once no pod mounts it, and it scales down only "
            "the workloads it restores — the restore filter picks only the runtime "
            "Deployment, so the restore hook runs once.",
            "",
            "So the pre-restore script parks the operator, then scales down the three "
            "selectors IBM's own restore ConfigMap lists. After the restore, the operator "
            "scales them back up itself.",
        ]),
    ])

d.table_slide(
    "Versions and blueprint actions",
    "4.8: one hook per DataStage workload; the lock decides which one does the work",
    ["Action", "What it does"],
    [
        ("backupPrehook",
         "Takes or follows the lock. The leader waits for dsStatus == Completed, enters "
         "maintenance mode (IBM's enable-maint) and waits for InMaintenance, finds every "
         "running pod mounting a labelled engine PVC, warns about gaps and jobs in "
         "flight, runs sync in each pod — last — and marks the lock ready. Followers wait "
         "for that, or exit at once if this backup's volumes are already captured."),
        ("backupPosthook",
         "Checks this backup's snapshot of every labelled PVC. Taken before the quiesce: "
         "fails the backup. Not all taken yet: exits without waiting. All taken: leaves "
         "maintenance mode (disable-maint), marks the lock released, waits for Completed."),
        ("restorePosthook",
         "Sets spec.ignoreForMaintenance: false on the DataStage CR and every PXRuntime "
         "CR, idempotently, then waits up to 1800 seconds for all of them to report "
         "dsStatus == Completed — so the restore does not report success while the "
         "engine is still assembling itself. Prints the recompile reminder."),
    ],
    col_widths=[2.6, 9.7],
    row_heights=[0.5, 1.6, 0.8, 1.4],
    body_size=10.5,
    cards=[
        ('file_list', "TESTED VERSIONS", [
            "One folder per version. 4.8.9: OpenShift 4.18.56, Kasten 9.0.8, cpd-cli "
            "13.1.9, nfs-csi. 5.4.0 Patch 7 with DataStage 5.4.7 and 5.2.2: OpenShift "
            "4.21.30, Kasten 9.0.7. IBM changed its backup ConfigMaps between each of "
            "them, so read them on your own cluster before adapting anything."]),
        ('error_warning', "RESTORE PREREQUISITES", [
            "The same DataStage and CP4D version as the backup: compiled artifacts are "
            "engine-version specific, and a Data Set descriptor embeds the engine build "
            "string. DataStage must also already be installed and reconciled — nothing "
            "restores into a DataStage that is not there. Before the restore, run "
            "prepare-restore.sh: it parks the operator and stops every pod mounting the "
            "engine volumes."]),
    ])

d.ending("TRANSCRIBE THE VENDOR'S SPECIFICATION, DO NOT GUESS IT",
         "Three ConfigMaps say what must happen around a snapshot. Read them on your "
         "own cluster.\ngithub.com/kastenhq/blueprint-ai — datastage-example")

d.save()
