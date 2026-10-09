#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = Path.home() / '.local/share/docking-tools'
PARAM_FILES = ('param.dat', 'param.TxT.dat', 'dun2010bbdep.bin')


def candidate(path, name):
    """Smoke-test a binary: must exist, be executable, and actually launch."""
    if not path or not path.is_file() or not os.access(path, os.X_OK):
        return None
    path = path.resolve()
    flags = ['-V'] if name == 'obabel-vinardock' else ['--help']
    result = subprocess.run([str(path), *flags], capture_output=True, text=True, timeout=20)
    if name == 'obabel-vinardock' and (result.returncode != 0 or not result.stdout.startswith('Open Babel')):
        raise RuntimeError(f'{path} cannot run: {result.stderr.strip()[:300]}')
    if name == 'vinardock' and not result.stdout.startswith('2Vinardo molecular docking program'):
        raise RuntimeError(f'{path} cannot run: {result.stderr.strip()[:300]}')
    version = (result.stdout + result.stderr).splitlines()
    return {'path': str(path), 'version_line': version[0] if version else ''}


def probe():
    result = {}
    unusable = {}
    for name in ('vinardock', 'obabel-vinardock'):
        paths = [ROOT / 'bin' / name, Path.cwd() / name,
                 Path(shutil.which(name)) if shutil.which(name) else None,
                 TOOLS / 'bin' / name]
        seen = set()
        found = []
        for path in paths:
            if path and path.is_file() and path.resolve() not in seen:
                seen.add(path.resolve())
                try:
                    item = candidate(path, name)
                    if item:
                        found.append(item)
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                    unusable.setdefault(name, []).append(
                        {'path': str(path.resolve()), 'error': str(error)})
        result[name] = found
    result['unusable'] = unusable
    result['param'] = [str(directory.resolve())
                       for directory in (ROOT / 'param', Path.cwd() / 'param', TOOLS / 'param')
                       if directory.is_dir() and all((directory / name).is_file() for name in PARAM_FILES)]
    result['plip'] = (TOOLS / 'plip-venv/bin/plip').is_file()
    found = shutil.which('vinardock-pipeline')
    result['launcher'] = str(Path(found).resolve()) if found else None
    return result


def launcher_dir():
    """Where to link vinardock-pipeline: ~/.local/bin when on PATH, else
    /usr/local/bin when writable, else ~/.local/bin with a PATH warning.
    Arbitrary writable PATH dirs are never picked — a stray link in an
    unrelated app's bin dir is worse than a PATH warning."""
    local = Path.home() / '.local/bin'
    dirs = {Path(d) for d in os.environ.get('PATH', '').split(os.pathsep) if d}
    if local in dirs:
        return local, None
    system = Path('/usr/local/bin')
    if system in dirs and os.access(system, os.W_OK):
        return system, None
    return local, str(local) + ' is not on PATH — add it or call the script by path'


def make_launcher(replace=False):
    """Write a wrapper that execs the install-time interpreter on
    pipeline.py — a symlink would need pipeline.py itself to be
    executable, which a fresh download does not guarantee."""
    script = (ROOT / 'scripts/pipeline.py').resolve()
    body = f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n'
    found = shutil.which('vinardock-pipeline')
    if found:
        path = Path(found)
        if (path.is_symlink() and path.resolve() == script) or \
           (path.is_file() and not path.is_symlink()
            and path.read_text(errors='replace') == body):
            return {'path': found, 'note': 'already installed'}
    directory, warning = launcher_dir()
    target = directory / 'vinardock-pipeline'
    if target.exists() or target.is_symlink():
        if not replace:
            raise FileExistsError(f'{target} exists; pass --replace to overwrite')
        target.unlink()
    directory.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    target.chmod(0o755)
    result = {'path': str(target)}
    if found and Path(found) != target:
        result['warning'] = f'{found} is earlier on PATH and will shadow this'
    elif warning:
        result['warning'] = warning
    return result


def install_copy(source, target, replace=False):
    """Copy a file into the tools dir; refuses to clobber without --replace."""
    if source.resolve() == target.resolve():
        return
    if target.exists() and not replace:
        raise FileExistsError(f'{target} exists; pass --replace to overwrite')
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temp:
        temp_path = Path(temp.name)
        with source.open('rb') as inp:
            shutil.copyfileobj(inp, temp)
    temp_path.chmod(source.stat().st_mode & 0o777)
    os.replace(temp_path, target)


def install(args):
    (TOOLS / 'bin').mkdir(parents=True, exist_ok=True)
    (TOOLS / 'param').mkdir(parents=True, exist_ok=True)
    summary = {}
    receipt = TOOLS / 'install.json'
    previous = json.loads(receipt.read_text()) if receipt.is_file() else {}
    for name, selected in [('vinardock', args.vinardock),
                           ('obabel-vinardock', args.obabel_vinardock)]:
        target = TOOLS / 'bin' / name
        source = Path(selected).expanduser().resolve()
        if not candidate(source, name):
            raise ValueError('not an executable: ' + str(source))
        same = source == target.resolve()
        install_copy(source, target, replace=args.replace)
        origin = (previous.get(name, {}).get('source') or 'local ' + str(target)) if same \
            else 'local ' + str(source)
        summary[name] = candidate(target, name)
        summary[name]['source'] = origin
    param_dir = Path(args.param).expanduser().resolve()
    for name in PARAM_FILES:
        source = param_dir / name
        if not source.is_file():
            raise FileNotFoundError(source)
        install_copy(source, TOOLS / 'param' / name, replace=args.replace)
        summary.setdefault('param', {})[name] = {'path': str(TOOLS / 'param' / name),
                                                 'source': 'local ' + str(param_dir)}
    plip = TOOLS / 'plip-venv/bin/plip'
    if not args.skip_plip and not plip.is_file():
        uv = shutil.which('uv')
        if not uv:
            raise RuntimeError('uv is required to provision PLIP')
        subprocess.run([uv, 'venv', '--python', sys.executable, str(TOOLS / 'plip-venv')], check=True)
        subprocess.run([uv, 'pip', 'install', '--python', str(TOOLS / 'plip-venv/bin/python'),
                        'plip==3.0.1', 'openbabel==3.2.1', 'numpy==2.2.6', 'lxml==6.1.3'], check=True)
    if plip.is_file():
        py = TOOLS / 'plip-venv/bin/python'
        info = subprocess.check_output([str(py), '-c',
            "import importlib.metadata as m; print(m.version('plip'), m.version('openbabel'))"], text=True).strip().split()
        summary['plip'] = {'path': str(plip),
                           'version': info[0], 'openbabel_bindings': info[1]}
    # regenerate the checked-in help references so they always document the
    # binaries actually installed
    refs = ROOT / 'references'
    for name, command in [('vinardock-help.txt', [str(TOOLS / 'bin/vinardock'), '--help']),
                          ('obabel-vinardock-help.txt', [str(TOOLS / 'bin/obabel-vinardock'), '-H'])]:
        output = subprocess.run(command, capture_output=True, text=True)
        text = output.stdout + output.stderr
        if name == 'vinardock-help.txt':
            text = re.sub(r'(--seed arg[^\n]*?\(default: )\d+(\))', r'\g<1><timestamp>\2', text)
        (refs / name).write_text(text)
    if args.launcher:
        summary['launcher'] = make_launcher(replace=args.replace)
    elif sys.stdin.isatty():
        directory, _ = launcher_dir()
        if input(f'install vinardock-pipeline launcher in {directory}? [y/N] ').strip().lower() == 'y':
            summary['launcher'] = make_launcher(replace=args.replace)
    (TOOLS / 'install.json').write_text(json.dumps(summary, indent=2) + '\n')
    return summary


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('probe')
    install_parser = sub.add_parser('install')
    # the repo ships fully static builds in bin/ — no downloads or host libs
    for name in ('vinardock', 'obabel-vinardock'):
        install_parser.add_argument('--' + name, default=str(ROOT / 'bin' / name),
                                    help='path to a ' + name + ' binary (default: bundled)')
    install_parser.add_argument('--param', default=str(ROOT / 'param'),
                                help='dir containing the param files (default: bundled)')
    install_parser.add_argument('--skip-plip', action='store_true')
    install_parser.add_argument('--launcher', action='store_true',
                                help='symlink vinardock-pipeline into a PATH dir without asking')
    install_parser.add_argument('--replace', action='store_true')
    args = parser.parse_args()
    print(json.dumps(probe() if args.action == 'probe' else install(args), indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
