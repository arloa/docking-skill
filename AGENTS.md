# docking skill

Vinardock docking pipeline (prepare → dock → analyse) packaged as an
agent skill. No build step; scripts are plain Python 3 stdlib. Requires
Python ≥ 3.9 (`Path.is_relative_to`) and Linux x86_64 — `bin/` ships
linux-amd64 static binaries.

## Verify

- `python3 -m unittest discover -s tests -v` — unit tests for the recipe
  parser, log.csv/log parsing, PLIP XML matching, resume-skip logic,
  probe and installer selection logic.
- `IntegrationTests` run the *real* binaries end-to-end when
  `~/.local/share/vinardock-tools` is populated (skipped otherwise) and
  assert actual output formats — log.csv headers, REMARK 980 lines, the
  DOCK/ tree. Unit-test fixtures are fabricated, so any change to
  parsing code should be confirmed against the integration test.

## Conventions

- `scripts/pipeline.py` is the single entry point:
  `prepare | dock | analyse` stages plus the `workflow` coordinator.
  Shared helpers live once at the top of the file.
- `bin/` ships **fully static** builds of both executables —
  `obabel-vinardock` (Open Babel 3.1.1) and `vinardock` (built from
  `~/Calculos/Vinardock` with the Makefile flags minus
  `-march=native`, linked `-static`). Both: `ldd` → `not a dynamic
  executable`, no glibc or host-lib requirement; built for
  ubuntu 22.04 but runs anywhere x86_64 Linux. `setup.py install`
  defaults to them and `pipeline.py` prefers them over
  `<tools_dir>/bin/`.
- `scripts/setup.py install` copies from `bin/` and `param/` (or
  explicit dirs) — there is no download path at all; the only network
  step is provisioning the PLIP venv (`uv`).
- **No hashing for verification.** status.json lists artifact *paths*
  only; resume checks are status + existence + timestamp staleness
  (a stage is stale when its predecessor finished after it started).
  Inputs are referenced in place by path — nothing copies or hashes them,
  and edits to inputs are not detected on resume (use a new run dir).
- Validation is structural, not byte-level: pdbt files must contain
  atoms + `TORSDOF`, log.csv headers are matched by name, PLIP output is
  verified by binding-site identity. Prefer adding a structural check
  over adding a hash.
- `modified_residues` detects a modified amino acid (MSE, SEP, TPO, ...)
  marked HETATM by its N/CA/C + O backbone (CA must be a carbon element),
  so such residues are never stripped under `--drop_hetatm` and never
  offered by `hetatm_groups` (scan / `--ligand_from_receptor`). It is a
  structural check, not a residue-name list — nothing to keep in sync.
- `prepare` always reports the receptor's non-standard residues (every
  residue but the 20 standard amino acids, ATOM or HETATM alike) as
  `Receptor non-standard residues kept/dropped` warnings plus
  `metrics.residues_kept`/`residues_dropped` ({resname: count}).
- `prepare` names prepared files after the input stem: receptor
  `<stem>.pdbt`, autobox reference `<stem>_autobox.pdbt`, ligands
  `ligands/<stem>.pdbt`. `--ligand_from_receptor` extracts a co-crystal
  HETATM group as the ligand (mutually exclusive with
  `--prepare_ligand`), always removes it from the receptor, and also
  writes it as `<stem>_autobox.pdbt` so `--autobox` boxes the original
  binding site. It deletes the obabel `REMARK  Name =` line so
  prepared artifacts depend only on input content (the conversion runs
  in a random temp dir otherwise).
- The coordinator must not inspect input file contents before a run —
  trust the extension. Only after a stage (e.g. obabel) fails, report the
  error and ask the user before inspecting/fixing the file.
- `probe` never silently drops a binary that exists but cannot launch —
  it reports it under `unusable` with the error.
- Update `references/setup.md` and `references/gotchas.md` alongside any
  change to probe/install behaviour; update `references/contract.md`
  alongside any change to the status.json schema or run-dir layout.
