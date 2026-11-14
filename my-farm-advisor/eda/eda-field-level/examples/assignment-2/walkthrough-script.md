# Assignment 2 video walkthrough script (target 3:30)

Screen recording: terminal, browser, image viewer. Numbers below match the
committed run (`manifest.json`, seed 2026, 30 fields).

---

## 0:00-0:25 - What this is

> "Assignment 2: field-level exploratory data analysis and spatial comparison for
> My Farm Advisor. Three growers, one farm each, ten fields each, in Illinois,
> Iowa, and Nebraska. Thirty fields total. Everything is produced by two committed
> scripts from real OpenStreetMap boundaries, NASA POWER weather, and USDA
> Cropland Data Layer rasters for 2021 through 2025."

Show: repo root, branch `assignment-2`, `git log --oneline -3`.

## 0:25-0:55 - Repo vs runtime, analysis vs report

> "The repo holds code and docs only. Raw and generated data live outside Git
> under DATA_PIPELINE_DATA_ROOT. The reusable analysis is eda_field_level.py; the
> HTML report is a separate script, generate_eda_report.py, so the analysis can be
> reused without the report step."

Show: `ls ${DATA_PIPELINE_DATA_ROOT}/data-pipeline/growers` (three growers), then
`ls shared/eda/field-level` (figures, tables, report, manifest).

## 0:55-1:30 - Boundaries (category A)

Show: `fig01`, `fig02`, `fig03`.

> "A1 is field area on a log scale. Median area is 76 acres in Illinois, 147 in
> Iowa, and 127 in Nebraska. A2 separates shape from size: Polsby-Popper
> compactness plus rectangle fill. Nebraska is the only state with pivot circles:
> 40 percent pivot-like, because Phelps County is center-pivot irrigated. Illinois
> is 80 percent irregular, Iowa is 80 percent rectangular. A3 checks perimeter-area
> scaling on log-log axes; slopes 0.50 to 0.59 mean the outlines are close to
> self-similar, and the Spearman rho uses a permutation test."

## 1:30-2:05 - Weather (category B)

Show: `fig04`, `fig05`, `fig06`.

> "B1 is April to October precipitation for every field-year. B2 is cumulative
> growing degree days, base 10 Celsius with a 30 degree cap. Median season totals:
> Illinois 1,964 GDD, Iowa 1,713, Nebraska 2,062, with Nebraska also the driest at
> 467 millimetres. B3 is the spatial comparison: daily mean temperature correlation
> against distance. Same-farm pairs sit at about 1.0 because all fields in a farm
> share one NASA POWER cell; the informative signal is the between-state gradient,
> which falls to about 0.91 from Nebraska to Illinois at roughly 950 kilometres."

## 2:05-2:45 - CDL crop history (category C)

Show: `fig07`, `fig08`, `fig09`.

> "C1 is the dominant crop for every field-year. Illinois alternates corn and
> soybean almost perfectly. Iowa and Nebraska keep more corn-on-corn: Nebraska has
> 20 percent continuous corn, which is typical for irrigated pivots. C2 combines
> area-weighted crop mix by state-year with Shannon diversity per field. C3 is the
> row-normalised transition matrix; Illinois is pure off-diagonal rotation, while
> Nebraska and Iowa have a strong corn-to-corn diagonal. One Nebraska polygon is
> tagged farmland in OSM but CDL reads it as forest and pasture, so it is kept and
> grouped as Other instead of being quietly dropped."

## 2:45-3:15 - Static map and report

Show: `fig10`, then the HTML report in a browser, then `manifest.json`.

> "D is the single static map: real polygons, coloured by the latest CDL crop, over
> an Esri basemap, with the pivots clearly visible in Nebraska. No dashboard. The
> report embeds every figure and table in one self-contained HTML file, and the
> manifest records input SHA-256 hashes, years, seed, and every output path."

## 3:15-3:35 - Reproduce and wrap up

Show: the two commands from the example README.

> "Reproducing this is two commands: run the EDA with the three growers, then run
> the report generator. Offline unit tests cover the metric helpers, and the repo
> validator passes. That is the deliverable: reusable analysis, separate report,
> static outputs only."

---

## Recording checklist

- [ ] Terminal font large enough for 1080p; close unrelated windows.
- [ ] Report opens locally from
      `shared/eda/field-level/report/field_level_eda_report.html`.
- [ ] End on `git status` clean and the PR URL.
