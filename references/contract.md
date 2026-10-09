# Pipeline contract

The single source of truth for the file interface between the
coordinator and the stages of `scripts/pipeline.py`
(`prepare`, `dock`, `analyse` subcommands; `workflow` orchestrates them
in-process). Stages communicate only through the run-dir filesystem —
each can also be invoked standalone.

## Run-dir layout

```
<run_dir>/
  prep/
    status.json
    <stem>.pdbt                  # prepared receptor, input stem preserved
    <stem>_autobox.pdbt          # autobox reference (autobox only)
    ligands/<stem>.pdbt          # one file per ligand, flat
    prep.log                     # prepare's own log
  dock/
    status.json
    attempt-<N>/                 # fresh dir per run — never reuses vinardock's
      config.txt                 #   mtime cache. The fully merged config
      vinardock.log              #   vinardock ran. N = next free counter.
      DOCK/
        <file>.pdbt                     # single-ligand runs (flat)
        <2-char>/<stem>/<file>.pdbt     # batch runs (>1 ligand → VS mode)
        <receptor>_modified.pdbt        # flex runs only
        log.csv
  analysis/
    status.json
    <name>_pose.pdbt / <name>_ligand.pdb / <name>_receptor.pdb
    <name>_complex.pdb
    <name>_report.xml / <name>_report.txt / <name>_plip.log
    # PLIP intermediates (<name>_complex_protonated.pdb, plipfixed.*)
    # are deleted after the PLIP workers finish
  report.md            # written by analyse, header = provenance block
```

Input files are **not copied** into the run dir; stage scripts read them
in place. A stage may only write inside its own folder (`prep/`,
`dock/`, `analysis/`); `report.md` at the root is analyse's exception.

## status.json

Written **atomically** (`status.json.tmp` then `os.replace`).

```json
{
  "stage": "prepare|dock|analyse",
  "status": "ok|failed",
  "started": "ISO-8601",
  "finished": "ISO-8601",
  "artifacts": ["<path relative to run_dir>"],
  "metrics": {},
  "warnings": [],
  "error": null
}
```

Exit codes (all subcommands): `0` = ok, `1` = stage/run failure
(status.json still written), `2` = usage error, `3` = internal error.

### Per-stage metrics

**prepare** — `n_ligands`, `ligands` (sorted names), `receptor_chains`,
`residues_kept` / `residues_dropped` ({resname: count} for every residue
other than the 20 standard amino acids — what the user must be told was
retained vs stripped; counts are distinct chain+resseq residues, ATOM
and HETATM alike), `ligand_from_receptor`
(stem of the extracted co-crystal ligand, or null), `receptor_file` /
`autobox_file` (filenames in `prep/`).

**dock** — `recipe` (recipe name), `config` (`--config` file stem or
null), `seed`, `threads`, `resolved_box`
({center, size} — scraped from vinardock's `Search box:` log line when
it emits one (autobox), else computed from the reference bounding box +
pad or the manual flags), `output_dir` and `vinardock_log` (paths in the
current attempt), `receptor_file`, `receptor_path`, `per_ligand`
({name: {energies,
best, nconfs, torsdof, rmsd} — or {status: 'failed', error} for a
ligand that failed; plus `dG` or `reference_state` in those modes}).
`best` is the log.csv score cross-checked against `min(energies)`.

**analyse** — `analyzed_pose` ({name: "k of N"}), `interactions`
({name: counts per category}), `pose`, `report`.

## Gates and resume

The coordinator's only check is status + filesystem state — there is no
hashing anywhere:

- A stage is **skipped** on re-run iff its `status.json` says `ok`,
  every listed artifact exists and is non-empty, and `started >=` its
  predecessor's `finished` — timestamps are parsed as ISO-8601
  (a `Z` suffix is tolerated) and compared as datetimes, not strings.
  A re-run predecessor makes downstream stages stale automatically.
  `--force` re-runs everything.
- After a stage runs, `status.json` must say `ok`. Otherwise stop and
  report `error` + the log tail (`prep/prep.log`, or the newest
  `dock/attempt-*/vinardock.log`).
- A stage script may exit 0 with `status: "failed"` — trust the file.
  Conversely exit ≠0 without a status.json is a usage/crash error.
- Changed inputs, recipe, config flags, or tools are **not** detected —
  use a new run dir (or `--force`) when anything upstream changed.

## Validation (this is where failures are caught)

Corruption/bad output is caught by *structural* checks, not hashes:

- prepare: receptor must contain ATOM records; every prepared ligand
  must contain atoms and a `TORSDOF` record; 2D inputs get `--gen3d`.
  The autobox reference follows the same rules and must be exactly
  one molecule (`.pdbt`/`.smi`/`.sdf`/`.mol2`/`.pdb`); SMILES
  references always get `--gen3d`. A HETATM group carrying the
  amino-acid N/CA/C + O backbone is a modified residue (the polymer,
  not a heterogen): it is never stripped under `--drop_hetatm` and
  never appears as a `scan`/`--ligand_from_receptor` candidate.
- dock: `log.csv` header recognized, score column matched by content
  (`vinardo score …`, `E_corrected`, `binding_energy`, …) and
  cross-checked against pdbt model energies (fail on divergence
  > 0.05 kcal/mol), per-ligand conformer count must match parsed
  REMARK 980 energies, autobox reference with zero coordinate extent
  is fatal (degenerate input would otherwise produce a bogus box),
  mutation-dg must produce a `E_WT/E_MUT/dG` table — a normal
  `nConfs` table means vinardock ignored `--calc_mutate_dG` and the
  stage fails closed.
- analyse: PLIP runs per ligand; a ligand whose analysis fails is a
  warning, the stage fails only if *all* analyses fail. `report.md`
  is deleted before the stage runs so a stale report can never sit
  next to a failed status.
- analyse XML: PLIP's XML must contain exactly one bindingsite
  matching the target ligand (hetid/chain/position) — PLIP's exit
  code is not trusted. The site lives under `bindingsites` with an
  `identifiers` element (`hetid`/`chain`/`position`); its
  `interactions` block lists `hydrophobic_interactions`,
  `hydrogen_bonds`, `salt_bridges`, `pi_stacks`,
  `pi_cation_interactions`, `halogen_bonds`, `water_bridges`,
  `metal_complexes`, each child's `resnr`/`restype`/`reschain`
  naming the contacting residue. A missing list = zero
  interactions; zero sites or no site matching the ligand = failed
  analysis.
- Sanity warnings (never fatal): best score > 0, pose centroid outside
  the resolved box, zero PLIP interactions for a ligand.

## Partial failures

A ligand that fails (zero conformers, missing output, N/A dG) is
recorded in `per_ligand` with `status: 'failed'` and listed in
`warnings` — the stage still reports `ok` if at least one ligand
succeeded, and report.md lists failures explicitly. The same applies
to analyse: a failed PLIP run appears in a "Failed ligand analyses"
report section and only an all-ligand failure fails the stage.
Structural log.csv problems (unknown header, duplicate rows) are
still fatal.
