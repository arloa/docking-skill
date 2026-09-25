# docking — Agent Skill

Reproducible Vinardock molecular docking pipeline (prepare → dock →
analyse) packaged as an [Agent Skills](https://agentskills.io)-format
skill: a `SKILL.md` instruction file plus the Python scripts and
reference docs the agent needs to coordinate the run.

## Requirements

- Linux x86_64 — the Vinardock/obabel-vinardock release assets are
  linux-amd64 only
- Python ≥ 3.9 (stdlib only; no pip dependencies)
- Network access on first run to download the pinned tool binaries
  (vinardock, obabel-vinardock, PLIP) into
  `~/.local/share/docking-tools` — override per run with `--tools-dir`

## Install

Copy or symlink this directory into your agent's skills folder:

| Host | Location |
|---|---|
| Devin | `~/.config/devin/skills/docking` |
| Claude Code | `~/.claude/skills/docking` or `<project>/.claude/skills/docking` |
| Codex / generic | `~/.agents/skills/docking` or `<project>/.agents/skills/docking` |
| Cursor | `<project>/.cursor/skills/docking` |

On a single machine, a symlink keeps one source of truth:

```bash
ln -s /path/to/docking ~/.claude/skills/docking
```

## Usage

Ask the agent to dock a receptor against one or more ligands (`.pdb`,
`.sdf`, `.mol2`, `.pdbt`, or a `.smi` SMILES file). The skill interviews
for inputs, search box (manual or autobox), recipe, seed and run
directory, then runs `scripts/workflow.py`, which gates each stage on
artifact hashes and writes `<run_dir>/report.md` — including the exact
vinardock config and log — for reproducibility.

Non-interactive agents: every interview answer maps to a `workflow.py`
flag (`--receptor --ligand --recipe --seed --run-dir`, plus `--set`,
`--slot`, `--autobox-ligand`, `--drop-hetatm`); supply them all and no
questions are asked.

## Development

- Tests: `python3 -m unittest discover -s tests`
- `AGENTS.md` documents repo conventions; `references/contract.md` is
  the stage-script file interface.
