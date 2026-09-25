# Pipeline contract

The single source of truth for the file interface between the
coordinator and the stage scripts (`prepare.py`, `run.py`, `analyse.py`).
Each script implements this contract independently — there is no shared
code between them, on purpose.

## Run-dir layout

```
<run_dir>/
  manifest.json      # coordinator-owned provenance (see below)
  prep/
    status.json
    <stem>.pdbt                  # prepared receptor, input stem preserved
    <stem>_autobox.pdbt          # autobox reference, input stem preserved (autobox only)
    ligands/<stem>.pdbt          # one file per ligand, flat; input stem preserved
    prep.log                     # prepare.py's own log
  dock/
    status.json
    config.txt                   # the exact config passed to vinardock
    vinardock.log                # captured stdout+stderr of the run
    attempts/<signature>[-N]/DOCK/  # fresh attempt; never reuses mtime cache
      <file>.pdbt                      # single-ligand runs (flat)
      <2-char>/<stem>/<file>.pdbt      # batch runs (>1 ligand → VS mode)
      <receptor>_modified.pdbt         # flex runs only
      log.csv
  analysis/
    status.json
    <name>_complex.pdb
    <name>_report.xml
    <name>_report.txt
  report.md            # written by analyse.py, header = provenance block
```

Input files are **not copied** into the run dir. The coordinator records
each input's absolute path and sha256 in `manifest.json.inputs` and the
stage scripts read the originals in place. There is no `inputs/`
directory.

Write scope: a stage may only write inside its own directory
(`prep/`, `dock/`, `analysis/`). `manifest.json` belongs to the
coordinator. `report.md` is written by analyse.py as a special
exception.

## status.json

Written **atomically** (write `status.json.tmp`, then `os.replace`) so a
reader never sees a partial file.

```json
{
  "stage": "prepare|run|analyse",
  "status": "ok|failed",
  "started": "ISO-8601",
  "finished": "ISO-8601",
  "artifacts": [{"path": "<relative to run_dir>", "sha256": "<hex>"}],
  "metrics": {},
  "warnings": [],
  "error": null
}
```

Exit codes: `0` = ok, `1` = stage failed (status.json still written),
`2` = usage error (status.json may not exist).

### Per-stage metrics

**prepare** — `n_ligands`, `ligands` (names), `receptor_chains`,
`hetatm_resnames` (receptor non-ATOM residues found), `ligand_hashes`
({name: sha256 of pdbt}), `receptor_file` / `autobox_file` (prepared
pdbt filenames inside `prep/`).

**run** — `recipe` ({name, sha256}), `seed`, `threads`, `resolved_box`
({center, size} — from `[WORKFLOW] Search box:` stdout when autobox),
`ligand_hashes` and `receptor_hash` (prepared input hashes used),
`receptor_file` (prepared receptor filename inside `prep/`),
`output_dir` (current attempt), `per_ligand` ({name: {energies:
[per model], best, nconfs, torsdof, rmsd}}), plus any extra log.csv
columns (`E_corrected`, `BE_ligwater`, `BE_recwater`).

**analyse** — `analyzed_pose` ({name: "k of N"}), `interactions`
({name: {hbonds, salt_bridges, pi_stacks, hydrophobic, …}}), `report`
(path to report.md), `dock_status_sha256` (analysis input gate).

## manifest.json

Written by the coordinator at run start, extended after each gate.
`inputs` paths are the original absolute paths (not copies); each entry
also carries the sha256 captured at run start and re-checked on resume.

```json
{
  "created": "ISO-8601",
  "tools": {
    "vinardock": {"path": "…", "sha256": "…", "build_id": "…", "source": "local|download"},
    "obabel-vinardock": {"path": "…", "sha256": "…", "version": "…"},
    "plip": {"version": "…", "openbabel_bindings": "…"},
    "param": {"param.dat": "<sha256>", "param.TxT.dat": "<sha256>", "dun2010bbdep.bin": "<sha256>"}
  },
  "env": {"cpu": "…", "nproc": 0, "threads": 0, "hostname": "…", "python": "…"},
  "inputs": {"receptor": {"path": "…", "sha256": "…"}, "ligands": [{"path": "…", "sha256": "…"}]},
  "run": {"seed": 0, "conformations": 0, "recipe": {"name": "…", "sha256": "…"},
          "box": {"mode": "manual|autobox"}, "resolved_box": {},
          "config_sha256": "…"},
  "stages": {"prepare": "ok", "run": "ok", "analyse": "ok"}
}
```

## Gates (coordinator verification after each stage)

After every stage script returns, the coordinator MUST:

1. Read the stage's `status.json`.
2. `status == "ok"` AND every listed artifact exists AND its sha256
   recomputes to the recorded value → proceed.
3. Otherwise → stop. Report the `error` field and the last ~20 lines of
   the stage log (`prep/prep.log`, `dock/vinardock.log`) to the user.

A stage script may return exit 0 with `status: "failed"` — trust the
file, not the exit code alone. Conversely exit ≠0 without a status.json
means a usage/crash error — read stderr.

## Resume

- A stage whose `status.json` is `ok` and whose artifacts still
  hash-match is **skipped** unless the user passes `--force` semantics
  (coordinator decides; scripts always re-run when invoked — skipping is
  a coordinator decision).
- `workflow.py` records input paths/hashes and provenance at start, checks
  all artifact hashes after each stage, and refuses to resume when
  inputs, tools, recipe, or settings differ. A stage rerun is fresh:
  `run.py` creates a new `dock/attempts/<signature>[-N]/DOCK` directory;
  it never deletes old outputs or invokes vinardock's mtime-based cache.
  `dock/status.json.metrics.output_dir` is the current path.
- When checking an existing run, compare its `ligand_hashes` and
  `receptor_hash` to current preparation and the analysis' stored
  `dock_status_sha256` to the current dock status. Never reuse analysis
  for a different docking result.
- `manifest.json.stages` records which stages passed, so the coordinator
  can state precisely what was re-used vs re-executed.
