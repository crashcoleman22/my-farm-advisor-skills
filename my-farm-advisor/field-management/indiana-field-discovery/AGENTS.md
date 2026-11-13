# Local Instructions

## Purpose

This folder owns the county-scale field discovery pipeline: CSB boundaries,
SSURGO soils, CDL crop history, and streamed Sentinel-2 NDVI.

## Safe edit scope

Edits stay in this folder and its children unless the user asks for a broader
change. Do not modify parent `SKILL.md`, sibling field-management workflows, or
root policy from a subskill task.

## Read nearby docs first

Read `GUIDE.md` first. For routing context read `../INDEX.md` and `../../SKILL.md`.
For runtime path conventions read `../../../data-pipeline/AGENTS.md`.

## Generated data

Never write datasets, imagery, temporary rasters, or virtual environments into the
repository. All runtime output belongs under `INDIANA_RUNTIME_ROOT`, outside Git.

## Local validation

- `python -m compileall src/indiana_field_discovery` for syntax.
- Run the 10-field NDVI pilot before any county run.
- Run `./scripts/validate.sh` from the repository root after structural changes.
