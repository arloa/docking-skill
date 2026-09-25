#!/usr/bin/env python3
import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGES = ('prepare', 'run', 'analyse')
FOLDERS = {'prepare': 'prep', 'run': 'dock', 'analyse': 'analysis'}


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path, data):
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    os.replace(temporary, path)


def tool_info(tools):
    installed = tools / 'install.json'
    if not installed.is_file():
        raise FileNotFoundError('run setup.py install first: ' + str(installed))
    info = json.loads(installed.read_text())
    for name in ('vinardock', 'obabel-vinardock', 'plip'):
        item = info[name]
        file = Path(item['path'])
        if not file.is_file() or digest(file) != item['sha256']:
            raise ValueError('installed tool hash mismatch: ' + name)
    obabel = subprocess.run([info['obabel-vinardock']['path'], '-V'],
                            capture_output=True, text=True, timeout=20)
    if obabel.returncode or not obabel.stdout.startswith('Open Babel'):
        raise RuntimeError('obabel-vinardock is installed but cannot execute: ' + obabel.stderr.strip())
    vinardock = subprocess.run([info['vinardock']['path'], '--help'],
                               capture_output=True, text=True, timeout=20)
    if not vinardock.stdout.startswith('2Vinardo molecular docking program'):
        raise RuntimeError('vinardock is installed but cannot execute: ' + vinardock.stderr.strip())
    for name, item in info['param'].items():
        if not Path(item['path']).is_file() or digest(Path(item['path'])) != item['sha256']:
            raise ValueError('parameter table hash mismatch: ' + name)
    return info


def check_gate(root, stage):
    path = root / FOLDERS[stage] / 'status.json'
    if not path.is_file():
        raise ValueError(stage + '/status.json missing')
    result = json.loads(path.read_text())
    if result.get('stage') != stage or result.get('status') != 'ok':
        raise ValueError(stage + ' failed: ' + str(result.get('error')))
    for item in result['artifacts']:
        file = (root / item['path']).resolve(strict=True)
        if not file.is_relative_to(root) or digest(file) != item['sha256']:
            raise ValueError(stage + ' artifact hash mismatch: ' + item['path'])
    if stage == 'run':
        prep = check_gate(root, 'prepare')
        metrics = result['metrics']
        if (metrics['ligand_hashes'] != prep['metrics']['ligand_hashes'] or
                metrics['receptor_hash'] != digest(root / 'prep' /
                    prep['metrics'].get('receptor_file', 'receptor.pdbt'))):
            raise ValueError('run inputs differ from current preparation')
    if stage == 'analyse':
        check_gate(root, 'run')
        if result['metrics']['dock_status_sha256'] != digest(root / 'dock/status.json'):
            raise ValueError('analysis is for a different docking result')
    return result


def update_manifest(root, stage, data):
    path = root / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['stages'][stage] = 'ok'
    if stage == 'run':
        metrics = data['metrics']
        manifest['run']['config_sha256'] = metrics['config_sha256']
        manifest['run']['resolved_box'] = metrics['resolved_box']
        manifest['run']['output_dir'] = metrics['output_dir']
    atomic_json(path, manifest)


def read_recipe(text):
    path = Path(text)
    if not path.is_file():
        path = ROOT / 'recipes' / (text + '.conf')
    if not path.is_file():
        raise ValueError('unknown recipe: ' + text)
    return path.resolve()


def input_ref(source):
    source = source.expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError('not an input file: ' + str(source))
    return {'path': str(source), 'sha256': digest(source)}


def initialise(args, tools, recipe, root):
    root.mkdir(parents=True, exist_ok=True)
    receptor = input_ref(args.receptor)
    ligands = [input_ref(path) for path in args.ligand]
    slots = list(args.slot)
    settings = list(args.settings)
    if any(value.startswith('autobox_ligand=') for value in settings):
        raise ValueError('use --autobox-ligand <file> so the reference is hashed')
    if args.autobox_ligand:
        ref = input_ref(args.autobox_ligand)
        settings += ['autobox_ligand=' + ref['path']]
    else:
        ref = None
    cpu = ''
    cpuinfo = Path('/proc/cpuinfo')
    if cpuinfo.is_file():
        cpu = next((line.split(':', 1)[1].strip() for line in cpuinfo.read_text().splitlines()
                    if line.startswith('model name')), '')
    manifest = {
        'created': datetime.now(timezone.utc).isoformat(),
        'tools': tools,
        'env': {'cpu': cpu or platform.processor(), 'nproc': os.cpu_count(),
                'threads': args.threads, 'hostname': socket.gethostname(), 'python': platform.python_version()},
        'inputs': {'receptor': receptor, 'ligands': ligands, 'autobox_ref': ref},
        'run': {'seed': args.seed, 'threads': args.threads, 'conformations': args.conformations,
                'recipe': {'name': recipe.stem, 'sha256': digest(recipe), 'path': str(recipe)},
                'settings': settings, 'slots': slots, 'drop_hetatm': args.drop_hetatm,
                'config_sha256': None, 'resolved_box': None},
        'stages': {}
    }
    atomic_json(root / 'manifest.json', manifest)
    return manifest


def validate_resume(args, root, tools, recipe):
    manifest = json.loads((root / 'manifest.json').read_text())
    existing = manifest['run']
    expected = {'seed': args.seed, 'threads': args.threads,
                'conformations': args.conformations, 'settings': args.settings,
                'slots': args.slot, 'drop_hetatm': args.drop_hetatm}
    if args.autobox_ligand:
        ref = manifest['inputs']['autobox_ref']
        expected['settings'] += ['autobox_ligand=' + ref['path']]
        if input_ref(args.autobox_ligand) != ref:
            raise ValueError('autobox reference changed; use a new run dir')
    for key, value in expected.items():
        if existing[key] != value:
            raise ValueError(key + ' changed; use a new run dir')
    if existing['recipe']['sha256'] != digest(recipe) or tools != manifest['tools']:
        raise ValueError('recipe or tools changed; use a new run dir')
    for key, originals in [('receptor', [args.receptor]), ('ligands', args.ligand)]:
        stored = [manifest['inputs'][key]] if key == 'receptor' else manifest['inputs'][key]
        if len(stored) != len(originals) or any(
                input_ref(path) != item for path, item in zip(originals, stored)):
            raise ValueError('input contents changed; use a new run dir')
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', required=True, type=Path)
    parser.add_argument('--receptor', required=True, type=Path)
    parser.add_argument('--ligand', required=True, action='append', type=Path)
    parser.add_argument('--recipe', required=True)
    parser.add_argument('--seed', required=True, type=int)
    parser.add_argument('--threads', type=int, default=os.cpu_count() or 1)
    parser.add_argument('--conformations', type=int, default=9)
    parser.add_argument('--set', dest='settings', action='append', default=[])
    parser.add_argument('--slot', action='append', default=[])
    parser.add_argument('--autobox-ligand', type=Path)
    parser.add_argument('--drop-hetatm', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--tools-dir', type=Path, default=Path.home() / '.local/share/docking-tools')
    args = parser.parse_args()
    if args.seed < 0 or args.threads < 1 or args.conformations < 1:
        parser.error('seed must be non-negative; threads and conformations positive')
    root = args.run_dir.expanduser().resolve()
    tools_dir = args.tools_dir.expanduser().resolve()
    tools = tool_info(tools_dir)
    recipe = read_recipe(args.recipe)
    if (root / 'manifest.json').exists():
        manifest = validate_resume(args, root, tools, recipe)
    elif root.exists() and any(root.iterdir()):
        raise ValueError('run dir is nonempty without a manifest; choose a new directory')
    else:
        manifest = initialise(args, tools, recipe, root)
    receptor = manifest['inputs']['receptor']['path']
    ligands = [entry['path'] for entry in manifest['inputs']['ligands']]
    commands = {
        'prepare': [sys.executable, str(ROOT / 'scripts/prepare.py'), '--run-dir', str(root),
                    '--receptor', receptor, '--obabel-vinardock', tools['obabel-vinardock']['path'],
                    *[part for path in ligands for part in ('--ligand', path)],
                    *(['--autobox-ligand', manifest['inputs']['autobox_ref']['path']]
                      if manifest['inputs']['autobox_ref'] else []),
                    *(['--drop-hetatm'] if args.drop_hetatm else [])],
        'run': [sys.executable, str(ROOT / 'scripts/run.py'), '--run-dir', str(root),
                '--recipe', str(recipe), '--seed', str(args.seed), '--threads', str(args.threads),
                '--conformations', str(args.conformations), '--tools-dir', str(tools_dir),
                *['--set=' + ('autobox_ligand=' + str(root / 'prep' / (
                    Path(manifest['inputs']['autobox_ref']['path']).stem + '_autobox.pdbt'))
                    if value.startswith('autobox_ligand=') else value)
                  for value in manifest['run']['settings']],
                *['--slot=' + value for value in args.slot]],
        'analyse': [sys.executable, str(ROOT / 'scripts/analyse.py'), '--run-dir', str(root),
                    '--tools-dir', str(tools_dir)]}
    for stage in STAGES:
        if not args.force:
            try:
                data = check_gate(root, stage)
                update_manifest(root, stage, data)
                print(stage + ': verified existing artifacts; skipped', flush=True)
                continue
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                pass
        result = subprocess.run(commands[stage], capture_output=True, text=True)
        if result.stdout:
            print(result.stdout.strip(), flush=True)
        try:
            data = check_gate(root, stage)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            log = root / ('prep/prep.log' if stage == 'prepare' else 'dock/vinardock.log')
            tail = '\n'.join(log.read_text(errors='replace').splitlines()[-20:]) if log.is_file() else result.stderr[-1000:]
            raise RuntimeError(stage + ' gate failed: ' + str(error) + '\n' + tail) from error
        update_manifest(root, stage, data)
        print(stage + ': ok', flush=True)
    print('Report: ' + str(root / 'report.md'))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        sys.exit(str(error))
