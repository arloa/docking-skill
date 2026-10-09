#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = Path.home() / '.local/share/docking-tools'
RELEASE = 'https://github.com/arloa/Vinardock-exec/releases/download/v1.0.0/'
SOURCE = 'https://raw.githubusercontent.com/arloa/Vinardock-exec/11caaa8/'
CHECKSUMS = {
    'vinardock-linux-amd64': '9ce6b1f9878b4e19b7861e14b664bdf328019a591c7931d027343e100460c61f',
    'vinardock-linux-amd64-ubuntu22.04': '15ae31b5a21ba848fcf9b560d2a6af50429c876e655050a12dda5db175c8da18',
    'obabel-vinardock-linux-amd64': '6e935b3e3ae569f2e5844e06d82777b1c356e89cfc67dacd030f784b64445523',
    'obabel-vinardock-linux-amd64-ubuntu22.04': '68a97cd71881a37d6deeabec042318a342a89c1bc5d3aeacbaa1aff8193423b1',
}
# (modern asset, glibc<min fallback asset, minimum glibc). The ubuntu22.04
# builds exist because the modern binaries link against newer glibc symbols.
ASSETS = {
    'vinardock': ('vinardock-linux-amd64', 'vinardock-linux-amd64-ubuntu22.04', (2, 39)),
    'obabel-vinardock': ('obabel-vinardock-linux-amd64', 'obabel-vinardock-linux-amd64-ubuntu22.04', (2, 38)),
}
PARAM_HASHES = {
    'param.dat': 'd6ae4eb72dd94d228eaaebe14a50762d54ae8a0e19303e20d1d0195bfcd13ae7',
    'param.TxT.dat': 'bba8ee8a0ccd702cf46d37a6d2680da3dfaec4d445b8d2b7e65d162d9cb8704c',
    'dun2010bbdep.bin': 'ed3f7be5f33b5fa947ac5e83cb024c6a6af6440bb50a1c8073aacabe6d792d0e',
}


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def libc_version():
    version = platform.libc_ver()[1]
    parts = version.split('.')[:2]
    if len(parts) == 2 and all(part.isdigit() for part in parts):
        return tuple(int(part) for part in parts)
    return ()


def release_assets(name):
    modern, legacy, minimum = ASSETS[name]
    return [legacy, modern] if libc_version() and libc_version() < minimum else [modern, legacy]


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
                       for directory in (Path.cwd() / 'param', TOOLS / 'param')
                       if directory.is_dir() and all((directory / name).is_file() for name in PARAM_HASHES)]
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


def fetch(url, target, expected):
    with urllib.request.urlopen(url, timeout=90) as response, open(target, 'wb') as out:
        shutil.copyfileobj(response, out)
    if digest(target) != expected:
        target.unlink()
        raise ValueError('sha256 mismatch for ' + url)


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


def download_binary(name, target, replace=False):
    last_error = None
    with tempfile.TemporaryDirectory() as temp:
        for asset in release_assets(name):
            source = Path(temp) / asset
            fetch(RELEASE + asset, source, CHECKSUMS[asset])
            source.chmod(0o755)
            try:
                candidate(source, name)
            except (RuntimeError, subprocess.TimeoutExpired) as error:
                last_error = error
                continue
            install_copy(source, target, replace=replace)
            return asset
        raise RuntimeError(f'no released {name} asset runs on this host: {last_error}')


def install(args):
    (TOOLS / 'bin').mkdir(parents=True, exist_ok=True)
    (TOOLS / 'param').mkdir(parents=True, exist_ok=True)
    summary = {}
    receipt = TOOLS / 'install.json'
    previous = json.loads(receipt.read_text()) if receipt.is_file() else {}
    local_installs = []
    for name, selected in [('vinardock', args.vinardock),
                           ('obabel-vinardock', args.obabel_vinardock)]:
        target = TOOLS / 'bin' / name
        if selected == 'download':
            asset = download_binary(name, target, replace=args.replace)
            origin = 'release v1.0.0/' + asset
        else:
            source = Path(selected).expanduser().resolve()
            if not candidate(source, name):
                raise ValueError('not an executable: ' + str(source))
            same = source == target.resolve()
            install_copy(source, target, replace=args.replace)
            origin = (previous.get(name, {}).get('source') or 'local ' + str(target)) if same \
                else 'local ' + str(source)
            local_installs.append(name)
        summary[name] = candidate(target, name)
        summary[name]['source'] = origin
        if origin.startswith('release ') and origin.rsplit('/', 1)[1] != ASSETS[name][0]:
            summary[name]['note'] = origin.rsplit('/', 1)[1] + ' selected for glibc < ' + '.'.join(map(str, ASSETS[name][2]))
    for name, expected in PARAM_HASHES.items():
        target = TOOLS / 'param' / name
        if args.param == 'download':
            with tempfile.TemporaryDirectory() as temp:
                source = Path(temp) / name
                fetch(SOURCE + 'param/' + name, source, expected)
                install_copy(source, target, replace=args.replace)
            origin = 'source commit 11caaa8'
        else:
            source = Path(args.param).expanduser().resolve() / name
            if not source.is_file():
                raise FileNotFoundError(source)
            install_copy(source, target, replace=args.replace)
            origin = 'local ' + str(Path(args.param).expanduser().resolve())
        summary.setdefault('param', {})[name] = {'path': str(target), 'source': origin}
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
    # regenerate help references only for tools installed from a local path —
    # the checked-in files already document the pinned release builds
    refs = ROOT / 'references'
    for name, command in [('vinardock-help.txt', [str(TOOLS / 'bin/vinardock'), '--help']),
                          ('obabel-vinardock-help.txt', [str(TOOLS / 'bin/obabel-vinardock'), '-H'])]:
        tool = 'vinardock' if name.startswith('vinardock') else 'obabel-vinardock'
        if tool not in local_installs:
            continue
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
    install_parser.add_argument('--vinardock', required=True)
    # the repo ships a fully static build — zero host Open Babel needed
    bundled_obabel = ROOT / 'bin/obabel-vinardock'
    install_parser.add_argument('--obabel-vinardock',
                                default=str(bundled_obabel) if bundled_obabel.is_file()
                                else 'download')
    install_parser.add_argument('--param', default='download')
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
