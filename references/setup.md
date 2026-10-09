# Tool bootstrap: probe → ask → install

Follow this when SKILL.md step 1 routes here. Resolve `<skill_dir>` from the
location of the invoked SKILL.md; do not change the caller's working directory,
as `probe` deliberately inspects that directory.

## 1. Probe

```bash
python3 "<skill_dir>/scripts/setup.py" probe
```

Prints JSON per tool: `{path, version_line}` for launchable binaries;
binaries that exist but cannot launch are listed under `unusable` with
the error — "not found" for a present tool usually means an unusable
build. Search scope is deliberately narrow — **PATH, the literal pwd,
and the existing tools dir only**:

- `vinardock`, `obabel-vinardock` — the repo's own `bin/<name>` first
  (it ships fully static builds of both), then PATH,
  `<pwd>/<name>`, `tools/bin/<name>` (bare `obabel` is NEVER probed
  or used)
- `param` — `<pwd>/param/` and `tools/param/` (needs `param.dat` +
  `param.TxT.dat` minimum)
- `plip` — `tools/plip-venv/bin/plip`
- `launcher` — resolved path of `vinardock-pipeline` on PATH, or `null`

No filesystem scanning, no guessed directories — a binary elsewhere is
"not found"; pass it explicitly via `setup.py install --vinardock /abs/path`.

## 2. Ask (coordinator)

Show the probe result. `vinardock` and `obabel-vinardock` need no
asking — the repo ships fully static builds (`bin/`, no host
libraries needed at all) and install defaults to them; explicit paths
still override. The remaining question is `param/` (download the
pinned files, or a local `param/` dir) and PLIP (always the venv).

If `plip-venv` is missing it is always created — there is no system PLIP
to reuse.

## 3. Install

```bash
python3 "<skill_dir>/scripts/setup.py" install \
    [--vinardock <abs-path|download>] \
    [--obabel-vinardock <abs-path|download>] \
    [--param <abs-dir|download>] [--skip-plip] [--launcher]
```

`--vinardock`/`--obabel-vinardock` default to the repo's bundled
static builds (`bin/`, built for ubuntu 22.04 — static, so they run
anywhere regardless of host glibc) — pass either only to override.

- `local` (a path) → copied into `~/.local/share/docking-tools/bin/`
  (copied, not symlinked — freezes the artifact).
- `download` → release `v1.0.0` assets from
  `github.com/arloa/Vinardock-exec`, verified against the sha256 pinned
  in `CHECKSUMS`. Each tool has a modern build and an `-ubuntu22.04`
  fallback; glibc picks the candidate order (`vinardock` needs ≥2.39,
  `obabel-vinardock` ≥2.38), and the launch smoke test picks the winner
  — if neither launches, install fails rather than leaving a broken
  tool. `param/` is fetched from pinned commit `11caaa8`
  with per-file sha256 (the `v1.0.0` tag predates the parameter files).
- PLIP → `uv venv --python <invoking Python> <tools_dir>/plip-venv` +
  pinned deps including the self-contained `openbabel` 3.2.1 wheel —
  a different Open Babel than the obabel-vinardock CLI uses; noted in
  report.md's provenance.
- Replacing an installed binary needs explicit `--replace` after the
  user approves the source.
- Launcher: after installing, an interactive run offers to write a
  `vinardock-pipeline` wrapper (`exec <install-python> <pipeline.py>
  "$@"`) into `~/.local/bin` when it is on PATH, else `/usr/local/bin`
  when writable; otherwise `~/.local/bin` with a warning that it is
  not on PATH. Arbitrary writable PATH dirs are never picked. A
  wrapper rather than a symlink because it needs no exec bit on
  `pipeline.py` and pins the interpreter that ran install. An existing
  PATH entry pointing at the same script is detected first
  (`shutil.which`), so re-runs are a no-op; a second entry earlier on
  PATH is reported as shadowing.
  Non-interactive runs never prompt — pass `--launcher` to create it
  (after the user approves). An existing link to the same script is a
  no-op; a different file needs `--replace`.
- `references/*-help.txt` regenerate only for **local** installs —
  the checked-in files already document the pinned release builds.
  `vinardock-help.txt` masks the timestamp seed default as `<timestamp>`
  so help diffs compare across runs.

Prints a JSON summary of what was installed (`{path, version_line,
source}` per tool, plus param files and PLIP versions) and writes the
same to `<tools>/install.json`; `analyse` quotes it in report.md's
provenance block.
