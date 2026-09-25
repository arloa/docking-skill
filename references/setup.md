# Tool bootstrap: probe → ask → install

Follow this when SKILL.md step 1 routes here. Resolve `<skill_dir>` from the
location of the invoked SKILL.md; do not change the caller's working directory,
as `probe` deliberately inspects that directory.

## 1. Probe

```bash
python3 "<skill_dir>/scripts/setup.py" probe
```

Prints JSON per tool: `{found, path, sha256, build_id, version_line}`.
A binary that exists but cannot launch is **not** silently dropped: it
is listed under `unusable` as `{path, error}` (e.g. a build requiring a
newer glibc). Show those to the user — "not found" for a tool that is
actually present usually means an unusable build.
Search scope is deliberately narrow — **PATH and the literal current
working directory (`pwd`) only**, plus the existing tools dir:

- `vinardock` — PATH lookup, `<pwd>/vinardock`, `tools/bin/vinardock`
- `obabel-vinardock` — PATH lookup, `<pwd>/obabel-vinardock`,
  `tools/bin/obabel-vinardock` (bare `obabel` is NEVER probed or used)
- `param` — `<pwd>/param/` and `tools/param/` containing at least
  `param.dat` + `param.TxT.dat`
- `plip` — `tools/plip-venv/bin/plip`

It does not scan the filesystem and never guesses directories
(`./bin`, `~/Calculos`, etc. are out of scope by design). A binary that
lives elsewhere is simply "not found" — point it out explicitly with
`setup.py install --vinardock /abs/path` if needed.

## 2. Ask (coordinator)

Show the probe result and ask the user **one question**: use the local
tools found, or download the pinned release? Defaults: local when all
three (vinardock, obabel-vinardock, param) were found; download
otherwise. Also offer per-tool mixing (e.g. local vinardock + downloaded
obabel-vinardock) via explicit paths.

If `plip-venv` is missing it is always created — there is no system PLIP
to reuse.

## 3. Install

```bash
python3 "<skill_dir>/scripts/setup.py" install \
    --vinardock <abs-path|download> \
    --obabel-vinardock <abs-path|download> \
    [--param <abs-dir|download>] [--skip-plip]
```

- `local` (a path) → copied into `~/.local/share/docking-tools/bin/`
  (copied, not symlinked — freezes the artifact) and re-hashed.
- `download` → release `v1.0.0` assets from
  `github.com/arloa/Vinardock-exec`, verified against `sha256sums.txt`.
  Each tool has a modern build and an `-ubuntu22.04` build; the modern
  one is tried first and the fallback is selected when glibc is older
  than the build's minimum (`vinardock` 2.39, `obabel-vinardock` 2.38).
  Selection is not decided by glibc alone: whichever asset is chosen
  must pass the launch smoke test, and the script falls back to the
  other asset if it does not (the `obabel-vinardock` ubuntu22.04 build
  is dynamically linked against `libopenbabel.so.7`, so it only works
  where Open Babel 3.1.1 is installed).
  `param/` is fetched from pinned source commit `11caaa8` and each file is checked against a built-in sha256: the `v1.0.0` tag itself predates the parameter files.
- PLIP → `uv venv --python <invoking Python> <tools_dir>/plip-venv` then
  install pinned PLIP and dependencies (including the self-contained `openbabel` 3.2.1
  wheel — note the version split vs the obabel-vinardock CLI; it is
  recorded in the manifest/report as a known caveat).
- Both executables are smoke-tested; a binary that cannot launch is rejected
  even when its checksum is correct. For `download`, the smoke test is what
  picks the working asset (modern vs `-ubuntu22.04`) — if neither launches,
  the install fails instead of leaving a broken tool in place. On hosts
  where `obabel-vinardock-linux-amd64-ubuntu22.04` is selected but
  `libopenbabel.so.7` is missing, use an explicitly approved compatible
  local build or request a portable release.
- A subsequent replacement of an installed binary requires an explicit
  `--replace` flag, after the user approves the selected source.
- Regenerates `references/*-help.txt` from the resolved binaries;
  `vinardock-help.txt` replaces only the changing timestamp seed default
  with `<timestamp>` so regenerated help can be compared across runs.

Prints a JSON summary of what was installed — the coordinator merges it
into `manifest.json.tools`.
