# Docking pitfalls

- Batch outputs live at `dock/DOCK/<first two stem chars>/<stem>/<filename>.pdbt`; single ligand outputs are flat. Input order is alphabetical.
- `log.csv` has one row per ligand (best score only). Rigid rows include an RMSD column absent from their header; other modes use different headers. A zero-conformer row signals a ligand failure even if vinardock exits 0.
- Read every MODEL's REMARK 980 in each output pdbt for conformer energies. `--conformations` is a maximum, not a guaranteed count.
- Vinardock caches existing outputs based on modification times, not content. Each stage rerun uses a new `dock/attempts/<signature>[-N]/DOCK`; workflow.py requires a new run dir when recipe, config, seed, binary, parameter, or original input changes.
- The default seed is a timestamp; always set it. The default threads count depends on the machine; set that too.
- The rotamer library default is relative to the config directory; use an absolute path for mutation runs.
- Use `obabel-vinardock` exclusively for CLI conversion; PLIP uses Python `openbabel` bindings separately, possibly of a different version.
- Do not interpret a PLIP zero exit code as a successful ligand analysis: validate an expected ligand binding site in XML.
- On flex runs, use the modified receptor output for the complex instead of the unmodified prep receptor when available.
- Probe searches only PATH and the invocation's working directory (plus the existing tools install target), never guessed source directories. Binaries that exist but cannot launch are listed under `unusable` with the error — check it before concluding a tool is absent.
- Receptor HETATM records may represent essential cofactors/metal ions or irrelevant waters/co-crystal ligands; ask before stripping, never silently discard them. Do not pre-read the receptor to list them — ask directly; prepare reports the residue names it found.
