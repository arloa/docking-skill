# Recipe file format

A recipe is a plain vinardock config fragment — the same `--key value`
grammar vinardock itself parses, `#` comments allowed. **No templating**:
a recipe is exactly what it says, and anything it doesn't cover is
passed verbatim on the command line.

```text
# recipes/mutation-dg.conf
# recipe: mutation-dg
# description: mutation binding-energy comparison
--calc_mutate_dG
```

`# recipe:`/`# description:` comments are advisory documentation for
whoever reads the file; the pipeline only parses flag lines.

## Merge & precedence

Vinardock's own convention — later layers win. `dock` is a consolidation
hub: four layers merged once into `config.txt` (the file vinardock runs):

1. Script defaults — generated paths (`receptor`, `ligand`, `out`,
   scoring tables, `rotamer_lib`), `threads`, `conformations 9`,
   `--calc_lig_rmsd`. `receptor`/`ligand` default to the prep outputs
   when `prep/` is ok.
2. Recipe — `--recipe <name|path>` (default `standard`).
3. User config — `--config <path>`, same grammar as a recipe.
4. CLI flags — any `--key value` on `dock`/`workflow` that isn't a
   pipeline flag goes to vinardock verbatim.

Relative path values in a `--config` file resolve against the config's
own directory (write them as if running vinardock from there); on the
command line they resolve against the caller's cwd. This matters
because vinardock itself runs inside `dock/attempt-N/`.

Overriding a generated path key (`receptor`, `ligand`, `out`,
`autobox_ligand`, `scoring*`, `rotamer_lib`) warns — it can desync
prep/analysis (poses docked on a different receptor than the PLIP
complex). All other overrides are silent, by design. `seed` is
optional — vinardock defaults to a timestamp and the pipeline records
the used seed in `metrics.seed`.

`dock` runs without a prior `prepare` when the merged config supplies
existing `receptor` and `ligand` paths (prepared `.pdbt`s); otherwise
it fails naming the fix. It requires a box — `center_x/y/z` +
`size_x/y/z`, or `autobox` + a reference via `--prepare_autobox_ligand`
or a prepared `--autobox_ligand` path — and `out` must live inside
the run dir.

## Catalog (this repo)

One line per file — the user-intent → recipe mapping lives in
`interview.md`, each recipe's own `# description:` is canonical.

- `standard.conf` — explicit `--docking_mode pso_mc` (vinardock's own
  default; exists so runs record a named protocol)
- `screening.conf` — `--vsmode` (virtual-screening swarm defaults)
- `rescore.conf` — `--score_only`
- `flexible.conf` — comment-only: names the intent and the gate; you
  must pass `--flexres.res <spec>` or `--flexres.autoflex <Å>`
- `mutation-dg.conf` — `--calc_mutate_dG`; you must pass
  `--flexres.res <chain:OLDresNEW>` (e.g. `A:S63T`, `OLD != NEW`)

Per-recipe constraints worth knowing: `flexible` fails without a
`flexres.*` flag; `mutation-dg` needs one ligand + a real
`chain:OLDresNEW` spec and fails closed when the mode doesn't engage;
`rescore` needs an already-posed ligand inside the box.
