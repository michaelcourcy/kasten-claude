# DataStage on Cloud Pak for Data — engine-state protection with Kasten

Protect the **DataStage parallel-engine state** inside an existing Cloud Pak for Data (IBM Software
Hub) installation: custom libraries, driver configuration, compiled job artifacts, run history, Data
Set descriptors and Data Set data. Kasten snapshots the DataStage engine volumes; the blueprint runs
IBM's own health precheck, flushes the filesystems before the snapshot, and takes the operator out of
maintenance mode after a restore.

DataStage flows and jobs are CP4D **project assets** and are protected by
[cp4d-example](../cp4d-example/), not by this blueprint.

## Pick the folder for your DataStage version

What the blueprint must do depends on the DataStage version. IBM changed its own backup hooks between
5.2.2 and 5.4.7: on 5.4 they create a backup lock file on `/ds-storage`, and the blueprint must take
and release that lock. Each version that was validated has its **own folder**. Use the folder that
matches your installation, and follow the `README.md` inside it.

| CP4D / IBM Software Hub | DataStage | Folder | OpenShift | Kasten |
|---|---|---|---|---|
| 5.4.0 Patch 7 | 5.4.7 | [5.4/](5.4/) | 4.21.30 | 9.0.7 |
| 5.2.2 | 5.2.2 | [5.2/](5.2/) | 4.21.30 | 9.0.7 |

A folder is a **snapshot of what was validated** on that version. It is not updated when another
version moves on. Restore into the **same** DataStage version as the backup: compiled artifacts are
engine-version specific. If your version is not listed, start from the nearest folder and re-run the
validation (Steps 2–5 of [AGENTS.md](../AGENTS.md)) before you rely on it.

## Shared across versions

| File | Purpose |
|---|---|
| [datastage.pptx](datastage.pptx) | Concept deck, built from [ppt-tools/deck.py](ppt-tools/deck.py) |
