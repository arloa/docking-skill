# docking — Agent Skill

Vinardock molecular docking pipeline (prepare → dock → analyse)
packaged as an [Agent Skills](https://agentskills.io)-format skill: a
`SKILL.md` instruction file plus the Python scripts and reference docs
the agent needs to coordinate the run.

## Requirements

- Linux x86_64 — the Vinardock/obabel-vinardock release assets are
  linux-amd64 only
- Python ≥ 3.9 (stdlib only; no pip dependencies)
- Network access on first run to download the pinned tool binaries
  (vinardock, obabel-vinardock, PLIP) into
  `~/.local/share/docking-tools` — override per run with `--tools_dir`

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
directory, then runs `scripts/pipeline.py workflow`, which gates each
stage on its `status.json` and writes `<run_dir>/report.md` — including
the exact vinardock config and per-ligand interaction tables.
Results are validated structurally (score vs. model energies, pose
inside the search box, PLIP binding-site identity); warnings flag
suspect results rather than stopping the run.

Non-interactive agents: every interview answer maps to a `pipeline.py
workflow` flag (`--prepare_receptor --prepare_ligand --recipe
--run_dir`, plus `--prepare_autobox_ligand`, `--drop_hetatm`,
`--pose`, `--timeout`); supply them all and no questions are asked.
Vinardock's own flags (`--seed`, box coordinates, `autobox`,
`flexres.*`, swarm/scoring options) are passed through verbatim on the
same command line — precedence is CLI > `--config` > recipe > script
defaults.
Re-running the same command skips completed stages; `--force` reruns
everything.

## Development

- Tests: `python3 -m unittest discover -s tests` — the `IntegrationTests`
  class runs the real binaries (when `~/.local/share/docking-tools` is
  populated) and asserts their actual output formats.
- `AGENTS.md` documents repo conventions; `references/contract.md` is
  the stage interface.
