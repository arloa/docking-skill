# Recipe file format

A recipe is a config-fragment template in vinardock's own config
grammar. `run.py` instantiates it deterministically — there is no
templating language, only the rules below.

```text
# recipes/flexible.conf
# recipe: flexible
# description: flexible sidechains (autoflex or explicit residues)
# requires: box
# asks: flexres (optional — explicit residues replace autoflex)
--flexres.autoflex {autoflex_pad}       # if-unset: flexres
--flexres.res {flexres}                 # if-set:   flexres
--interpolation tricubic
```

## Grammar

- **Flag lines** — `--name value` or bare `--name` (implicit bool).
  `#`/`;` start comments anywhere.
- **Metadata comments** — `# recipe: <name>`, `# description: …`,
  `# requires: <capability>`, `# asks: <slot> (note)`. Advisory except
  `requires: box`, which run.py enforces.
- **`{slot}`** — substituted from `--slot name=value` or built-ins
  (`tools_dir`, `param_dir`, `run_dir`, `prep_dir`, `dock_dir`, `seed`,
  `threads`). `{slot:default}` uses the default when unset. A `{slot}`
  with no value and no default on an emitted line → hard error naming
  the slot.
- **Line guards** — a trailing `# if-set: <slot>` emits the line only
  when the slot has a value; `# if-unset: <slot>` only when it doesn't.
  Use these for mutually exclusive alternatives.

## Merge & precedence

1. Base flags (paths, tables, out, seed, threads, `--calc_lig_rmsd`).
2. Recipe lines.
3. Explicit `--set` flags.

Later wins by flag name — each flag is emitted once. Overridden recipe
lines are reported in `warnings`. `run.py` also enforces
`requires: box` (center_x/y/z + size_x/y/z, or `autobox` +
`autobox_ligand`).

## Built-in slots

| Slot | Value |
|---|---|
| `tools_dir` | tools dir (default `~/.local/share/docking-tools`) |
| `param_dir` | `<tools_dir>/param` |
| `run_dir` `prep_dir` `dock_dir` | run-dir stage paths |
| `seed` `threads` | from `--seed`/`--threads` |

## Catalog (this repo)

| File | Intent |
|---|---|
| `standard.conf` | default rigid docking (pso_mc, tricubic) |
| `screening.conf` | `--vsmode` VS-tuned swarm defaults |
| `deep-search.conf` | placeholder, fails closed until user supplies tuned settings |
| `flexible.conf` | explicit `--flexres.res` or user-provided `--set=flexres.autoflex=<cutoff>` |
| `mutation-dg.conf` | `--calc_mutate_dG` + one mutation + rotamer lib |
| `rescore.conf` | `--minimize` local polish of a pose |
