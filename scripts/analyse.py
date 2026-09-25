#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

CATEGORIES = {'hydrogen_bonds': 'H-bonds', 'salt_bridges': 'Salt bridges',
              'pi_stacks': 'Pi-stacks', 'hydrophobic_interactions': 'Hydrophobic',
              'pi_cation_interactions': 'Pi-cation', 'halogen_bonds': 'Halogen',
              'water_bridges': 'Water bridges', 'metal_complexes': 'Metal complexes'}


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


def status(path, state):
    temp = path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(state, indent=2) + '\n')
    os.replace(temp, path)


def convert(executable, source, dest):
    result = subprocess.run([str(executable), str(source), '-O', str(dest)], capture_output=True, text=True)
    if result.returncode or not dest.is_file():
        raise RuntimeError('obabel-vinardock conversion failed: ' + result.stderr[-400:])


def selected_model(source, destination, number):
    lines = source.read_text(errors='replace').splitlines(keepends=True)
    has_models = any(line.startswith('MODEL') for line in lines)
    if not has_models:
        if number != 1:
            raise ValueError('model number not found')
        destination.write_text(''.join(lines))
        return
    selected = []
    inside = False
    for line in lines:
        if line.startswith('MODEL'):
            inside = int(line.split()[1]) == number
        if inside:
            selected.append(line)
        if line.startswith('ENDMDL') and inside:
            break
    if not selected or not selected[-1].startswith('ENDMDL'):
        raise ValueError('model number not found or unterminated')
    destination.write_text(''.join(selected))


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


def provenance(manifest, run, root):
    tools = manifest.get('tools', {})
    lines = ['## Provenance', '']
    for name in ('vinardock', 'obabel-vinardock'):
        tool = tools.get(name, {})
        lines.append(f'- {name}: `{tool.get("path", "UNKNOWN")}`  sha256=`{tool.get("sha256", "UNKNOWN")}` '
                     f'BuildID=`{tool.get("build_id", "N/A")}` version=`{tool.get("version_line", tool.get("version", "N/A"))}` '
                     f'source=`{tool.get("source", "UNKNOWN")}`')
        if tool.get('note'):
            lines.append(f'  - note: {tool["note"]}')
    plip = tools.get('plip', {})
    lines.append(f'- PLIP: `{plip.get("path", "UNKNOWN")}` sha256=`{plip.get("sha256", "UNKNOWN")}` '
                 f'version=`{plip.get("version", "UNKNOWN")}` Open Babel bindings=`{plip.get("openbabel_bindings", "UNKNOWN")}`')
    for name, item in tools.get('param', {}).items():
        if isinstance(item, dict):
            lines.append(f'- parameter {name}: `{item.get("path", "UNKNOWN")}` sha256=`{item.get("sha256", "UNKNOWN")}`')
        else:
            lines.append(f'- parameter {name}: sha256=`{item}`')
    recipe = run['recipe']
    lines.append(f'- recipe: `{recipe["name"]}` sha256=`{recipe["sha256"]}`')
    lines.append(f'- exact config: `dock/config.txt` sha256=`{run["config_sha256"]}`; '
                 f'seed={run["seed"]}; threads={run["threads"]}')
    lines.append(f'- box: `{json.dumps(run["resolved_box"])}`')
    lines.append('')
    lines.append('```')
    lines += (root / 'dock/config.txt').read_text().splitlines()
    lines.append('```')
    lines.append('')
    return lines


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--pose', type=int, default=1)
    parser.add_argument('--tools-dir', type=Path, default=Path.home() / '.local/share/docking-tools')
    args = parser.parse_args()
    root = args.run_dir.resolve()
    stage = root / 'analysis'
    stage.mkdir(parents=True, exist_ok=True)
    started = now()
    state = {'stage': 'analyse', 'status': 'failed', 'started': started, 'finished': started,
             'artifacts': [], 'metrics': {}, 'warnings': [], 'error': None}
    try:
        manifest = json.loads((root / 'manifest.json').read_text())
        dock = json.loads((root / 'dock/status.json').read_text())
        if dock['status'] != 'ok':
            raise ValueError('dock/status.json is not ok')
        for item in dock['artifacts']:
            path = root / item['path']
            if not path.is_file() or digest(path) != item['sha256']:
                raise ValueError('docking artifact hash mismatch: ' + item['path'])
        tools = args.tools_dir.expanduser().resolve()
        obabel = tools / 'bin/obabel-vinardock'
        plip = tools / 'plip-venv/bin/plip'
        if not obabel.is_file() or not plip.is_file():
            raise FileNotFoundError('obabel-vinardock or PLIP not installed')
        run = dock['metrics']
        outdir = root / run['output_dir']
        lines = ['# Docking report', ''] + provenance(manifest, run, root)
        lines += ['## Ligands', '', '| Ligand | Best score (kcal/mol) | Conformers | Analysed pose |',
                  '|---|---:|---:|---:|']
        for name, info in sorted(run['per_ligand'].items(), key=lambda pair: pair[1]['best']):
            lines.append(f'| {name} | {info["best"]:.3f} | {info["nconfs"]} | {args.pose} |')
        lines += ['']
        analyzed = {}
        interactions = {}
        for name, info in sorted(run['per_ligand'].items()):
            if args.pose < 1 or args.pose > info['nconfs']:
                raise ValueError(f'{name}: pose {args.pose} outside 1..{info["nconfs"]}')
            output = outdir / (name + '.pdbt') if len(run['per_ligand']) == 1 else outdir / name[:2] / name / (name + '.pdbt')
            model_pdbt = stage / (name + '_pose.pdbt')
            selected_model(output, model_pdbt, args.pose)
            ligand_pdb = stage / (name + '_ligand.pdb')
            convert(obabel, model_pdbt, ligand_pdb)
            modified = sorted(output.parent.glob('*_modified.pdbt'))
            rec = modified[0] if modified else root / 'prep' / run.get('receptor_file', 'receptor.pdbt')
            receptor_pdb = stage / (name + '_receptor.pdb')
            convert(obabel, rec, receptor_pdb)
            complex_pdb = stage / (name + '_complex.pdb')
            ligand_ids = make_complex(receptor_pdb, ligand_pdb, complex_pdb)
            cmd = [str(plip), '-f', str(complex_pdb), '-o', str(stage), '-x', '-t',
                   '--model', '1', '--name', name + '_report']
            result = subprocess.run(cmd, capture_output=True, text=True)
            log = stage / (name + '_plip.log')
            log.write_text(result.stdout + result.stderr)
            if result.returncode:
                raise RuntimeError(f'PLIP failed for {name}: {result.stderr[-400:]}')
            xml = stage / (name + '_report.xml')
            txt = stage / (name + '_report.txt')
            if not xml.is_file() or not txt.is_file():
                raise RuntimeError(f'PLIP reports missing for {name}')
            counts, residues = parse_xml(xml, ligand_ids)
            analyzed[name] = f'{args.pose} of {info["nconfs"]}'
            interactions[name] = counts
            lines += [f'### {name}', '', f'- Analysed pose: {analyzed[name]}',
                      '- Model energies (REMARK 980, kcal/mol): ' + ', '.join(
                          f'{index}: {energy:.3f}' for index, energy in enumerate(info['energies'], 1)),
                      f'- TORSDOF: {info["torsdof"]}',
                      f'- Initial-to-final RMSD: {info["rmsd"]}',
                      f'- Complex: `{complex_pdb.relative_to(root)}`',
                      f'- PLIP: `{xml.relative_to(root)}`, `{txt.relative_to(root)}`', '']
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
            for path in (model_pdbt, ligand_pdb, receptor_pdb, complex_pdb, xml, txt, log):
                state['artifacts'].append(artifact(path, root))
        lines += ['## Vinardock log', '', '`dock/vinardock.log`:', '', '```']
        lines += (root / 'dock/vinardock.log').read_text(errors='replace').splitlines()
        lines += ['```', '']
        report = root / 'report.md'
        report.write_text('\n'.join(lines) + '\n')
        state['artifacts'].append(artifact(report, root))
        state['metrics'] = {'analyzed_pose': analyzed, 'interactions': interactions,
                            'report': 'report.md', 'dock_status_sha256': digest(root / 'dock/status.json')}
        state['status'] = 'ok'
    except (OSError, ValueError, KeyError, RuntimeError, ET.ParseError, subprocess.SubprocessError) as error:
        state['error'] = str(error)
    state['finished'] = now()
    status(stage / 'status.json', state)
    print(json.dumps({'status': state['status'], 'error': state['error'], 'metrics': state['metrics']}))
    return 0 if state['status'] == 'ok' else 1


if __name__ == '__main__':
    sys.exit(main())
