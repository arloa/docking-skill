# Interview

Follow this when SKILL.md step 2 routes here. Collect everything in ONE
question pass — scripts never ask questions.

## Questions

1. **Receptor** — path to a `.pdb` (or `.pdbt`) file. Trust the extension;
   do not open the file to inspect it.
   - **ASK**: strip receptor HETATM records (waters, ions, co-crystal
     ligands)? (`workflow.py --drop-hetatm` strips all HETATM.) Ask the
     question directly — do not read the receptor to enumerate the
     residues. If HETATM are kept, `prepare.py` reports the residue names
     it found in its warning/metrics.
2. **Ligands** — either:
   - structure files (`.pdbt`, `.sdf`, `.mol2`, `.pdb`), or
   - SMILES — create a `.smi` input file outside the run directory,
     `SMILES<space>name` per line; `workflow.py` reads it in place (inputs
     are never copied into the run directory).
   - 2D structures are handled (prepare runs `--gen3d`); duplicate
     ligand names are a hard error.
3. **Box** — pick one:
   - manual: `center_x center_y center_z size_x size_y size_z`
   - autobox: a reference ligand file (usually the co-crystal ligand or
     one of the docking ligands) + pad in Å → `--autobox {pad}` +
     `--autobox_ligand {ref}`.
   - The resolved box is read back from the vinardock log and recorded.
4. **Run dir** — where results go.
5. **conformations** — poses to write (default 9).
6. **seed** — offer a generated random seed (reproducible by being
   recorded); user may override. Never run without a seed.
7. **threads** — default `nproc`. Note: batch VS parallelizes across
   ligands only when `n_ligands >= threads`; smaller batches use inner
   per-particle parallelism.
8. **recipe** — see table below. Confirm or let the user pick.
9. **Recipe slots** — whatever the chosen recipe's `# asks:`/`# requires:`
   declares (e.g. `flexres` for flexible, `mutation` for mutation-dg).

## Recipe selection table (deterministic)

| User intent (keywords) | Recipe |
|---|---|
| flexible, flexres, sidechain | `flexible` |
| screen, library, batch, many ligands, VS | `screening` |
| deep, thorough, intense, exhaustive | `deep-search` |
| mutation, mutate, dG, ΔG | `mutation-dg` |
| rescore, minimize, score-only | `rescore` |
| anything else / ambiguous | `standard` |

No matching intent → `standard`, and list the catalog so the user can
override. If multiple recipe intents are present (e.g. flexible + deep),
ask which protocol to use or request a combined template; do not silently
pick one. A `.conf` path is also accepted (`workflow.py --recipe <path>`).
The `deep-search` recipe is unavailable until the user supplies tuned values.

## Slot rules

- Fill only the slots the recipe declares. If a required slot
  (`# requires:`) has no value and no default, ask the user — never
  invent one.
- Pass slot values to workflow.py as `--slot name=value`, explicit config
  flags as `--set flag=value` (box coordinates go through `--set`).
  `--autobox-ligand <file>` hashes the reference input in place.
