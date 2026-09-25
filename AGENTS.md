# docking skill

Reproducible Vinardock docking pipeline (prepare → dock → analyse). No
build step; scripts are plain Python 3 stdlib. Requires Python ≥ 3.9
(`Path.is_relative_to`) and Linux x86_64 — the release assets in
`setup.py` are linux-amd64 only.

## Verify

- `python3 -m unittest discover -s tests -v` — unit tests for the recipe
  parser, stage gates, probe and installer selection logic.

## Conventions

- `scripts/setup.py` pins every release asset by sha256 in `CHECKSUMS`;
  `ASSETS` maps each tool to (modern asset, glibc fallback asset, minimum
  glibc). Downloads try the modern asset first on new glibc and the
  `-ubuntu22.04` fallback on old glibc, but always keep the first asset
  that passes the launch smoke test.
- Inputs are referenced **in place**: `workflow.py` records each input's
  absolute path + sha256 in `manifest.json.inputs` and never copies them
  into the run dir. There is no `inputs/` directory.
- The coordinator must not inspect input file contents before a run —
  trust the extension. Only after a stage (e.g. obabel) fails, report the
  error and ask the user before inspecting/fixing the file.
- `prepare.py` names prepared files after the input stem: receptor
  `<stem>.pdbt`, autobox reference `<stem>_autobox.pdbt`, ligands
  `ligands/<stem>.pdbt`. It normalizes the obabel `REMARK  Name =` line to
  the original input path so prepared artifacts are byte-reproducible
  across run dirs (the conversion uses a random temp dir otherwise).
- `probe` never silently drops a binary that exists but cannot launch —
  it reports it under `unusable` with the error.
- Update `references/setup.md` and `references/gotchas.md` alongside any
  change to probe/install behaviour.
