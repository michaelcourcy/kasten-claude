# CP4D artifact-level backup with Kasten

Back up **Cloud Pak for Data analytics projects** (notebooks, jobs, data assets, connections) at the
**logical / artifact level** using `cpdctl` project export/import, with **Kasten as the data mover**.
Each CP4D project is exported into its **own PVC**; Kasten snapshots those PVCs. Restore is
**granular**: pick the project(s) you want, restore only those PVCs, and recreate the projects with
a script.

## Pick the folder for your CP4D version

The blueprint, its keeper image and its runbook depend on the CP4D version: `cpdctl` changes between
releases, and so do the APIs it calls. Each version that was validated has its **own folder**. Use the
folder that matches your installation, and follow the `README.md` inside it.

| CP4D / IBM Software Hub | Folder | OpenShift | Kasten | Keeper image |
|---|---|---|---|---|
| 5.4.0 Patch 7 | [5.4/](5.4/) | 4.21.30 | 9.0.7 | `cp4d-backup:1.10.34-1` |
| 5.2.x | [5.2/](5.2/) | 4.18.6 | 8.5.12 | `cp4d-backup:1.8.244-3` |

A folder is a **snapshot of what was validated** on that version. It is not updated when another
version moves on. If your version is not listed, start from the nearest folder and re-run the
validation (Steps 2–5 of [AGENTS.md](../AGENTS.md)) before you rely on it.

A bundle exported on 5.2.2 imports into 5.4.0 — see [DESIGN.md § 7](DESIGN.md#7-why-latest-cpdctl-is-the-right-version-policy). Any other version
pair is a separate validation.

## Shared across versions

| File | Purpose |
|---|---|
| [DESIGN.md](DESIGN.md) | Why the design is what it is — read it before any version folder |
| [DESIGN.pptx](DESIGN.pptx) | The same rationale as a deck, built by [tools/build-design-deck.py](tools/build-design-deck.py) |
| [cp4d.pptx](cp4d.pptx) | Concept deck, built from [ppt-tools/deck.py](ppt-tools/deck.py) |

The DataStage engine state is protected by a separate blueprint with the same folder layout:
[datastage-example](../datastage-example/).
