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
    "Why there is no quiesce at backup time",
    "Deriving the flush set from the policy's own selector",
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
    "Why there is no quiesce at backup time",
    "IBM's online hooks\n", "are empty.",
    [
        ("01", "THE CONFIGMAP SAYS SO EXPLICITLY",
         "datastage-maint-aux-ckpt-cm declares pre-hooks, post-hooks and checkpoint "
         "hooks as exec-rules: [] — all three empty. Its aux-meta.managed-resources is "
         "empty too, so the online path scales nothing down."),
        ("02", "THE ONE REQUIREMENT IS A PRECHECK",
         "The DataStage and PXRuntime custom resources must report dsStatus == "
         "Completed, with on-error: Fail. So backupPrehook fails the backup before "
         "anything is snapshotted if the engine is not settled."),
        ("03", "MAINTENANCE MODE IS THE OTHER PATH",
         "It belongs to the offline backup path and to restores, not to a routine "
         "backup. Putting the engine into maintenance mode for every snapshot is not "
         "what IBM's specification asks for, and it would not make the backup safer."),
    ],
    left_body="One detail worth copying: the PXRuntime entry in that rule has no name "
              "field, while the DataStage entry does. So it applies to every PXRuntime "
              "CR in the namespace. The blueprint loops over kubectl get pxruntime "
              "rather than assuming a single default instance.")

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

d.cards(
    "What the policy captures, and how the restore frees the volumes",
    "The filter has two entries: the labelled PVCs, and the one workload the hooks are bound to",
    [
        ('scan_target', "INCLUDE THE BOUND WORKLOAD, AND ONLY IT", [
            "The hooks are bound to the runtime Deployment. Leave it out of the filter and "
            "the hooks never run, and Kasten does not snapshot the PVC it mounts.",
            "",
            "But every included workload brings in every PVC it mounts, labelled or not. "
            "Including all the DataStage workloads added three unlabelled PVCs, one of them "
            "owned by another CP4D service. The runtime Deployment mounts only ds-storage.",
        ]),
        ('error_warning', "RESTORE: STOP WHAT IBM STOPS", [
            "Kasten can only replace a PVC once no pod mounts it, and it scales down only "
            "the workloads it restores — here, just one.",
            "",
            "So the pre-restore script parks the operator, then scales down the three "
            "selectors IBM's own restore ConfigMap lists. After the restore, the operator "
            "scales them back up itself.",
        ]),
    ])

d.table_slide(
    "Versions and blueprint actions",
    "The sync runs last, so nothing can dirty the page cache after it",
    ["Action", "What it does"],
    [
        ("backupPrehook",
         "Runs IBM's precheck: the DataStage CR and every PXRuntime CR must report "
         "dsStatus == Completed, or the backup fails before anything is captured. "
         "Discovers every running pod mounting a PVC in the backup, warns if the label "
         "matches nothing and names any labelled PVC no pod mounts, warns if a job run "
         "is in flight, then runs sync in each pod — last."),
        ("restorePosthook",
         "Sets spec.ignoreForMaintenance: false on the DataStage CR and every PXRuntime "
         "CR, idempotently, then waits up to 1800 seconds for all of them to report "
         "dsStatus == Completed — so the restore does not report success while the "
         "engine is still assembling itself. Prints the recompile reminder."),
    ],
    col_widths=[2.6, 9.7],
    row_heights=[0.5, 1.8, 1.5],
    body_size=10.5,
    cards=[
        ('file_list', "TESTED VERSIONS", [
            "Kubernetes 1.34.9 on OpenShift 4.21.30, Kasten 9.0.7, Cloud Pak for Data "
            "5.2.2, DataStage 5.2.2 with operator ibm-cpd-datastage-operator.v8.2.0, "
            "Common Core Services 11.2.0, cpdctl 1.10.3, cpd-cli 14.2.2. The ConfigMaps "
            "are versioned — 5.2.2 here — and IBM can change them between releases, so "
            "read them on your own cluster before adapting anything."]),
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
