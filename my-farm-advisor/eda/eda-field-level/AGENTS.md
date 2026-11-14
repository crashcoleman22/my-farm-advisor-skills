# Local Instructions

## Purpose

This folder owns the field-level EDA workflow: boundary, weather, and USDA CDL
analysis across growers, plus the static report handoff.

## Safe edit scope

Edits should stay in this folder and its children unless the user explicitly
asks for a broader skill change. Do not change parent `SKILL.md`, sibling EDA
workflows, or root policy from a subskill task unless explicitly requested.

## Read nearby docs first

Read `GUIDE.md` first. If routing context is needed, read `../INDEX.md` and
`../../SKILL.md`. Read `../../data-pipeline/AGENTS.md` before changing runtime
paths, install behavior, or pipeline commands.

## Script boundaries

- Analysis lives in `data-pipeline/src/scripts/eda/eda_field_level.py` and
  `data-pipeline/src/scripts/lib/eda_metrics.py`.
- Report generation lives in
  `data-pipeline/src/scripts/reporting/generate_eda_report.py`.
- Keep the analysis read-only. It must not download data or write inside a
  grower tree; write to `shared/eda/<run-slug>` or an explicit `--out` path.
- Keep report generation separate from the reusable analysis command.

## Local validation

Run the offline metric tests with the runtime venv:

```bash
python -m unittest \
  my-farm-advisor/data-pipeline/src/scripts/tests/test_eda_metrics.py
```

Run `./scripts/validate.sh` from the repository root after structural changes.

## Local-delta-only reminder

This nested AGENTS.md only records instructions that differ from the parent or
root files. Do not duplicate root-wide asset, vendor, or validation policy here
except this pointer to `../../../AGENTS.md`.
