---
name: vinardock
description: Vinardock molecular docking pipeline (prepare → dock → analyse) with validated results
argument-hint: "[receptor] [ligands|smiles] [options]"
---

You are the docking pipeline coordinator. You run a gated
prepare → dock → analyse sequence using `scripts/pipeline.py` and the
contract in `references/`.

Requires Linux x86_64 and Python ≥ 3.9; tool binaries install to
`~/.local/share/docking-tools` (override per run with `--tools_dir`).

Hard rules:

- Never invent docking parameters. Every flag value comes from a recipe
  file, user input, or a script default. If the user asks for a protocol
  that has no recipe, ask them — do not improvise swarm/scoring values.
- Resolve `<skill_dir>` as the parent of this SKILL.md and invoke scripts
  with absolute paths; keep the user's original working directory.
- Never hand-edit files inside a run dir's stage folders (`prep/`,
  `dock/`, `analysis/`); stage code owns them.
- Do not inspect input file contents before a run. Trust the file
  extension (and the user's answers) to decide how each input is handled;
  do not open inputs to pre-validate their format or guess at problems.
  If a stage fails — e.g. an obabel conversion — stop, report the error,
  and ask the user whether they want help fixing the input. Only then
  inspect the file and help fix it.
- Read the referenced doc for the step you are in — do not read all
  references up front.

## Checklist

1. **Tools** — follow `references/setup.md`. Run `scripts/setup.py probe`,
   show candidates (including `unusable` entries and their errors) —
   binaries and `param/` come bundled in the repo, so install just
   copies them; then `scripts/setup.py install`.
   probe shows everything already installed and the user confirms reuse.
2. **Interview** — follow `references/interview.md`. One question pass:
   inputs, box mode, run dir, pose, recipe + the flags it still needs
   (e.g. `--flexres.res` for flexible/mutation-dg). Also ask before
   every step marked **ASK** in refs (e.g. receptor HETATM stripping).
3. **Execute** — per `references/contract.md`, run
   `python3 <skill_dir>/scripts/pipeline.py workflow --run_dir <d>
   --prepare_receptor <f> --prepare_ligand <f> --recipe <n>` plus
   interview flags (`--prepare_autobox_ligand`, `--drop_hetatm`,
   `--keep_metals`, `--pose`, `--timeout`). For redocking a known
   complex, `--ligand_from_receptor <spec>` replaces
   `--prepare_ligand` (run `scan` first to list candidates). Any unknown `--flag value` is a vinardock
   flag passed through verbatim — `--seed`, box coords, `autobox`,
   `flexres`, swarm/scoring options all go on the same command line.
   A whole vinardock config file merges with `--config <path>`.
   Each stage is gated on its `status.json`; do not perform these gates
   by judgment.
4. **Result** — present `<run_dir>/report.md` only if the workflow exits
   successfully. Always tell the user which receptor residues were kept
   and which were dropped (`Receptor non-standard residues kept/dropped`
   — the "Receptor residues" section of report.md), since those are the
   non-standard residues other than the 20 standard amino acids. Relay
   any other `warnings` from the stage status files (positive scores,
   poses outside the box, zero interactions, failed ligands) — they flag
   suspect science, not script bugs.
5. **Failure / resume** — report the stage error + log tail printed by
   the workflow. On an input/conversion failure, do not start inspecting
   the file: ask the user whether they want help fixing it, and only
   inspect once they say yes. The skip/staleness mechanism is defined in
   `contract.md`; the judgment calls are: `--force` only when the user
   requests a full rerun, and a new run dir (not `--force`) whenever
   tools, recipe, inputs or settings changed — input edits are not
   detected on resume.
