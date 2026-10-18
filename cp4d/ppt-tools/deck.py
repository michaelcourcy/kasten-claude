#!/usr/bin/env python3
"""Concept deck for cp4d-example. Content only — the slide machinery lives in the
shared toolkit at ../../ppt-tools/veeam_deck.py.

    python3 ppt-tools/build.py cp4d-example

Note: this directory also contains DESIGN.pptx, built by tools/build-design-deck.py
from DESIGN.md. That is a separate, longer design document deck; this one is the
short concept deck.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'ppt-tools'))
from veeam_deck import Deck  # noqa: E402

d = Deck('cp4d', 'cp4d-example')

d.title("CLOUD PAK FOR DATA:\nARTIFACT-LEVEL BACKUP",
        "One PVC per project, so restore is granular and multi-tenant")

d.agenda([
    "What this covers, and what cpdbr covers",
    "The pattern: an action blueprint, not a workload blueprint",
    "One PVC per project — the whole point",
    "Four design decisions",
    "Blueprint action",
])

d.hero(
    "ONE PVC PER PROJECT", "IS THE UNIT OF RESTORE",
    body="PVC equals granularity equals multi-tenant restore. Pick the projects you "
         "want, restore only\nthose PVCs into a namespace, and recreate the projects "
         "with a script.",
    pills=["Granular", "Multi-tenant", "Portable between clusters"],
    icon='folder_directory')

d.cards(
    "This complements the platform backup — it does not replace it",
    "cpdbr and this blueprint cover different lanes; you want both",
    [
        ('server_hardware', "WHAT CPDBR COVERS", [
            "Whole-tenant infrastructure disaster recovery, including the control "
            "plane.",
            "",
            "That is the platform's own backup, and it remains the right tool for "
            "rebuilding a tenant.",
        ]),
        ('checkbox_multiple', "WHAT THIS COVERS", [
            "The lane cpdbr does not enter: per-project restore, portability between "
            "clusters, and self-service recovery.",
            "",
            "Analytics projects at the logical level — notebooks, jobs, data assets and "
            "connections — exported with cpdctl.",
        ]),
        ('error_warning', "WHAT IT NEEDS, AND DOES NOT PROTECT", [
            "It needs the CP4D platform to be up, because the API is the only path to "
            "a notebook.",
            "",
            "It does not protect the control plane. If the platform is gone, this "
            "blueprint cannot bring it back — restore the platform first, then the "
            "projects.",
        ]),
    ])

d.split_dark(
    "Why this is an action blueprint",
    "No CR to bind to.\n", "So bind to the namespace.",
    [
        ("01", "THERE IS NO PROJECT OR NOTEBOOK CR",
         "A CP4D analytics project is not a Kubernetes object, so there is nothing for "
         "a BlueprintBinding to match. This is therefore a BackupAction preHook — an "
         "action blueprint — rather than a workload blueprint."),
        ("02", "AND THAT IS ALSO WHY IT WORKS",
         "A preHook runs before Kasten's PVC discovery. So every per-project PVC the "
         "hook creates is included in the restore point — which a backupPrehook on a "
         "workload could never achieve, because discovery would already have run."),
    ],
    left_body="The pattern is a hybrid: pattern 4, a permanent keeper PVC, combined "
              "with the action-hook mechanism. Note the usual trade-off of action "
              "hooks: the context is namespace-only, and one blueprint covers "
              "everything in the namespace — so deploy one type of workload per "
              "namespace when you use this shape.")

d.cards(
    "Four decisions that shape the design",
    "Each one exists to make a specific failure or cost go away",
    [
        ('file_list', "PERMANENT, GUID-KEYED PVCs", [
            "Named cp4d-<sanitized-name>-<short-guid>, created if missing and reused "
            "afterwards.",
            "",
            "The GUID rather than the name is what makes a renamed project keep its "
            "history instead of starting a new PVC.",
        ]),
        ('speed_up', "STORE THE EXPORT UNZIPPED", [
            "Not the zip. A one-cell change then rewrites one file rather than the "
            "whole archive.",
            "",
            "That is what lets Kasten's content-addressed export deduplicate between "
            "runs — a zip would change wholesale every time.",
        ]),
        ('loop_left', "RESTORE IS DECOUPLED", [
            "A standalone restore.sh does the work, rather than a Kasten restore hook.",
            "",
            "So a restore can select individual projects, and does not depend on "
            "restore-hook behaviour.",
        ]),
        ('lock_password', "CREDENTIALS DO NOT FAN OUT", [
            "The CP4D API key lives in one Secret in the cpd namespace — not copied "
            "into each project namespace.",
            "",
            "One place to rotate, one place to audit.",
        ]),
    ],
    body_size=11)

d.table_slide(
    "Versions and the blueprint action",
    "One action, and it fails the whole backup rather than protecting some projects quietly",
    ["Component", "Version"],
    [
        ("Kubernetes", "1.34.9"),
        ("OpenShift", "4.21.30"),
        ("Kasten", "9.0.7"),
        ("Cloud Pak for Data", "5.4.0 Patch 7 (Watson Studio 13.0.4)"),
        ("cpdctl", "1.10.34"),
        ("Keeper image", "cp4d-backup:1.10.34-1"),
    ],
    col_widths=[2.7, 3.15],
    row_heights=[0.5] + [0.52] * 6,
    side=('upload_cloud', "backupPrehook — WHAT orchestrate.sh DOES",
          "Authenticates cpdctl using a cross-namespace secret.\n"
          "Enumerates all CP4D analytics projects.\n"
          "Enrols itself as editor on any project it is not already a member of — and "
          "aborts before any data moves if that fails.\n"
          "Ensures one permanent GUID-keyed PVC per project.\n"
          "Launches one export pod per project, with bounded concurrency, one retry, "
          "and an atomic unzip into /backup/current.\n"
          "Prunes the PVCs of deleted projects.\n"
          "Deletes the export pods, so nothing sensitive is left to be captured.\n"
          "Exits non-zero — failing the whole backup — if any project fails to enrol "
          "or export."))

d.hero(
    "IT ENROLS ITSELF,", "OR IT REFUSES TO RUN",
    body="A backup that silently skips the projects it cannot read is worse than one "
         "that fails:\nit reports success while leaving exactly the projects nobody "
         "checked unprotected.",
    pills=["Enrol as editor, or abort", "Before any data moves"],
    icon='shield_check')

d.ending("THE PVC IS THE UNIT OF GRANULARITY",
         "Export per project, snapshot per project, restore per project.\n"
         "github.com/kastenhq/blueprint-ai — cp4d-example")

d.save()
