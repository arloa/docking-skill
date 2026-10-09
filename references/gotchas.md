# Docking pitfalls

- Batch outputs live at `dock/attempt-<N>/DOCK/<first two stem chars>/<stem>/<filename>.pdbt`; single ligand outputs are flat under `DOCK/`. Input order is alphabetical.
- `log.csv` has one row per ligand (best score only). The score column is matched by content — the real header is `vinardo score (kcal/mol)`; reference-state runs report `binding_energy`/`E_corrected` and the corrected value is preferred. RMSD is read by name when the header has an `rmsd` column, with the legacy trailing-column heuristic only as fallback. A zero-conformer row signals a ligand failure even if vinardock exits 0 — it is recorded per ligand, not fatal to the batch.
- Read every MODEL's REMARK 980 in each output pdbt for conformer energies. `--conformations` is a maximum, not a guaranteed count.
- Vinardock caches existing outputs based on modification times, not content. Each run uses a new `dock/attempt-<N>` directory; the old attempts' config.txt (the fully merged config vinardock ran) and vinardock.log are kept next to their `DOCK/` output so every attempt stays self-describing.
- Vinardock's default seed is a timestamp — pass `--seed <n>` (or set it in a recipe/config) for reproducibility. When omitted, the pipeline recovers the seed vinardock actually used from its log into `metrics.seed`. Threads default to `nproc`; pass `--threads <n>` to override.
- Unknown `--flag value` tokens on `dock`/`workflow` go to vinardock verbatim — a typo'd pipeline flag (`--run_dirr`) surfaces as vinardock's `Option 'run_dirr' does not exist`. `--key=value` is not vinardock syntax and is rejected; a bare `--flag` is a bool switch. Precedence is CLI > `--config` file > recipe > script defaults; overriding a generated path key (`receptor`, `ligand`, `out`, `autobox_ligand`, `scoring*`) warns in `status.json` because it can desync the pipeline (e.g. poses docked on a receptor different from the one PLIP analyses). `dock` alone needs `prep/` ok or a merged config that supplies `receptor`/`ligand` paths — otherwise it fails naming the fix.
- `rotamer_lib` is a pipeline default pointing at `<tools_dir>/param/dun2010bbdep.bin` — vinardock's own default is relative (`flexres/dun2010bbdep.bin`) and would resolve inside the attempt dir. A `--config` file may still override it.
- Recipes and `--config` files are plain vinardock config fragments — `--key value` lines and `#` comments, no templating. Recipe-specific values (e.g. a mutation spec) are passed verbatim: `--flexres.res A:S63T`. The old `--slot name=value` flag is gone — typed verbatim it reaches vinardock as an unknown option and fails loudly.
- Autobox references must be exactly one molecule and genuinely 3D: `.smi` gets `--gen3d`, multi-molecule SDF/Mol2 is rejected, planar input is regenerated in 3D, and a reference whose coordinate extent is ~0 fails the dock stage — padding a degenerate reference would otherwise yield a plausible-but-wrong box.
- `vinardock-pipeline` is a generated `sh` wrapper (not a symlink —
  `pipeline.py` needs no exec bit, and the wrapper pins the interpreter
  that ran `setup.py install`, which may differ from PATH's `python3`).
  Written by `install --launcher` or its interactive prompt into
  `~/.local/bin` if on PATH, else `/usr/local/bin` if writable —
  arbitrary writable PATH dirs are never used. `probe`'s `launcher`
  field is the resolved path or `null` (call `pipeline.py` by path).
  If the skill dir moves the wrapper dangles — re-run
  `install --launcher --replace`.
- Vinardock prints `Search box: center = …, size = …` only when it resolves the box itself (autobox); the pipeline records that value in `resolved_box` when present, and otherwise computes center/size from the autobox reference bounding box + pad (or the manual flags). Note the printed autobox can differ slightly from the computed estimate.
- mutation-dg fails closed on purpose: vinardock silently degrades to rigid docking when the mutation spec is unusable (watch for `[FLEX] ... ignored` in vinardock.log), so the pipeline requires exactly one ligand, a `chain:OLDresNEW` spec with `OLD != NEW`, and a `E_WT/E_MUT/dG` log.csv — a normal `nConfs` table is a hard failure.
- `rescore` (`--score_only`) evaluates the ligand *as supplied* — it needs an already-posed ligand inside the box (e.g. a `.pdbt` from a previous dock run). A freshly `--gen3d`-generated structure placed arbitrarily is not a docked pose; rescoring it is almost always a box-outside failure or a meaningless number.
- Inputs are not re-checked on resume: editing a receptor/ligand file in place is not detected. Use a new run dir (or `--force`) after changing inputs, recipe, or settings.
- Use `obabel-vinardock` exclusively for CLI conversion; PLIP uses Python `openbabel` bindings separately, possibly of a different version.
- Prefer a fully static `obabel-vinardock` build (`ldd` must print `not a dynamic executable`); the downloaded `-ubuntu22.04` fallback needs system `libopenbabel.so.7` and fails the launch test without it. Install a static build with `--obabel-vinardock <path>`.
- Do not interpret a PLIP zero exit code as a successful ligand analysis: validate an expected ligand binding site in XML.
- On flex runs, use the modified receptor output for the complex instead of the unmodified prep receptor when available.
- Probe searches only PATH and the invocation's working directory (plus the existing tools install target), never guessed source directories. Binaries that exist but cannot launch are listed under `unusable` with the error — check it before concluding a tool is absent.
- Receptor HETATM records may represent essential cofactors/metal ions or irrelevant waters/co-crystal ligands; ask before stripping, never silently discard them. Do not pre-read the receptor to list them — ask directly; prepare reports the residue names it found.
- Blank-chain receptor records are reported as chain `(blank)` in `receptor_chains` — a reporting placeholder, not a real chain ID; do not use it in flexres/mutation specs.


