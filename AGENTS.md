# docking skill

Vinardock docking pipeline (prepare → dock → analyse) packaged as an
agent skill. No build step; scripts are plain Python 3 stdlib. Requires
Python ≥ 3.9 (`Path.is_relative_to`) and Linux x86_64 — the release
assets in `setup.py` are linux-amd64 only.

## Verify

- `python3 -m unittest discover -s tests -v` — unit tests for the recipe
  parser, log.csv/log parsing, PLIP XML matching, resume-skip logic,
  probe and installer selection logic.
- `IntegrationTests` run the *real* binaries end-to-end when
  `~/.local/share/docking-tools` is populated (skipped otherwise) and
  assert actual output formats — log.csv headers, REMARK 980 lines, the
  DOCK/ tree. Unit-test fixtures are fabricated, so any change to
  parsing code should be confirmed against the integration test.

## Conventions

- `scripts/pipeline.py` is the single entry point:
  `prepare | dock | analyse` stages plus the `workflow` coordinator.
  Shared helpers live once at the top of the file.
- `bin/obabel-vinardock` is a **fully static** Open Babel 3.1.1 build
  shipped in the repo — needs no host libraries. `setup.py install`
  defaults to it and `pipeline.py` prefers it over
  `<tools_dir>/bin/obabel-vinardock`.
- `scripts/setup.py` pins every release asset by sha256 in `CHECKSUMS`;
  `ASSETS` maps each tool to (modern asset, glibc fallback asset, minimum
  glibc). Downloads try the modern asset first on new glibc and the
  `-ubuntu22.04` fallback on old glibc, but always keep the first asset
  that passes the launch smoke test.
- **No hashing for verification.** status.json lists artifact *paths*
  only; resume checks are status + existence + timestamp staleness
  (a stage is stale when its predecessor finished after it started).
  Inputs are referenced in place by path — nothing copies or hashes them,
  and edits to inputs are not detected on resume (use a new run dir).
  Checksums in `setup.py` exist only to verify downloads, not to track
  provenance.
- Validation is structural, not byte-level: pdbt files must contain
  atoms + `TORSDOF`, log.csv headers are matched by name, PLIP output is
  verified by binding-site identity. Prefer adding a structural check
  over adding a hash.
- `prepare` names prepared files after the input stem: receptor
  `<stem>.pdbt`, autobox reference `<stem>_autobox.pdbt`, ligands
  `ligands/<stem>.pdbt`. It deletes the obabel `REMARK  Name =` line so
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
