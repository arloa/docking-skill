#!/usr/bin/env python3
import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FLAG = re.compile(r'^--([\w.]+)(?:\s+|=)?(.*)$')
SLOT = re.compile(r'\{([a-zA-Z_][\w]*)(?::([^{}]*))?\}')
PROTECTED = {'receptor', 'ligand', 'out', 'scoring', 'scoring.table', 'scoring.tableTxT',
             'seed', 'threads', 'conformations'}


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def artifact(path, root):
    return {'path': str(path.relative_to(root)), 'sha256': digest(path)}


def status(path, data):
    temp = path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(data, indent=2) + '\n')
    os.replace(temp, path)


def kv_pairs(pairs):
    result = {}
    for item in pairs:
        key, separator, value = item.partition('=')
        if not separator or not re.fullmatch(r'[\w.]+', key) or any(c in value for c in '\n\r#;'):
            raise ValueError('expected safe name=value, got ' + repr(item))
        result[key] = value
    return result


def recipe_flags(recipe, slots):
    flags = OrderedDict()
    requires = []
    for number, line in enumerate(recipe.read_text().splitlines(), 1):
        body, _, comment = line.partition('#')
        stripped = line.strip()
        if stripped.startswith('# requires:'):
            requires.append(stripped.split(':', 1)[1].strip().split()[0])
        if stripped.startswith('# unavailable:'):
            raise ValueError(stripped.split(':', 1)[1].strip())
        if not body.strip():
            continue
        guard = re.search(r'if-(set|unset):\s*(\w+)', comment)
        if guard and bool(slots.get(guard.group(2))) != (guard.group(1) == 'set'):
            continue
        def fill(match):
            value = slots.get(match.group(1)) or match.group(2)
            if value is None or not str(value).strip():
                raise ValueError(f'{recipe.name}:{number}: missing slot {match.group(1)}')
            return str(value)
        entry = SLOT.sub(fill, body.strip())
        match = FLAG.fullmatch(entry)
        if not match:
            raise ValueError(f'{recipe.name}:{number}: invalid flag line')
        if match.group(1) in PROTECTED:
            raise ValueError('recipe cannot set protected flag --' + match.group(1))
        flags[match.group(1)] = match.group(2).strip()
    return flags, requires


def model_data(path):
    energies = []
    torsdof = None
    inside = False
    for line in path.read_text(errors='replace').splitlines():
        if line.startswith('MODEL'):
            inside = True
        elif line.startswith('ENDMDL'):
            inside = False
        elif inside and line.startswith('REMARK 980'):
            match = re.search(r':\s*([-+]?\d+(?:\.\d+)?)', line)
            if match:
                energies.append(float(match.group(1)))
        elif inside and line.startswith('TORSDOF'):
            torsdof = int(line.split()[1])
    return energies, torsdof


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--recipe', required=True)
    parser.add_argument('--seed', required=True, type=int)
    parser.add_argument('--threads', type=int, default=os.cpu_count() or 1)
    parser.add_argument('--conformations', type=int, default=9)
    parser.add_argument('--slot', action='append', default=[])
    parser.add_argument('--set', dest='settings', action='append', default=[])
    parser.add_argument('--tools-dir', type=Path, default=Path.home() / '.local/share/docking-tools')
    args = parser.parse_args()
    root = args.run_dir.resolve()
    stage = root / 'dock'
    stage.mkdir(parents=True, exist_ok=True)
    started = now()
    state = {'stage': 'run', 'status': 'failed', 'started': started, 'finished': started,
             'artifacts': [], 'metrics': {}, 'warnings': [], 'error': None}
    try:
        if args.seed < 0 or args.threads < 1 or args.conformations < 1:
            raise ValueError('seed must be non-negative; threads and conformations must be positive')
        tools = args.tools_dir.expanduser().resolve()
        executable = tools / 'bin/vinardock'
        if not executable.is_file():
            raise FileNotFoundError(executable)
        prep = json.loads((root / 'prep/status.json').read_text())
        if prep['status'] != 'ok':
            raise ValueError('prep/status.json is not ok')
        receptor = root / 'prep' / prep['metrics'].get('receptor_file', 'receptor.pdbt')
        ligands = sorted((root / 'prep/ligands').glob('*.pdbt'))
        if not ligands or not receptor.is_file():
            raise ValueError('prepared receptor or ligands missing')
        for item in prep['artifacts']:
            source = root / item['path']
            if not source.is_file() or digest(source) != item['sha256']:
                raise ValueError('prepared artifact hash mismatch: ' + item['path'])
        names = [path.stem for path in ligands]
        if names != sorted(prep['metrics']['ligands']):
            raise ValueError('prepared ligand list changed')
        recipe = Path(args.recipe)
        if not recipe.is_file():
            recipe = ROOT / 'recipes' / (args.recipe + '.conf')
        recipe = recipe.resolve(strict=True)
        slots = {'tools_dir': str(tools), 'param_dir': str(tools / 'param'),
                 'run_dir': str(root), 'prep_dir': str(root / 'prep'),
                 'dock_dir': str(stage), 'seed': str(args.seed),
                 'threads': str(args.threads), 'conformations': str(args.conformations)}
        slots.update(kv_pairs(args.slot))
        explicit = kv_pairs(args.settings)
        if PROTECTED & explicit.keys():
            raise ValueError('protected base flags cannot be overridden: ' + ', '.join(PROTECTED & explicit.keys()))
        recipe_settings, requires = recipe_flags(recipe, slots)
        preliminary = OrderedDict({'receptor': str(receptor), 'ligand': str(ligands[0] if len(ligands) == 1 else root / 'prep/ligands'),
                                   'scoring': '2vinardo', 'scoring.table': str(tools / 'param/param.dat'),
                                   'scoring.tableTxT': str(tools / 'param/param.TxT.dat'),
                                   'seed': str(args.seed), 'threads': str(args.threads),
                                   'conformations': str(args.conformations), 'calc_lig_rmsd': ''})
        for name, value in recipe_settings.items():
            preliminary[name] = value
        for name, value in explicit.items():
            if name in recipe_settings and recipe_settings[name] != value:
                state['warnings'].append(f'Explicit --{name} overrides recipe value')
            preliminary[name] = value
        if any(require != 'box' for require in requires):
            raise ValueError('unsupported recipe requirement: ' + ', '.join(requires))
        manual = all(k in preliminary for k in ('center_x', 'center_y', 'center_z', 'size_x', 'size_y', 'size_z'))
        autobox = all(k in preliminary for k in ('autobox', 'autobox_ligand'))
        if manual == autobox or any(k in preliminary for k in ('center_x', 'center_y', 'center_z', 'size_x', 'size_y', 'size_z')) and autobox:
            raise ValueError('specify either center_x/y/z + size_x/y/z or autobox + autobox_ligand')
        if 'flexres.res' in preliminary and 'flexres.autoflex' in preliminary:
            raise ValueError('flexres.res and flexres.autoflex are mutually exclusive')
        if recipe.stem == 'flexible' and not {'flexres.res', 'flexres.autoflex'} & preliminary.keys():
            raise ValueError('flexible recipe needs --slot flexres=... or --set flexres.autoflex=<user cutoff>')
        if 'flexres.res' in preliminary and any(
                match.group(1) != match.group(2) for match in re.finditer(
                    r'\b[A-Z]:([A-Z])\d+([A-Z])\b', preliminary['flexres.res'])):
            preliminary['rotamer_lib'] = str(tools / 'param/dun2010bbdep.bin')
        signature = hashlib.sha256(json.dumps({'flags': preliminary, 'recipe': digest(recipe),
            'inputs': prep['metrics']['ligand_hashes'], 'vinardock': digest(executable),
            'receptor': digest(receptor),
            'tables': {p.name: digest(p) for p in (tools / 'param').iterdir() if p.is_file()}},
            sort_keys=True).encode()).hexdigest()[:12]
        attempt = stage / 'attempts' / signature
        sequence = 0
        while attempt.exists():
            sequence += 1
            attempt = stage / 'attempts' / (signature + '-' + str(sequence))
        attempt.mkdir(parents=True)
        outdir = attempt / 'DOCK'
        preliminary['out'] = str(outdir)
        config = stage / 'config.txt'
        config.write_text(''.join('--' + key + (' ' + val if val else '') + '\n'
                                  for key, val in preliminary.items()))
        state['artifacts'].append(artifact(config, root))
        log = stage / 'vinardock.log'
        with log.open('w') as stream:
            proc = subprocess.run([str(executable), '--config', str(config)], cwd=attempt,
                                  stdout=stream, stderr=subprocess.STDOUT)
        state['artifacts'].append(artifact(log, root))
        if proc.returncode:
            raise RuntimeError(f'vinardock exited {proc.returncode}; see dock/vinardock.log')
        csvpath = outdir / 'log.csv'
        if not csvpath.is_file():
            raise RuntimeError('vinardock did not write log.csv')
        with csvpath.open(newline='') as stream:
            rows = list(csv.reader(stream))
        if not rows:
            raise RuntimeError('log.csv is empty')
        if len(rows) - 1 != len(ligands):
            raise RuntimeError(f'log.csv has {len(rows)-1} rows, expected {len(ligands)}')
        if rows[0][0] != 'Ligand':
            raise RuntimeError('unsupported log.csv header')
        results = {}
        for row in rows[1:]:
            if row[0] not in names or row[0] in results:
                raise RuntimeError('unexpected or duplicate ligand in log.csv: ' + row[0])
            dg_mode = rows[0][1:4] == ['E_WT', 'E_MUT', 'dG']
            if not dg_mode and rows[0][1] != 'nConfs':
                raise RuntimeError('unsupported log.csv mode: ' + ','.join(rows[0]))
            count = None if dg_mode else int(row[1])
            if dg_mode and (len(row) < 4 or row[3] == 'N/A') or count is not None and count <= 0:
                raise RuntimeError('vinardock failed on ligand: ' + row[0])
            name = row[0]
            output = (outdir / (name + '.pdbt')) if len(ligands) == 1 else outdir / name[:2] / name / (name + '.pdbt')
            if not output.is_file():
                raise RuntimeError('missing output for ligand: ' + name)
            energies, torsdof = model_data(output)
            if count is None:
                count = len(energies)
            if count < 1 or len(energies) != count:
                raise RuntimeError(f'{name}: {count} conformers in log.csv but {len(energies)} energies in pdbt')
            results[name] = {'nconfs': count, 'energies': energies, 'best': energies[0],
                             'torsdof': torsdof, 'rmsd': row[-1] if len(row) > 3 and not dg_mode else 'N/A'}
            if dg_mode:
                results[name]['dG'] = {key: float(value) for key, value in zip(rows[0][1:], row[1:4])}
            elif 'E_corrected' in rows[0]:
                results[name]['reference_state'] = {
                    key: float(row[rows[0].index(key)])
                    for key in ('binding_energy', 'BE_ligwater', 'BE_recwater', 'E_corrected')}
            state['artifacts'].append(artifact(output, root))
        state['artifacts'].append(artifact(csvpath, root))
        for path in outdir.rglob('*_modified.pdbt'):
            state['artifacts'].append(artifact(path, root))
        logtext = log.read_text(errors='replace')
        match = re.search(r'Search box: center =\s*([^\n]+?), size =\s*([^\n]+)', logtext)
        if match:
            box = {'center': [float(s) for s in match.group(1).split(',')],
                   'size': [float(s) for s in match.group(2).split(',')]}
        else:
            box = {'center': [float(preliminary['center_' + c]) for c in 'xyz'],
                   'size': [float(preliminary['size_' + c]) for c in 'xyz']} if 'center_x' in preliminary else None
        state['metrics'] = {'recipe': {'name': recipe.stem, 'sha256': digest(recipe)},
                            'seed': args.seed, 'threads': args.threads, 'resolved_box': box,
                            'config_sha256': digest(config), 'output_dir': str(outdir.relative_to(root)),
                            'ligand_hashes': prep['metrics']['ligand_hashes'],
                            'receptor_hash': digest(receptor), 'receptor_file': receptor.name,
                            'per_ligand': results}
        state['status'] = 'ok'
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
        state['error'] = str(error)
    state['finished'] = now()
    status(stage / 'status.json', state)
    print(json.dumps({'status': state['status'], 'error': state['error'], 'metrics': state['metrics']}))
    return 0 if state['status'] == 'ok' else 1


if __name__ == '__main__':
    sys.exit(main())
