#!/usr/bin/env python3
"""Vinardock docking pipeline: prepare -> dock -> analyse, coordinated by the
`workflow` subcommand. Stages communicate only through the run-dir filesystem:
each writes <stage>/status.json; the coordinator skips stages whose status is
ok, whose artifacts still exist, and that are newer than their predecessor."""
import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import xml.etree.ElementTree as ET
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
TOOLS = Path.home() / '.local/share/docking-tools'
# the repo ships fully static binaries — no install or host libs needed
BUNDLED_OBABEL = ROOT / 'bin/obabel-vinardock'
BUNDLED_VINARDOCK = ROOT / 'bin/vinardock'
STAGES = ('prepare', 'dock', 'analyse')
FOLDERS = {'prepare': 'prep', 'dock': 'dock', 'analyse': 'analysis'}
PARAM_FILES = ('param.dat', 'param.TxT.dat', 'dun2010bbdep.bin')

FLAG = re.compile(r'^--([\w.]+)(?:\s+|=)?(.*)$')
PASSTHROUGH = re.compile(r'--([\w.]+)')
# keys the pipeline generates as run-dir paths — an override can desync
# preparation/analysis, so changing one always warns (never blocks)
GENERATED = {'receptor', 'ligand', 'out', 'autobox_ligand',
             'scoring', 'scoring.table', 'scoring.tableTxT'}
PATH_KEYS = {'receptor', 'ligand', 'out', 'autobox_ligand',
             'scoring.table', 'scoring.tableTxT', 'rotamer_lib'}
SCORE_TOLERANCE = 0.05

CATEGORIES = {'hydrogen_bonds': 'H-bonds', 'salt_bridges': 'Salt bridges',
              'pi_stacks': 'Pi-stacks', 'hydrophobic_interactions': 'Hydrophobic',
              'pi_cation_interactions': 'Pi-cation', 'halogen_bonds': 'Halogen',
              'water_bridges': 'Water bridges', 'metal_complexes': 'Metal complexes'}


# ---------------------------------------------------------------- helpers

def now():
    return datetime.now(timezone.utc).isoformat()


def write_status(path, payload):
    temp = path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(payload, indent=2) + '\n')
    os.replace(temp, path)


def read_status(root, stage):
    path = root / FOLDERS[stage] / 'status.json'
    return json.loads(path.read_text()) if path.is_file() else None


def rel(path, root):
    return str(path.relative_to(root))


def new_status(stage):
    started = now()
    return {'stage': stage, 'status': 'failed', 'started': started,
            'finished': started, 'artifacts': [], 'metrics': {},
            'warnings': [], 'error': None}


def run_logged(command, log, timeout):
    command = [str(c) for c in command]
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    with log.open('a') as stream:
        stream.write('$ ' + ' '.join(command) + '\n' + result.stdout + result.stderr + '\n')
    if result.returncode:
        raise RuntimeError(f'command failed ({result.returncode}): {result.stderr.strip()[-300:]}')


def stream_logged(command, log, timeout, cwd):
    """Run a long command, teeing stdout to log and stderr, enforcing timeout."""
    command = [str(c) for c in command]
    with log.open('w') as stream:
        proc = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)

        def pump():
            for line in proc.stdout:
                stream.write(line)
                stream.flush()
                print(line, end='', file=sys.stderr, flush=True)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            reader.join(5)
            raise RuntimeError(f'command exceeded timeout of {timeout}s')
        reader.join()
    return rc


def atom_lines(path):
    return [line for line in path.read_text(errors='replace').splitlines()
            if line.startswith(('ATOM  ', 'HETATM'))]


def atom_xyz(line):
    try:
        return (float(line[30:38]), float(line[38:46]), float(line[46:54]))
    except ValueError:
        return None


def coords(path):
    """(x, y, z) per atom line of a pdb/pdbt file (fixed columns)."""
    return [c for line in atom_lines(path) if (c := atom_xyz(line)) is not None]


def models(path):
    """Yield one list of lines per MODEL block (markers included); a file
    with no MODEL records yields a single block containing every line.
    A trailing unterminated MODEL is yielded too — callers decide."""
    lines = path.read_text(errors='replace').splitlines(keepends=True)
    if not any(line.startswith('MODEL') for line in lines):
        yield lines
        return
    block = None
    for line in lines:
        if line.startswith('MODEL'):
            block = [line]
        elif line.startswith('ENDMDL'):
            if block is not None:
                block.append(line)
                yield block
            block = None
        elif block is not None:
            block.append(line)
    if block:  # unterminated MODEL
        yield block


def parse_passthrough(tokens):
    """argparse leftovers -> verbatim vinardock flags. Grammar is the
    vinardock CLI's own: `--key value` pairs and bare `--key` switches —
    `--key=value` is not vinardock syntax and is rejected."""
    flags = OrderedDict()
    index = 0
    while index < len(tokens):
        match = PASSTHROUGH.fullmatch(tokens[index])
        if not match:
            raise ValueError('expected --flag value pairs (vinardock syntax), '
                             'got ' + repr(tokens[index]))
        index += 1
        value = ''
        if index < len(tokens) and not tokens[index].startswith('--'):
            value = tokens[index]
            index += 1
        flags[match.group(1)] = value
    return flags


def anchor_paths(flags, base):
    """Relative path values must be made absolute before consolidation —
    vinardock runs with cwd=attempt dir, so a config's `./PDBT/x` or a
    cli's relative path would otherwise resolve to the wrong place.
    `base` is the config file's directory (--config) or the caller's cwd
    (cli). PATH_KEYS resolve unconditionally; anything else only when it
    exists, so plain values (modes, specs, numbers) pass through."""
    for key, value in flags.items():
        if not value:
            continue
        value = Path(value).expanduser()
        if value.is_absolute():
            continue
        if key in PATH_KEYS or (base / value).exists():
            flags[key] = str(base / value)
    return flags


def merge_into(base, source, layer, warnings, guard=()):
    """Last-wins overlay (vinardock convention: cli precedes config). Warns
    only where an override can desync the pipeline — a key in `guard` — or
    where cli changes a recipe value. Overriding plain defaults (threads,
    conformations, science params) is routine and quiet."""
    for name, value in layer.items():
        if name in guard and name in base and base[name] != value:
            warnings.append(f'--{name} from {source} overrides {base[name]!r}')
        base[name] = value


def parse_stamp(text):
    """ISO-8601 -> datetime; tolerant of a trailing 'Z'. None if unparseable."""
    try:
        return datetime.fromisoformat(str(text).replace('Z', '+00:00'))
    except ValueError:
        return None


def stage_done(root, stage):
    """Reusable iff status ok, artifacts recorded + all exist, and not older
    than its predecessor."""
    data = read_status(root, stage)
    if not data or data.get('stage') != stage or data.get('status') != 'ok':
        return False
    if not data.get('artifacts') or any(
            not (root / path).is_file() or (root / path).stat().st_size == 0
            for path in data['artifacts']):
        return False
    index = STAGES.index(stage)
    if index:
        pred = read_status(root, STAGES[index - 1])
        if not pred or pred.get('status') != 'ok':
            return False
        started, finished = parse_stamp(data.get('started')), parse_stamp(pred.get('finished'))
        if started is None or finished is None or started < finished:
            return False
    return True


def log_tail(root, stage):
    """Last 20 lines of the stage's own log for failure reporting. Prefers
    the path recorded in status.json; falls back to the newest attempt log."""
    data = read_status(root, stage) or {}
    candidates = []
    recorded = (data.get('metrics') or {}).get('vinardock_log')
    if recorded:
        candidates.append(root / recorded)
    if stage == 'prepare':
        candidates.append(root / 'prep/prep.log')
    attempts = root / 'dock/attempts'
    if stage == 'dock' and attempts.is_dir():
        candidates += sorted(attempts.glob('*/vinardock.log'),
                             key=lambda p: p.stat().st_mtime)
    for log in reversed(candidates):
        if log.is_file():
            return '\n'.join(log.read_text(errors='replace').splitlines()[-20:])
    return ''


# ---------------------------------------------------------------- prepare

def safe_name(name):
    stem = re.sub(r'[^A-Za-z0-9_-]+', '_', name.strip()).strip('_')
    if not stem:
        raise ValueError('ligand name has no safe characters: ' + repr(name))
    return stem


def strip_source_remark(path):
    """Drop the obabel `REMARK  Name =` line (it leaks the temp conversion
    path), so prepared files depend only on input content."""
    lines = path.read_text(errors='replace').splitlines(keepends=True)
    kept = [line for line in lines if not line.startswith('REMARK  Name = ')]
    if len(kept) != len(lines):
        path.write_text(''.join(kept))


def planar_input(path):
    lines = path.read_text(errors='replace').splitlines()
    suffix = path.suffix.lower()
    if suffix == '.pdb':
        z = [float(line[46:54]) for line in lines if line.startswith(('ATOM  ', 'HETATM'))]
    elif suffix == '.sdf':
        if sum(line.strip() == '$$$$' for line in lines) > 1:
            raise ValueError('one molecule per SDF file required')
        if len(lines) < 4:
            raise ValueError('truncated SDF')
        n = int(lines[3][:3])
        z = [float(line[20:30]) for line in lines[4:4 + n]]
    elif suffix == '.mol2':
        if sum(line.startswith('@<TRIPOS>MOLECULE') for line in lines) > 1:
            raise ValueError('one molecule per Mol2 file required')
        start = next((i for i, line in enumerate(lines) if line.startswith('@<TRIPOS>ATOM')), None)
        if start is None:
            raise ValueError('Mol2 has no ATOM section')
        z = []
        for line in lines[start + 1:]:
            if line.startswith('@<TRIPOS>'):
                break
            if line.strip():
                z.append(float(line.split()[4]))
    else:
        return False
    return bool(z) and all(value == 0 for value in z)


def to_pdbt(source, dest, obabel, log, timeout, warnings, label, smiles=None):
    """Extension -> conversion strategy for one ligand-like input. Keeping
    every suffix branch in one place is deliberate: the ligand and autobox
    paths decided the same thing twice and drifted (gen3d on one, not the
    other). `smiles` is the molecule string when the input is a SMILES
    entry rather than a structure file."""
    if smiles is not None or source.suffix.lower() in ('.smi', '.smiles'):
        if smiles is None:
            entries = [line.split(maxsplit=1)[0] for line in
                       source.read_text().splitlines()
                       if line.strip() and not line.lstrip().startswith('#')]
            if len(entries) != 1:
                raise ValueError(f'{label}: one molecule per file required '
                                 f'({len(entries)} SMILES lines)')
            smiles = entries[0]
        with tempfile.TemporaryDirectory(dir=dest.parent) as temp:
            sdf = Path(temp) / 'in.sdf'
            run_logged([obabel, '-:' + smiles, '--gen3d', '-O', sdf], log, timeout)
            run_logged([obabel, sdf, '-O', dest, '-p7.4'], log, timeout)
        return
    suffix = source.suffix.lower()
    if suffix == '.pdbt':
        shutil.copyfile(source, dest)
        return
    if suffix not in ('.sdf', '.mol2', '.pdb'):
        raise ValueError(f'unsupported {label} format {suffix!r} '
                         '— use .pdbt/.smi/.sdf/.mol2/.pdb')
    extra = ['--gen3d'] if planar_input(source) else []
    if extra:
        warnings.append(f'{label}: planar input regenerated in 3D')
    run_logged([obabel, source, *extra, '-O', dest, '-p7.4'], log, timeout)


# element-symbol detection (col 77-78) with residue-name fallback —
# "standard" receptor prep drops waters/cofactors/co-solutes but keeps
# metal ions, which are often part of the binding site
METALS = frozenset(
    'LI BE NA MG K CA SC TI V CR MN FE CO NI CU ZN GA GE RB SR Y ZR NB MO '
    'RU RH PD AG CD IN SN SB CS BA HF TA W RE OS IR PT AU HG TL PB BI '
    'LA CE PR ND SM EU GD TB DY HO ER TM YB LU'.split())


def is_metal_hetatm(line):
    element = line[76:78].strip().upper()
    if element:
        return element in METALS
    return line[17:20].strip().upper() in METALS


# modified amino acids (MSE, SEP, TPO, ...) are the polypeptide chain even
# when a .pdb marks them HETATM — they are never stripped or docked as ligands
MODIFIED_AA = frozenset(
    line.strip() for line in (ROOT / 'references/modified_aa.txt').read_text().splitlines()
    if line.strip() and not line.startswith('#'))


def is_modified_residue(line):
    return line[17:20].strip().upper() in MODIFIED_AA


# prepare reports every residue it keeps or drops *other than* these, so a
# non-standard residue is never silently discarded or silently retained.
# Histidine tautomers count as standard (protonation variants, not PTMs).
STANDARD_AA = frozenset(
    'ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR '
    'TRP TYR VAL HID HIE HIP'.split())


def residue_counts(lines):
    """{resname: n} — distinct residues (chain + resseq) per residue name,
    counting ATOM and HETATM records alike."""
    seen = set()
    counts = {}
    for line in lines:
        if line.startswith(('ATOM  ', 'HETATM')):
            key = (line[17:20].strip(), line[21:22].strip(), line[22:27].strip())
            if key not in seen:
                seen.add(key)
                counts[key[0]] = counts.get(key[0], 0) + 1
    return counts


def nonstandard_counts(lines):
    return {name: n for name, n in residue_counts(lines).items()
            if name.upper() not in STANDARD_AA}


def format_counts(counts):
    """`MSE x2, ZN x1` — residue names sorted, each with its instance count."""
    return ', '.join(f'{name} x{counts[name]}' for name in sorted(counts))


WATER_RESNAMES = frozenset({'HOH', 'WAT', 'H2O', 'DOD', 'SOL', 'TIP3'})


def hetatm_groups(lines):
    """Dockable HETATM candidates in a receptor, keyed by
    (resname, chain, resseq). Waters, metal-only groups, fragments
    under 3 atoms (ions) and modified amino acids (MSE, SEP, ...) are
    never dockable ligands — the last are the polymer itself."""
    groups = OrderedDict()
    for line in lines:
        if line.startswith('HETATM'):
            key = (line[17:20].strip(), line[21:22].strip(), line[22:27].strip())
            groups.setdefault(key, []).append(line)
    result = []
    for (resname, chain, resseq), group in groups.items():
        if resname in WATER_RESNAMES or len(group) < 3 \
                or all(is_metal_hetatm(line) for line in group) \
                or resname.upper() in MODIFIED_AA:
            continue
        result.append({'resname': resname, 'chain': chain, 'resseq': resseq,
                       'atoms': len(group), 'lines': group})
    return result


def pick_hetatm(groups, spec):
    """spec is RESNAME or RESNAME:CHAIN:RESSEQ."""
    def listing():
        return ', '.join('{}:{}:{}'.format(g['resname'], g['chain'] or '-',
                                           g['resseq']) for g in groups)
    parts = spec.split(':')
    if len(parts) == 1:
        matches = [g for g in groups if g['resname'] == parts[0].upper()]
    elif len(parts) == 3:
        resname, chain, resseq = parts
        matches = [g for g in groups
                   if (g['resname'], g['chain'], g['resseq'])
                   == (resname.upper(), chain.strip(), resseq.strip())]
    else:
        raise ValueError('ligand_from_receptor spec must be RESNAME or '
                         'RESNAME:CHAIN:RESSEQ — got ' + spec)
    if not matches:
        raise ValueError(f'no HETATM ligand matching {spec!r} in receptor; '
                         f'candidates: {listing() or "none"}')
    if len(matches) > 1:
        raise ValueError(f'{spec} matches {len(matches)} molecules — '
                         f'specify RESNAME:CHAIN:RESSEQ: {listing()}')
    return matches[0]


def stage_prepare(run_dir, receptor, ligand, obabel_vinardock,
                  drop_hetatm=False, keep_metals=False, autobox_ligand=None,
                  ligand_from_receptor=None, timeout=300):
    root = run_dir.resolve()
    stage = root / 'prep'
    stage.mkdir(parents=True, exist_ok=True)
    (stage / 'ligands').mkdir(exist_ok=True)
    log = stage / 'prep.log'
    log.write_text('')  # per-run log, not an append-only history
    state = new_status('prepare')
    try:
        obabel = obabel_vinardock.resolve(strict=True)
        if not os.access(obabel, os.X_OK):
            raise ValueError('obabel-vinardock is not executable')
        receptor = receptor.expanduser().resolve(strict=True)
        lines = receptor.read_text(errors='replace').splitlines(keepends=True)
        chains = sorted({line[21:22].strip() or '(blank)' for line in lines
                         if line.startswith(('ATOM  ', 'HETATM'))})
        if keep_metals and not drop_hetatm:
            raise ValueError('--keep_metals only makes sense with --drop_hetatm')
        hetatms = sorted({line[17:20].strip() for line in lines if line.startswith('HETATM')})
        # redock mode: extract a co-crystal ligand from the receptor BEFORE
        # any HETATM stripping. The molecule always leaves the receptor —
        # docking into an occupied pocket is meaningless — regardless of
        # --drop_hetatm.
        extracted = None
        if ligand_from_receptor:
            groups = hetatm_groups(lines)
            group = pick_hetatm(groups, ligand_from_receptor)
            stem = safe_name(group['resname']) if sum(
                g['resname'] == group['resname'] for g in groups) == 1 \
                else safe_name('{}_{}{}'.format(group['resname'], group['chain'], group['resseq']))
            drop_key = (group['resname'], group['chain'], group['resseq'])
            lines = [line for line in lines if not (
                line.startswith('HETATM') and (line[17:20].strip(),
                line[21:22].strip(), line[22:27].strip()) == drop_key)]
            extracted = (stem, group)
            hetatms = sorted({line[17:20].strip() for line in lines
                              if line.startswith('HETATM')})
        # everything the user must be told about: which non-standard residues
        # (waters, ions, cofactors, modified amino acids, ligands) were kept
        # and which were dropped — standard amino acids are never listed
        before = nonstandard_counts(lines)
        if drop_hetatm:
            # modified amino acids are the polymer, not a heterogen — they
            # survive every stripping mode
            lines = [line for line in lines if not line.startswith('HETATM')
                     or is_modified_residue(line)
                     or (keep_metals and is_metal_hetatm(line))]
        kept_residues = nonstandard_counts(lines)
        dropped_residues = {name: n for name, n in before.items()
                            if name not in kept_residues}
        if kept_residues:
            state['warnings'].append('Receptor non-standard residues kept: '
                                     + format_counts(kept_residues))
        if dropped_residues:
            state['warnings'].append('Receptor non-standard residues dropped: '
                                     + format_counts(dropped_residues))
        if not any(line.startswith('ATOM  ') for line in lines):
            raise ValueError('receptor has no ATOM records')
        prepared_rec = stage / (receptor.stem + '.pdbt')
        # receptor stays outside to_pdbt() on purpose: -xr -xc and a temp file
        # that preserves the input extension (obabel infers format from it)
        with tempfile.TemporaryDirectory(dir=stage) as temp:
            source = Path(temp) / receptor.name
            source.write_text(''.join(lines))
            if receptor.suffix.lower() == '.pdbt':
                shutil.copyfile(source, prepared_rec)
            else:
                run_logged([obabel, source, '-O', prepared_rec, '-xr', '-xc'], log, timeout)
        if not atom_lines(prepared_rec):
            raise ValueError('prepared receptor has no atoms')
        strip_source_remark(prepared_rec)
        state['artifacts'].append(rel(prepared_rec, root))
        ref_out = None
        if autobox_ligand:
            ref = autobox_ligand.expanduser().resolve(strict=True)
            ref_out = stage / (ref.stem + '_autobox.pdbt')
            if ref_out == prepared_rec:
                raise ValueError('autobox reference and receptor share the same stem: ' + ref.stem)
            to_pdbt(ref, ref_out, obabel, log, timeout,
                    state['warnings'], 'autobox reference')
            if not atom_lines(ref_out):
                raise ValueError('autobox reference has no atoms')
            strip_source_remark(ref_out)
            state['artifacts'].append(rel(ref_out, root))
        names = set()   # real stems — dock compares them to glob'd filenames
        seen = set()    # case-insensitive dedup
        if extracted:
            stem, group = extracted
            target = stage / 'ligands' / (stem + '.pdbt')
            with tempfile.TemporaryDirectory(dir=stage) as temp:
                source = Path(temp) / (stem + '.pdb')
                source.write_text(''.join(group['lines']))
                to_pdbt(source, target, obabel, log, timeout,
                        state['warnings'], 'extracted ' + stem)
            if not atom_lines(target) or 'TORSDOF ' not in target.read_text(errors='replace'):
                raise ValueError('invalid prepared PDBT ligand: ' + stem)
            strip_source_remark(target)
            state['artifacts'].append(rel(target, root))
            # the extracted pose is the natural autobox reference for a
            # redock — dock picks it up via the _autobox.pdbt convention
            shutil.copyfile(target, stage / (stem + '_autobox.pdbt'))
            state['artifacts'].append(rel(stage / (stem + '_autobox.pdbt'), root))
            names.add(stem)
            seen.add(stem.lower())
        inputs = []
        for path in ligand or []:
            source = path.expanduser().resolve(strict=True)
            if source.suffix.lower() in ('.smi', '.smiles'):
                for line in source.read_text().splitlines():
                    if line.strip() and not line.lstrip().startswith('#'):
                        parts = line.split(maxsplit=1)
                        if len(parts) != 2:
                            raise ValueError('SMILES input needs SMILES and a ligand name per line')
                        inputs.append((parts[1].strip(), source, parts[0]))
            else:
                inputs.append((source.stem, source, None))
        for name, source, smiles in inputs:
            stem = safe_name(name) if smiles else name
            if stem.lower() in seen:
                raise ValueError('duplicate ligand name: ' + stem)
            names.add(stem)
            seen.add(stem.lower())
            target = stage / 'ligands' / (stem + '.pdbt')
            to_pdbt(source, target, obabel, log, timeout,
                    state['warnings'], stem, smiles=smiles)
            if not atom_lines(target) or 'TORSDOF ' not in target.read_text(errors='replace'):
                raise ValueError('invalid prepared PDBT ligand: ' + stem)
            strip_source_remark(target)
            state['artifacts'].append(rel(target, root))
        if not names:
            raise ValueError('no ligands prepared')
        state['metrics'] = {'n_ligands': len(names), 'ligands': sorted(names),
                            'receptor_chains': chains, 'hetatm_resnames': hetatms,
                            'residues_kept': kept_residues,
                            'residues_dropped': dropped_residues,
                            'ligand_from_receptor': extracted[0] if extracted else None,
                            'receptor_file': prepared_rec.name,
                            'autobox_file': ref_out.name if ref_out
                            else (extracted[0] + '_autobox.pdbt' if extracted else None)}
        state['status'] = 'ok'
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        state['error'] = str(error)
        with log.open('a') as stream:
            stream.write('ERROR: ' + str(error) + '\n')
    state['finished'] = now()
    if log.exists():
        state['artifacts'].append(rel(log, root))
    write_status(stage / 'status.json', state)
    print_state(stage, state)
    return 0 if state['status'] == 'ok' else 1


# ---------------------------------------------------------------- dock

def print_state(stage, state):
    for warning in state['warnings']:
        print('warning: ' + warning)
    line = stage.name + ': ' + state['status']
    if state['error']:
        line += ' — ' + state['error']
    print(line)


def parse_flags(path):
    """A recipe or --config file is a plain vinardock config fragment:
    `--key value` lines, `#` comments, no templating — the same grammar
    vinardock itself parses."""
    flags = OrderedDict()
    for number, line in enumerate(path.read_text().splitlines(), 1):
        body = line.partition('#')[0].strip()
        if not body:
            continue
        match = FLAG.fullmatch(body)
        if not match:
            raise ValueError(f'{path.name}:{number}: invalid flag line: {body}')
        flags[match.group(1)] = match.group(2).strip()
    return flags


def model_data(path):
    """Per-MODEL energies (REMARK 980) + ligand torsdof from an output pdbt."""
    energies = []
    torsdof = None
    for block in models(path):
        for line in block:
            if line.startswith('REMARK 980'):
                # label must be alphabetic — a label containing a digit (or a
                # stray colon elsewhere) yields no match instead of a wrong energy
                match = re.search(r'[A-Za-z_][A-Za-z_ ]*:\s*([-+]?\d+(?:\.\d+)?)', line[10:])
                if match:
                    energies.append(float(match.group(1)))
            elif line.startswith('TORSDOF'):
                torsdof = int(line.split()[1])
    return energies, torsdof


def ligand_output(outdir, name, single):
    return outdir / (name + '.pdbt') if single else outdir / name[:2] / name / (name + '.pdbt')


def score_column(header):
    """Best-score column by header name. Cells are normalized (lowercase,
    units stripped) and matched by content, not exact equality — real
    headers look like 'vinardo score (kcal/mol)'. Reference-state mode
    prefers E_corrected, the normalized energy that REMARK 980 reports."""
    cells = [h.strip().lower() for h in header]
    for pred in (lambda c: 'score' in c,
                 lambda c: c == 'e_corrected',
                 lambda c: c == 'binding_energy',
                 lambda c: 'energy' in c or 'affinity' in c):
        for i, cell in enumerate(cells):
            if pred(cell):
                return i
    return None


def parse_log_csv(rows, names):
    """Rows from log.csv -> (results, failures, dg_mode, score_col).
    Structural problems raise; per-ligand failures collect in `failures`."""
    header = rows[0]
    if not header or header[0] != 'Ligand':
        raise RuntimeError('unsupported log.csv header: ' + ','.join(header))
    dg_mode = header[1:4] == ['E_WT', 'E_MUT', 'dG']
    if not dg_mode and (len(header) < 2 or header[1] != 'nConfs'):
        raise RuntimeError('unsupported log.csv header: ' + ','.join(header))
    score_col = score_column(header)
    rmsd_col = next((i for i, h in enumerate(header) if h.strip().lower() == 'rmsd'), None)
    results = {}
    failures = {}
    for row in rows[1:]:
        name = row[0]
        if name not in names or name in results or name in failures:
            raise RuntimeError('unexpected or duplicate ligand in log.csv: ' + name)
        if dg_mode:
            if len(row) < 4 or row[3] == 'N/A':
                failures[name] = 'vinardock reported no dG'
                continue
            results[name] = {'dG': {key: float(value)
                                    for key, value in zip(header[1:4], row[1:4])},
                             'rmsd': 'N/A'}
        else:
            try:
                count = int(row[1])
            except (IndexError, ValueError):
                raise RuntimeError(f'malformed nConfs for ligand: {name}')
            if count <= 0:
                failures[name] = 'vinardock reported 0 conformers'
                continue
            entry = {'nconfs': count,
                     'rmsd': row[rmsd_col] if rmsd_col is not None and len(row) > rmsd_col
                             else (row[-1] if len(row) > len(header) else 'N/A')}
            if score_col is not None:
                try:
                    entry['csv_score'] = float(row[score_col])
                except (IndexError, ValueError):
                    failures[name] = 'unparseable score in log.csv'
                    continue
            if 'E_corrected' in header:
                entry['reference_state'] = {
                    key: float(row[header.index(key)])
                    for key in ('binding_energy', 'BE_ligwater', 'BE_recwater', 'E_corrected')}
            results[name] = entry
    for name in names:
        if name not in results and name not in failures:
            failures[name] = 'no row in log.csv'
    return results, failures, dg_mode, score_col


def check_score(name, csv_score, energies):
    """best = the log.csv score when present, cross-checked against the model
    energies in the output pdbt; fail closed when they disagree."""
    best = min(energies)
    if csv_score is not None:
        if abs(csv_score - best) > SCORE_TOLERANCE:
            raise RuntimeError(f'{name}: log.csv score {csv_score} disagrees with '
                               f'pdbt energies {best}')
        best = csv_score
    return best


def resolve_box(preliminary, autobox_path):
    """Box center/size in Angstrom. Autobox: computed from the prepared
    reference's bounding box + pad (vinardock does not print the resolved
    box, so computing it is the only reliable provenance)."""
    if 'autobox' in preliminary:
        xyz = coords(autobox_path)
        if not xyz:
            raise ValueError('autobox reference has no coordinates: ' + autobox_path.name)
        pad = float(preliminary['autobox'])
        extent = [max(c[i] for c in xyz) - min(c[i] for c in xyz) for i in range(3)]
        if any(e <= 1e-6 for e in extent):
            raise ValueError('autobox reference has zero extent '
                             f'({["%.2f" % e for e in extent]}): degenerate '
                             'coordinates — a 3D single-molecule reference is required')
        return {'center': [(min(c[i] for c in xyz) + max(c[i] for c in xyz)) / 2 for i in range(3)],
                'size': [extent[i] + 2 * pad for i in range(3)]}
    if 'center_x' in preliminary:
        return {'center': [float(preliminary['center_' + c]) for c in 'xyz'],
                'size': [float(preliminary['size_' + c]) for c in 'xyz']}
    return None


def centroid_outside_box(path, box):
    """True if the first MODEL's atom centroid lies outside the search box."""
    block = next(models(path), [])
    xyz = [c for line in block if line.startswith(('ATOM  ', 'HETATM'))
           for c in [atom_xyz(line)] if c is not None]
    if not xyz or not box:
        return False
    mean = [sum(c[i] for c in xyz) / len(xyz) for i in range(3)]
    return any(abs(mean[i] - box['center'][i]) > box['size'][i] / 2 + 1e-6 for i in range(3))


def sanity_warnings(results, box, outdir, single, dg_mode):
    """Non-fatal suspicion checks — vinardock exits 0 on failed searches,
    so these warnings are the only signal. Deliberately warnings, never
    gates. The scientific-correctness policy lives in this one block."""
    warnings = []
    for name, entry in results.items():
        if entry.get('status') == 'failed':
            continue
        if not dg_mode and entry['best'] > 0:
            warnings.append(f'{name}: positive best score ({entry["best"]:.2f} kcal/mol) '
                            'usually indicates a failed search')
        if box and centroid_outside_box(ligand_output(outdir, name, single), box):
            warnings.append(f'{name}: pose centroid lies outside the resolved search box')
    return warnings


def mutation_spec(value):
    """True for a real mutation spec like A:S63T (chain:OLDresNEW, OLD != NEW).
    vinardock fails open on bad specs (degrades to rigid docking), so the
    pipeline validates instead of trusting the flag."""
    match = re.fullmatch(r'[A-Za-z0-9_]+:([A-Za-z])(\d+)([A-Za-z])', value.strip())
    return bool(match and match.group(1).upper() != match.group(3).upper())


def write_config(flags, path):
    """config.txt = the fully merged, absolute-path config vinardock runs."""
    path.write_text(''.join('--' + key + (' ' + val if val else '') + '\n'
                            for key, val in flags.items()))


def stage_dock(run_dir, recipe=None, config=None, cli_flags=None,
               threads=None, autobox_ligand=None, tools_dir=TOOLS, timeout=21600):
    root = run_dir.resolve()
    stage = root / 'dock'
    stage.mkdir(parents=True, exist_ok=True)
    state = new_status('dock')
    try:
        tools = tools_dir.expanduser().resolve()
        executable = BUNDLED_VINARDOCK if BUNDLED_VINARDOCK.is_file() \
            else tools / 'bin/vinardock'
        if not executable.is_file():
            raise FileNotFoundError(executable)
        prep = read_status(root, 'prepare')
        prepared = bool(prep and prep.get('status') == 'ok')
        # config consolidation hub: script defaults < recipe < --config
        # file < cli flags, last wins. Prep supplies the receptor/ligand
        # defaults when it ran; a config can provide them instead, so dock
        # is usable standalone.
        number = 1
        while (stage / 'attempts' / f'attempt-{number}').exists():
            number += 1
        attempt = stage / 'attempts' / f'attempt-{number}'
        preliminary = OrderedDict({'out': str(attempt / 'DOCK'),
                                   'scoring': '2vinardo', 'scoring.table': str(tools / 'param/param.dat'),
                                   'scoring.tableTxT': str(tools / 'param/param.TxT.dat'),
                                   'rotamer_lib': str(tools / 'param/dun2010bbdep.bin'),
                                   'threads': str(threads or os.cpu_count() or 1),
                                   'conformations': '9', 'calc_lig_rmsd': ''})
        prep_ligand_default = None
        if prepared:
            receptor = root / 'prep' / prep['metrics'].get('receptor_file', 'receptor.pdbt')
            ligands = sorted((root / 'prep/ligands').glob('*.pdbt'))
            if receptor.is_file() and ligands:
                prep_ligand_default = str(ligands[0] if len(ligands) == 1
                                          else root / 'prep/ligands')
                preliminary['receptor'] = str(receptor)
                preliminary['ligand'] = prep_ligand_default
            else:
                prepared = False
        if autobox_ligand:
            prepared_ref = root / 'prep' / (Path(autobox_ligand).stem + '_autobox.pdbt')
            if not prepared_ref.is_file():
                raise ValueError('prepared autobox reference missing; '
                                 'run prepare with --prepare_autobox_ligand')
            preliminary['autobox_ligand'] = str(prepared_ref)
        # standard's only flag is vinardock's own default (pso_mc), and
        # config outranks it — so the default recipe applies even with
        # --config and never overrides a user's key
        recipe_path = read_recipe(recipe or 'standard')
        recipe_settings = parse_flags(recipe_path)
        config_settings = OrderedDict()
        if config:
            config_path = config.expanduser().resolve(strict=True)
            config_settings = parse_flags(config_path)
            anchor_paths(config_settings, config_path.parent)
        merge_into(preliminary, 'recipe', recipe_settings, state['warnings'], GENERATED)
        merge_into(preliminary, 'config', config_settings, state['warnings'], GENERATED)
        merge_into(preliminary, 'cli', anchor_paths(cli_flags or {}, Path.cwd()),
                   state['warnings'], GENERATED)
        # redock convenience: when the ligand was extracted from the
        # receptor, its own bound pose is the natural autobox reference
        if 'autobox' in preliminary and 'autobox_ligand' not in preliminary \
                and (prep or {}).get('metrics', {}).get('ligand_from_receptor'):
            ref = root / 'prep' / (prep['metrics']['ligand_from_receptor'] + '_autobox.pdbt')
            if not ref.is_file():
                raise ValueError('extracted-ligand autobox reference missing; '
                                 'rerun prepare with --ligand_from_receptor')
            preliminary['autobox_ligand'] = str(ref)
        if not Path(preliminary['out']).resolve().is_relative_to(root):
            raise ValueError('out must live inside the run dir')
        # whatever won the merge must point at real inputs; names are
        # derived from the effective ligand path so a config-supplied
        # ligand set works without a prior prepare
        receptor_path = Path(preliminary.get('receptor', ''))
        ligand_path = Path(preliminary.get('ligand', ''))
        if not receptor_path.is_file():
            raise ValueError('receptor missing — run prepare first or '
                             'supply --receptor <file.pdbt>')
        if ligand_path.is_dir():
            names = sorted(p.stem for p in ligand_path.glob('*.pdbt'))
        elif ligand_path.is_file():
            names = [ligand_path.stem]
        else:
            raise ValueError('ligand missing — run prepare first or '
                             'supply --ligand <file.pdbt|dir>')
        if not names:
            raise ValueError('no .pdbt ligands found in ' + str(ligand_path))
        if prepared and preliminary['ligand'] == prep_ligand_default and \
                names != sorted(prep['metrics']['ligands']):
            raise ValueError('prepared ligand list changed since prepare')
        box_flags = ('center_x', 'center_y', 'center_z', 'size_x', 'size_y', 'size_z')
        manual = all(k in preliminary for k in box_flags)
        autobox = 'autobox' in preliminary and 'autobox_ligand' in preliminary
        if manual == autobox or (any(k in preliminary for k in box_flags) and autobox):
            raise ValueError('specify either center_x/y/z + size_x/y/z or autobox + autobox_ligand')
        if 'flexres.res' in preliminary and 'flexres.autoflex' in preliminary:
            raise ValueError('flexres.res and flexres.autoflex are mutually exclusive')
        recipe_stem = recipe_path.stem
        if recipe_stem == 'flexible' and not {'flexres.res', 'flexres.autoflex'} & preliminary.keys():
            raise ValueError('flexible recipe needs --flexres.res <spec> '
                             'or --flexres.autoflex <cutoff>')
        if recipe_stem == 'mutation-dg':
            if len(names) != 1:
                raise ValueError('mutation-dg requires exactly one ligand '
                                 '(WT vs mutant is computed per ligand)')
            if not mutation_spec(preliminary.get('flexres.res', '')):
                raise ValueError('mutation-dg requires one mutation spec like A:S63T '
                                 '(chain:OLDresNEW, OLD != NEW)')
        attempt.mkdir(parents=True)
        outdir = Path(preliminary['out'])  # read post-merge: a --out override wins
        config_file = attempt / 'config.txt'
        write_config(preliminary, config_file)
        state['artifacts'].append(rel(config_file, root))
        log = attempt / 'vinardock.log'
        rc = stream_logged([executable, '--config', config_file], log, timeout, cwd=attempt)
        state['artifacts'].append(rel(log, root))
        if rc:
            raise RuntimeError(f'vinardock exited {rc}; see {rel(log, root)}')
        # seed is optional (vinardock defaults to a timestamp); recover the
        # actual seed from the log so provenance is never 'UNKNOWN'
        log_text = log.read_text(errors='replace')
        seed = preliminary.get('seed', '').strip()
        if not seed:
            match = re.search(r'Seed:\s*(\d+)', log_text)
            seed = match.group(1) if match else 'timestamp'
        csvpath = outdir / 'log.csv'
        if not csvpath.is_file():
            raise RuntimeError('vinardock did not write log.csv')
        with csvpath.open(newline='') as stream:
            rows = list(csv.reader(stream))
        if not rows:
            raise RuntimeError('log.csv is empty')
        results, failures, dg_mode, score_col = parse_log_csv(rows, names)
        if 'calc_mutate_dG' in preliminary and not dg_mode:
            raise RuntimeError('mutation-dg did not engage: log.csv is a normal docking '
                               'table (vinardock ignored --calc_mutate_dG); check the '
                               'mutation spec and [FLEX] warnings in vinardock.log')
        if score_col is None and not dg_mode:
            state['warnings'].append('no score column in log.csv header; best = min(model energies)')
        single = len(names) == 1
        for name, entry in list(results.items()):
            output = ligand_output(outdir, name, single)
            if not output.is_file():
                failures[name] = 'output pdbt missing'
                del results[name]
                continue
            energies, torsdof = model_data(output)
            if dg_mode:
                entry['nconfs'] = len(energies)
            if entry['nconfs'] < 1 or len(energies) != entry['nconfs']:
                failures[name] = (f"{entry['nconfs']} conformers in log.csv but "
                                  f'{len(energies)} energies in pdbt')
                del results[name]
                continue
            entry['energies'] = energies
            entry['torsdof'] = torsdof
            entry['best'] = check_score(name, entry.pop('csv_score', None), energies)
            state['artifacts'].append(rel(output, root))
        for name, error in failures.items():
            results[name] = {'status': 'failed', 'error': error}
            state['warnings'].append(f'{name}: {error}')
        if not any(entry.get('status') != 'failed' for entry in results.values()):
            raise RuntimeError('all ligands failed; see warnings')
        state['artifacts'].append(rel(csvpath, root))
        for path in outdir.rglob('*_modified.pdbt'):
            state['artifacts'].append(rel(path, root))
        # vinardock prints the resolved box only when it resolves one
        # (autobox) — prefer it over the computed estimate; the fallback
        # covers manual boxes and older/quieter output
        match = re.search(
            r'Search box: center =\s*([-\d.e]+),\s*([-\d.e]+),\s*([-\d.e]+),'
            r'\s*size =\s*([-\d.e]+),\s*([-\d.e]+),\s*([-\d.e]+)', log_text)
        box = ({'center': [float(v) for v in match.groups()[:3]],
                'size': [float(v) for v in match.groups()[3:]]} if match
               else resolve_box(preliminary,
                                Path(preliminary.get('autobox_ligand', ''))))
        state['warnings'] += sanity_warnings(results, box, outdir, single, dg_mode)
        state['metrics'] = {'recipe': recipe_stem,
                            'config': config.stem if config else None,
                            'seed': seed, 'threads': preliminary['threads'],
                            'resolved_box': box, 'output_dir': rel(outdir, root),
                            'vinardock_log': rel(log, root),
                            'receptor_file': receptor_path.name,
                            'receptor_path': str(receptor_path),
                            'per_ligand': results}
        state['status'] = 'ok'
    except (OSError, ValueError, RuntimeError, KeyError, IndexError,
            subprocess.SubprocessError) as error:
        state['error'] = str(error)
    state['finished'] = now()
    write_status(stage / 'status.json', state)
    print_state(stage, state)
    return 0 if state['status'] == 'ok' else 1


# ---------------------------------------------------------------- analyse

def convert(executable, source, dest, timeout):
    result = subprocess.run([str(executable), str(source), '-O', str(dest)],
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode or not dest.is_file():
        raise RuntimeError('obabel-vinardock conversion failed: ' + result.stderr[-400:])


def selected_model(source, destination, number):
    for index, block in enumerate(models(source), 1):
        if index == number:
            if not block or (block[0].startswith('MODEL')
                             and not block[-1].startswith('ENDMDL')):
                raise ValueError('model number not found or unterminated')
            destination.write_text(''.join(block))
            return
    raise ValueError('model number not found')


def make_complex(rec, lig, output):
    receptor = rec.read_text(errors='replace').splitlines()
    ligand = lig.read_text(errors='replace').splitlines()
    receptor_atoms = [line for line in receptor if line.startswith(('ATOM  ', 'HETATM'))]
    ligand_atoms = [line for line in ligand if line.startswith(('ATOM  ', 'HETATM'))]
    if not receptor_atoms or not ligand_atoms:
        raise ValueError('empty receptor or ligand after PDB conversion')
    taken = {line[21:22] for line in receptor_atoms}
    chain = next((c for c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
                  if c not in taken), None)
    if chain is None:
        raise ValueError('no unused chain ID available for ligand')
    fixed = []
    ids = set()
    for line in ligand_atoms:
        line = line.ljust(80)
        line = 'HETATM' + line[6:21] + chain + line[22:]
        ids.add((line[17:20].strip(), chain, line[22:26].strip()))
        fixed.append(line)
    output.write_text('\n'.join(receptor_atoms + ['TER'] + fixed + ['END']) + '\n')
    return ids


def parse_xml(path, ligand_ids):
    tree = ET.parse(path)
    sites = tree.findall('.//bindingsite')
    if not sites:
        raise ValueError('PLIP reported 0 binding sites')
    matches = []
    for site in sites:
        ident = site.find('identifiers')
        if ident is None:
            continue
        key = tuple((ident.findtext(label) or '').strip() for label in ('hetid', 'chain', 'position'))
        if key in ligand_ids:
            matches.append(site)
    if len(matches) != 1:
        raise ValueError(f'PLIP found {len(matches)} sites matching target ligand {sorted(ligand_ids)}')
    site = matches[0]
    counts = {}
    residues = []
    for tag, label in CATEGORIES.items():
        nodes = site.findall('./interactions/' + tag + '/*')
        counts[tag] = len(nodes)
        for node in nodes:
            fields = [node.findtext(key, '') for key in ('reschain', 'restype', 'resnr')]
            residues.append((label, ':'.join(fields)))
    return counts, residues


def analyse_ligand(name, info, ctx):
    """Per-ligand pose extraction, complex build, and PLIP run.
    `ctx` carries the shared run context: single, pose, obabel, plip,
    timeout, stage, outdir, rec_fallback."""
    if ctx.pose < 1 or ctx.pose > info['nconfs']:
        raise ValueError(f'{name}: pose {ctx.pose} outside 1..{info["nconfs"]}')
    output = ligand_output(ctx.outdir, name, ctx.single)
    model_pdbt = ctx.stage / (name + '_pose.pdbt')
    selected_model(output, model_pdbt, ctx.pose)
    ligand_pdb = ctx.stage / (name + '_ligand.pdb')
    convert(ctx.obabel, model_pdbt, ligand_pdb, 300)
    modified = sorted(output.parent.glob('*_modified.pdbt'))
    rec = modified[0] if modified else ctx.rec_fallback
    receptor_pdb = ctx.stage / (name + '_receptor.pdb')
    convert(ctx.obabel, rec, receptor_pdb, 300)
    complex_pdb = ctx.stage / (name + '_complex.pdb')
    ligand_ids = make_complex(receptor_pdb, ligand_pdb, complex_pdb)
    cmd = [str(ctx.plip), '-f', str(complex_pdb), '-o', str(ctx.stage), '-x', '-t',
           '--model', '1', '--name', name + '_report']
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=ctx.timeout)
    plip_log = ctx.stage / (name + '_plip.log')
    plip_log.write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f'PLIP failed for {name}: {result.stderr[-400:]}')
    xml = ctx.stage / (name + '_report.xml')
    txt = ctx.stage / (name + '_report.txt')
    if not xml.is_file() or not txt.is_file():
        raise RuntimeError(f'PLIP reports missing for {name}')
    counts, residues = parse_xml(xml, ligand_ids)
    artifacts = [model_pdbt, ligand_pdb, receptor_pdb, complex_pdb, xml, txt, plip_log]
    return name, counts, residues, artifacts


def provenance(manifest_tools, run, root):
    lines = ['## Provenance', '']
    for name in ('vinardock', 'obabel-vinardock'):
        tool = manifest_tools.get(name, {})
        lines.append(f'- {name}: `{tool.get("path", "UNKNOWN")}` '
                     f'version=`{tool.get("version_line", "N/A")}` source=`{tool.get("source", "UNKNOWN")}`')
        if tool.get('note'):
            lines.append(f'  - note: {tool["note"]}')
    plip = manifest_tools.get('plip', {})
    lines.append(f'- PLIP: `{plip.get("path", "UNKNOWN")}` version=`{plip.get("version", "UNKNOWN")}` '
                 f'Open Babel bindings=`{plip.get("openbabel_bindings", "UNKNOWN")}`')
    for name, item in manifest_tools.get('param', {}).items():
        lines.append(f'- parameter {name}: `{item.get("path", "UNKNOWN") if isinstance(item, dict) else item}`')
    config_rel = rel((root / run['output_dir']).parent / 'config.txt', root)
    lines.append(f'- recipe: `{run["recipe"]}`')
    lines.append(f'- exact config: `{config_rel}`; seed={run["seed"]}; threads={run["threads"]}')
    lines.append(f'- box: `{json.dumps(run["resolved_box"])}`')
    lines.append('')
    lines.append('```')
    lines += ((root / run['output_dir']).parent / 'config.txt').read_text().splitlines()
    lines.append('```')
    lines.append('')
    return lines


def stage_analyse(run_dir, pose=1, threads=1, tools_dir=TOOLS, timeout=600):
    root = run_dir.resolve()
    stage = root / 'analysis'
    stage.mkdir(parents=True, exist_ok=True)
    state = new_status('analyse')
    try:
        # a stale report must never survive any failed analysis attempt
        (root / 'report.md').unlink(missing_ok=True)
        dock = read_status(root, 'dock')
        if not dock or dock['status'] != 'ok':
            raise ValueError('dock/status.json is not ok')
        tools = tools_dir.expanduser().resolve()
        obabel = tools / 'bin/obabel-vinardock'
        plip = tools / 'plip-venv/bin/plip'
        if not obabel.is_file() or not plip.is_file():
            raise FileNotFoundError('obabel-vinardock or PLIP not installed')
        run = dock['metrics']
        outdir = root / run['output_dir']
        install = {}
        install_path = tools / 'install.json'
        if install_path.is_file():
            install = json.loads(install_path.read_text())
        single = len(run['per_ligand']) == 1
        rec_fallback = Path(run.get('receptor_path',
                                    root / 'prep' / run.get('receptor_file', 'receptor.pdbt')))
        lines = ['# Docking report', ''] + provenance(install, run, root)
        prep_metrics = (read_status(root, 'prepare') or {}).get('metrics') or {}
        if 'residues_kept' in prep_metrics or 'residues_dropped' in prep_metrics:
            lines += ['## Receptor residues', '',
                      'Non-standard residues in the prepared receptor (standard amino acids omitted):',
                      '',
                      '- kept: ' + (format_counts(prep_metrics.get('residues_kept') or {}) or 'none'),
                      '- dropped: ' + (format_counts(prep_metrics.get('residues_dropped') or {}) or 'none'),
                      '']
        lines += ['## Ligands', '', '| Ligand | Best score (kcal/mol) | Conformers | Analysed pose |',
                  '|---|---:|---:|---:|']
        ok = {n: i for n, i in run['per_ligand'].items() if i.get('status') != 'failed'}
        for name, info in sorted(ok.items(), key=lambda pair: pair[1]['best']):
            lines.append(f'| {name} | {info["best"]:.3f} | {info["nconfs"]} | {pose} |')
        failed = {n: i['error'] for n, i in run['per_ligand'].items() if i.get('status') == 'failed'}
        if failed:
            lines += ['', '### Failed ligands', '']
            for name, error in sorted(failed.items()):
                lines.append(f'- {name}: {error}')
        lines += ['']
        analyzed = {}
        interactions = {}
        failed_analyses = {}
        ctx = SimpleNamespace(single=single, pose=pose, obabel=obabel, plip=plip,
                              timeout=timeout, stage=stage, outdir=outdir,
                              rec_fallback=rec_fallback)
        with ThreadPoolExecutor(max_workers=max(1, min(threads, len(ok)))) as pool:
            futures = {pool.submit(analyse_ligand, name, info, ctx): name
                       for name, info in sorted(ok.items())}
            ligand_results = {}
            for future, name in futures.items():
                try:
                    _, counts, residues, artifacts = future.result()
                    ligand_results[name] = (counts, residues, artifacts)
                except Exception as error:
                    failed_analyses[name] = str(error)
                    state['warnings'].append(f'{name}: analysis failed: {error}')
        if not ligand_results and ok:
            raise RuntimeError('all ligand analyses failed; see warnings')
        # PLIP intermediates (protonated complex + randomly-named plipfixed
        # temp pdb) are regenerable — remove them once every worker is done
        for temp_file in (*stage.glob('*_complex_protonated.pdb'), *stage.glob('plipfixed.*')):
            temp_file.unlink(missing_ok=True)
        if failed_analyses:
            lines += ['### Failed ligand analyses', '']
            for name, error in sorted(failed_analyses.items()):
                lines.append(f'- {name}: {error}')
            lines.append('')
        for name, (counts, residues, artifacts) in sorted(ligand_results.items()):
            info = ok[name]
            analyzed[name] = f'{pose} of {info["nconfs"]}'
            interactions[name] = counts
            if not any(counts.values()):
                state['warnings'].append(f'{name}: PLIP reports zero interactions for the analysed pose')
            lines += [f'### {name}', '', f'- Analysed pose: {analyzed[name]}',
                      '- Model energies (REMARK 980, kcal/mol): ' + ', '.join(
                          f'{index}: {energy:.3f}' for index, energy in enumerate(info['energies'], 1)),
                      f'- TORSDOF: {info["torsdof"] if info["torsdof"] is not None else "N/A"}',
                      f'- Initial-to-final RMSD: {info["rmsd"]}',
                      f'- Complex: `{rel(stage / (name + "_complex.pdb"), root)}`',
                      f'- PLIP: `{rel(stage / (name + "_report.xml"), root)}`, `{rel(stage / (name + "_report.txt"), root)}`', '']
            if 'dG' in info:
                lines.append('- Mutation comparison: ' + ', '.join(f'{key}={val:.3f}' for key, val in info['dG'].items()))
            if 'reference_state' in info:
                lines.append('- Reference-state energies: ' + ', '.join(
                    f'{key}={val:.3f}' for key, val in info['reference_state'].items()))
            lines += ['| Interaction | Count | Residues |', '|---|---:|---|']
            for tag, label in CATEGORIES.items():
                values = sorted({res for category, res in residues if category == label})
                lines.append(f'| {label} | {counts[tag]} | {", ".join(values)} |')
            lines.append('')
            for path in artifacts:
                state['artifacts'].append(rel(path, root))
        log_path = root / run.get('vinardock_log', 'dock/vinardock.log')
        lines += ['## Vinardock log', '', f'`{rel(log_path, root)}` (last 100 lines):', '', '```']
        if log_path.is_file():
            lines += log_path.read_text(errors='replace').splitlines()[-100:]
        lines += ['```', '']
        report = root / 'report.md'
        report.write_text('\n'.join(lines) + '\n')
        state['artifacts'].append(rel(report, root))
        state['metrics'] = {'analyzed_pose': analyzed, 'interactions': interactions,
                            'report': 'report.md', 'pose': pose}
        state['status'] = 'ok'
    except (OSError, ValueError, KeyError, IndexError, RuntimeError, ET.ParseError,
            subprocess.SubprocessError) as error:
        state['error'] = str(error)
    state['finished'] = now()
    write_status(stage / 'status.json', state)
    print_state(stage, state)
    return 0 if state['status'] == 'ok' else 1


# ---------------------------------------------------------------- workflow

def read_recipe(text):
    path = Path(text)
    if not path.is_file():
        path = ROOT / 'recipes' / (text + '.conf')
    if not path.is_file():
        raise ValueError('unknown recipe: ' + text)
    return path.resolve()


def cmd_workflow(args, cli_flags):
    if args.threads < 1:
        raise ValueError('threads must be positive')
    root = args.run_dir.expanduser().resolve()
    tools = args.tools_dir.expanduser().resolve()
    obabel = BUNDLED_OBABEL if BUNDLED_OBABEL.is_file() else tools / 'bin/obabel-vinardock'
    vinardock = BUNDLED_VINARDOCK if BUNDLED_VINARDOCK.is_file() else tools / 'bin/vinardock'
    missing = [str(p) for p in [vinardock, obabel,
                                tools / 'plip-venv/bin/plip',
                                *[tools / 'param' / f for f in PARAM_FILES]]
               if not p.is_file()]
    recipe = read_recipe(args.recipe) if args.recipe else None
    if root.exists() and any(root.iterdir()) and not (root / 'prep/status.json').is_file():
        raise ValueError('run dir is nonempty without prep/status.json; choose a new directory')
    # input paths are passed unresolved; prepare resolves them when it
    # actually runs, so resuming a finished run doesn't require the inputs.
    # Explicit kwargs make the per-stage contract visible in the signatures.
    stages = {'prepare': lambda: stage_prepare(
                  run_dir=root, receptor=args.receptor, ligand=list(args.ligand or []),
                  obabel_vinardock=obabel,
                  drop_hetatm=args.drop_hetatm, keep_metals=args.keep_metals,
                  autobox_ligand=args.autobox_ligand,
                  ligand_from_receptor=args.ligand_from_receptor),
              'dock': lambda: stage_dock(
                  run_dir=root, recipe=str(recipe) if recipe else None,
                  config=args.config,
                  cli_flags=cli_flags, threads=args.threads, tools_dir=tools,
                  autobox_ligand=args.autobox_ligand, timeout=args.timeout),
              'analyse': lambda: stage_analyse(
                  run_dir=root, pose=args.pose, threads=args.threads,
                  tools_dir=tools)}
    for stage in STAGES:
        if not args.force and stage_done(root, stage):
            print(stage + ': artifacts verified; skipped', flush=True)
            continue
        if missing:
            raise FileNotFoundError('tools not installed: ' + ', '.join(missing) +
                                    ' — run setup.py install first')
        if stages[stage]() != 0:
            state = read_status(root, stage) or {}
            tail = log_tail(root, stage)
            raise RuntimeError(stage + ' failed: ' + str(state.get('error')) +
                               ('\n' + tail if tail else ''))
        print(stage + ': ok', flush=True)
    print('Report: ' + str(root / 'report.md'))
    return 0


def main():
    # parent parsers keep the shared flags defined once — a flag that exists
    # on workflow but not on dock is impossible to miss this way.
    # allow_abbrev=False everywhere: an unknown --flag must fall through to
    # vinardock, never abbreviate to a pipeline flag. Pipeline-owned flags
    # are underscore-spelled like vinardock's own.
    run = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    run.add_argument('--run_dir', type=Path, required=True)
    inputs = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    inputs.add_argument('--prepare_receptor', dest='receptor', type=Path, required=True)
    ligand_source = inputs.add_mutually_exclusive_group(required=True)
    ligand_source.add_argument('--prepare_ligand', dest='ligand', type=Path,
                               action='append')
    ligand_source.add_argument('--ligand_from_receptor', metavar='RES[:CHAIN:SEQ]',
                               help='extract a co-crystal HETATM ligand from the '
                                    'receptor (see the scan subcommand)')
    inputs.add_argument('--prepare_autobox_ligand', dest='autobox_ligand', type=Path,
                        help='raw autobox reference input (prepared like a ligand)')
    docking = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    docking.add_argument('--recipe', default=None,
                         help='recipe name or .conf path (default: standard)')
    docking.add_argument('--config', type=Path,
                         help='vinardock config file, merged after the recipe')
    tools = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    tools.add_argument('--tools_dir', type=Path, default=TOOLS)

    parser = argparse.ArgumentParser(
        prog='pipeline.py', allow_abbrev=False,
        description='Vinardock docking pipeline: prepare -> dock -> analyse. '
                    'dock/workflow also take vinardock flags verbatim, e.g. '
                    '--seed 7 --center_x 10 --swarm.extend_pso 10')
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('prepare', parents=[run, inputs], allow_abbrev=False,
                       help='convert receptor/ligand inputs to pdbt')
    p.add_argument('--obabel_vinardock', type=Path,
                   default=BUNDLED_OBABEL if BUNDLED_OBABEL.is_file()
                           else TOOLS / 'bin/obabel-vinardock')
    p.add_argument('--drop_hetatm', action='store_true')
    p.add_argument('--keep_metals', action='store_true',
                   help='with --drop_hetatm: keep metal ions (standard receptor prep)')
    p.add_argument('--timeout', type=int, default=300, help='per-conversion timeout (s)')

    p = sub.add_parser('dock', parents=[run, docking, tools], allow_abbrev=False,
                       help='run vinardock on prepared inputs')
    p.add_argument('--prepare_autobox_ligand', dest='autobox_ligand', type=Path)
    p.add_argument('--timeout', type=int, default=21600, help='vinardock timeout (s)')

    p = sub.add_parser('scan', allow_abbrev=False,
                       help='list dockable HETATM molecules in a receptor pdb')
    p.add_argument('receptor', type=Path)

    p = sub.add_parser('analyse', parents=[run, tools], allow_abbrev=False,
                       help='PLIP interaction analysis + report.md')
    p.add_argument('--pose', type=int, default=1)
    p.add_argument('--threads', type=int, default=os.cpu_count() or 1)
    p.add_argument('--timeout', type=int, default=600, help='per-ligand PLIP timeout (s)')

    p = sub.add_parser('workflow', parents=[run, inputs, docking, tools],
                       allow_abbrev=False, help='run the gated pipeline end to end')
    p.add_argument('--drop_hetatm', action='store_true')
    p.add_argument('--keep_metals', action='store_true',
                   help='with --drop_hetatm: keep metal ions (standard receptor prep)')
    p.add_argument('--pose', type=int, default=1)
    # --threads doubles as vinardock's --threads (injected as a script
    # default) and the PLIP pool size — the only flag both worlds share
    p.add_argument('--threads', type=int, default=os.cpu_count() or 1)
    p.add_argument('--timeout', type=int, default=21600, help='vinardock timeout (s)')
    p.add_argument('--force', action='store_true')
    args, extra = parser.parse_known_args()
    cli_flags = {}
    if args.command in ('dock', 'workflow'):
        try:
            cli_flags = parse_passthrough(extra)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
    elif extra:
        print('unrecognized arguments: ' + ' '.join(extra), file=sys.stderr)
        return 2

    handlers = {'prepare': stage_prepare, 'dock': stage_dock,
                'analyse': stage_analyse}
    try:
        if args.command == 'scan':
            lines = args.receptor.expanduser().resolve(strict=True).read_text(
                errors='replace').splitlines()
            groups = hetatm_groups(lines)
            for g in groups:
                print('{}:{}:{} atoms={}'.format(g['resname'], g['chain'] or '-',
                                                 g['resseq'], g['atoms']))
            if not groups:
                print('no dockable HETATM candidates')
            return 0
        if args.command == 'workflow':
            return cmd_workflow(args, cli_flags)
        # subparser arg names match the stage kwargs exactly
        kwargs = {k: v for k, v in vars(args).items() if k != 'command'}
        if args.command == 'dock':
            kwargs['cli_flags'] = cli_flags
        return handlers[args.command](**kwargs)
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        print('internal error: ' + repr(error), file=sys.stderr)
        return 3


if __name__ == '__main__':
    sys.exit(main())
