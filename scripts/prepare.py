#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


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


def status(path, payload):
    temp = path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(payload, indent=2) + '\n')
    os.replace(temp, path)


def run(command, log):
    result = subprocess.run(command, capture_output=True, text=True)
    with log.open('a') as stream:
        stream.write('$ ' + ' '.join(map(str, command)) + '\n' + result.stdout + result.stderr + '\n')
    if result.returncode:
        raise RuntimeError(f'conversion failed ({result.returncode}): {result.stderr.strip()[-300:]}')


def safe_name(name):
    stem = re.sub(r'[^A-Za-z0-9_-]+', '_', name.strip()).strip('_')
    if not stem:
        raise ValueError('ligand name has no safe characters: ' + repr(name))
    return stem


def atoms(path):
    return [line for line in path.read_text(errors='replace').splitlines()
            if line.startswith(('ATOM  ', 'HETATM'))]


def normalise_source_remark(path, original):
    lines = path.read_text(errors='replace').splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith('REMARK  Name = '):
            lines[index] = 'REMARK  Name = ' + str(original) + '\n'
            break
    path.write_text(''.join(lines))


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--receptor', type=Path, required=True)
    parser.add_argument('--ligand', type=Path, action='append', required=True)
    parser.add_argument('--obabel-vinardock', type=Path,
                        default=Path.home() / '.local/share/docking-tools/bin/obabel-vinardock')
    parser.add_argument('--drop-hetatm', action='store_true')
    parser.add_argument('--autobox-ligand', type=Path)
    args = parser.parse_args()
    root = args.run_dir.resolve()
    stage = root / 'prep'
    stage.mkdir(parents=True, exist_ok=True)
    (stage / 'ligands').mkdir(exist_ok=True)
    log = stage / 'prep.log'
    started = now()
    result = {'stage': 'prepare', 'status': 'failed', 'started': started, 'finished': started,
              'artifacts': [], 'metrics': {}, 'warnings': [], 'error': None}
    try:
        obabel = args.obabel_vinardock.resolve(strict=True)
        if not os.access(obabel, os.X_OK):
            raise ValueError('obabel-vinardock is not executable')
        receptor = args.receptor.resolve(strict=True)
        lines = receptor.read_text(errors='replace').splitlines(keepends=True)
        chains = sorted({line[21:22].strip() or '?' for line in lines
                         if line.startswith(('ATOM  ', 'HETATM'))})
        hetatms = sorted({line[17:20].strip() for line in lines if line.startswith('HETATM')})
        if hetatms and not args.drop_hetatm:
            result['warnings'].append('Receptor HETATM residues kept: ' + ', '.join(hetatms))
        if args.drop_hetatm:
            lines = [line for line in lines if not line.startswith('HETATM')]
        if not any(line.startswith('ATOM  ') for line in lines):
            raise ValueError('receptor has no ATOM records')
        prepared_rec = stage / (receptor.stem + '.pdbt')
        with tempfile.TemporaryDirectory(dir=stage) as temp:
            source = Path(temp) / receptor.name
            source.write_text(''.join(lines))
            if receptor.suffix.lower() == '.pdbt':
                shutil.copyfile(source, prepared_rec)
            else:
                run([str(obabel), str(source), '-O', str(prepared_rec), '-xr', '-xc'], log)
        if not atoms(prepared_rec):
            raise ValueError('prepared receptor has no atoms')
        normalise_source_remark(prepared_rec, receptor)
        result['artifacts'].append(artifact(prepared_rec, root))
        ref_out = None
        if args.autobox_ligand:
            ref = args.autobox_ligand.resolve(strict=True)
            ref_out = stage / (ref.stem + '_autobox.pdbt')
            if ref_out == prepared_rec:
                raise ValueError('autobox reference and receptor share the same stem: ' + ref.stem)
            if ref.suffix.lower() == '.pdbt':
                shutil.copyfile(ref, ref_out)
            else:
                run([str(obabel), str(ref), '-O', str(ref_out), '-p7.4'], log)
            if not atoms(ref_out):
                raise ValueError('autobox reference has no atoms')
            result['artifacts'].append(artifact(ref_out, root))
        names = set()
        hashes = {}
        inputs = []
        for path in args.ligand:
            source = path.resolve(strict=True)
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
            if stem.lower() in names:
                raise ValueError('duplicate ligand name: ' + stem)
            names.add(stem.lower())
            target = stage / 'ligands' / (stem + '.pdbt')
            if smiles:
                with tempfile.TemporaryDirectory(dir=stage) as temp:
                    sdf = Path(temp) / 'ligand.sdf'
                    run([str(obabel), '-:' + smiles, '--gen3d', '-O', str(sdf)], log)
                    run([str(obabel), str(sdf), '-O', str(target), '-p7.4'], log)
            elif source.suffix.lower() == '.pdbt':
                shutil.copyfile(source, target)
            elif source.suffix.lower() in ('.sdf', '.mol2', '.pdb'):
                extra = ['--gen3d'] if planar_input(source) else []
                if extra:
                    result['warnings'].append(f'{stem}: planar input regenerated in 3D')
                run([str(obabel), str(source), *extra, '-O', str(target), '-p7.4'], log)
            else:
                raise ValueError('unsupported ligand format: ' + source.suffix)
            text = target.read_text(errors='replace')
            if not atoms(target) or 'TORSDOF ' not in text:
                raise ValueError('invalid prepared PDBT ligand: ' + stem)
            normalise_source_remark(target, source)
            hashes[stem] = digest(target)
            result['artifacts'].append(artifact(target, root))
        if not hashes:
            raise ValueError('no ligands prepared')
        result['metrics'] = {'n_ligands': len(hashes), 'ligands': list(hashes),
                             'receptor_chains': chains, 'hetatm_resnames': hetatms,
                             'ligand_hashes': hashes, 'receptor_file': prepared_rec.name,
                             'autobox_file': ref_out.name if ref_out else None}
        result['status'] = 'ok'
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        result['error'] = str(error)
        with log.open('a') as stream:
            stream.write('ERROR: ' + str(error) + '\n')
    result['finished'] = now()
    if log.exists():
        result['artifacts'].append(artifact(log, root))
    status(stage / 'status.json', result)
    print(json.dumps({'status': result['status'], 'error': result['error'], 'metrics': result['metrics']}))
    return 0 if result['status'] == 'ok' else 1


if __name__ == '__main__':
    sys.exit(main())
