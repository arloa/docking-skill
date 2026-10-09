import importlib.util
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def atom(x, y, z, chain='A', kind='ATOM  ', name='CA', resname='ALA', resseq=1):
    # PDB fixed columns: resname at 17:20, chain at 21, resseq at 22:26,
    # x/y/z at 30:38 / 38:46 / 46:54
    return (f'{kind}{1:5d}  {name:<4s}{resname:>3s} {chain}{resseq:4d}    '
            f'{x:8.3f}{y:8.3f}{z:8.3f}')


def write_status(root, folder, stage, ok=True, artifacts=(), **extra):
    directory = root / folder
    directory.mkdir(parents=True, exist_ok=True)
    for name in artifacts:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text('x')
    state = {'stage': stage, 'status': 'ok' if ok else 'failed',
             'started': extra.pop('started', '2026-01-01T00:00:01+00:00'),
             'finished': extra.pop('finished', '2026-01-01T00:00:02+00:00'),
             'artifacts': list(artifacts), 'metrics': extra.pop('metrics', {}),
             'warnings': [], 'error': None}
    (directory / 'status.json').write_text(json.dumps(state))
    return state


class RecipeTests(unittest.TestCase):
    def test_parse_flags_plain_fragment(self):
        # a recipe or --config file is a vinardock config: --key value
        # lines, bare --key switches, # comments — no templating
        pipe = module('pipeline')
        with tempfile.TemporaryDirectory() as temp:
            recipe = Path(temp) / 'custom.conf'
            recipe.write_text('# a comment\n--seed 1\n--docking_mode pso_only\n'
                              '--verbose\n--swarm.extend_pso 10  # trailing\n')
            flags = pipe.parse_flags(recipe)
            self.assertEqual(flags, {'seed': '1', 'docking_mode': 'pso_only',
                                     'verbose': '', 'swarm.extend_pso': '10'})
            recipe.write_text('not-a-flag\n')
            with self.assertRaisesRegex(ValueError, 'invalid flag line'):
                pipe.parse_flags(recipe)

    def test_bundled_recipes_parse_to_plain_flags(self):
        pipe = module('pipeline')
        for name in ('standard', 'screening', 'rescore', 'flexible',
                     'mutation-dg'):
            flags = pipe.parse_flags(pipe.read_recipe(name))
            self.assertIsNotNone(flags)
        self.assertEqual(pipe.parse_flags(pipe.read_recipe('standard')),
                         {'docking_mode': 'pso_mc'})
        self.assertEqual(pipe.parse_flags(pipe.read_recipe('mutation-dg')),
                         {'calc_mutate_dG': ''})
        self.assertEqual(pipe.parse_flags(pipe.read_recipe('flexible')), {})


class PrepareHelperTests(unittest.TestCase):
    def test_planar_input_pdb(self):
        pipe = module('pipeline')
        with tempfile.TemporaryDirectory() as temp:
            planar = Path(temp) / 'flat.pdb'
            planar.write_text(atom(1.0, 2.0, 0.0) + '\n' + atom(3.0, 4.0, 0.0) + '\n')
            self.assertTrue(pipe.planar_input(planar))
            planar.write_text(atom(1.0, 2.0, 0.0) + '\n' + atom(3.0, 4.0, 5.0) + '\n')
            self.assertFalse(pipe.planar_input(planar))

    def test_planar_input_sdf(self):
        pipe = module('pipeline')
        with tempfile.TemporaryDirectory() as temp:
            sdf = Path(temp) / 'lig.sdf'
            sdf.write_text('name\nprog\ncomment\n  2  0  0  0  0  0  0  0  0  0\n'
                           '    1.0000    2.0000    0.0000 C   0  0\n'
                           '    3.0000    4.0000    0.0000 C   0  0\n$$$$\n')
            self.assertTrue(pipe.planar_input(sdf))
            sdf.write_text(sdf.read_text() + 'name\nprog\ncomment\n  1  0\n'
                           '    1.0000    2.0000    0.0000 C   0  0\n$$$$\n')
            with self.assertRaisesRegex(ValueError, 'one molecule'):
                pipe.planar_input(sdf)

    def test_planar_input_mol2(self):
        pipe = module('pipeline')
        with tempfile.TemporaryDirectory() as temp:
            mol2 = Path(temp) / 'lig.mol2'
            mol2.write_text('@<TRIPOS>MOLECULE\nlig\n@<TRIPOS>ATOM\n'
                            '1 C1 1.0 2.0 0.0 C.2 1 LIG 0.0\n'
                            '2 C2 3.0 4.0 0.0 C.2 1 LIG 0.0\n@<TRIPOS>BOND\n')
            self.assertTrue(pipe.planar_input(mol2))
            mol2.write_text('@<TRIPOS>MOLECULE\nlig\n')
            with self.assertRaisesRegex(ValueError, 'ATOM'):
                pipe.planar_input(mol2)
            mol2.write_text('@<TRIPOS>MOLECULE\na\n@<TRIPOS>ATOM\n'
                            '1 C1 1.0 2.0 0.0 C.2 1 LIG 0.0\n'
                            '@<TRIPOS>MOLECULE\nb\n@<TRIPOS>ATOM\n'
                            '1 C1 5.0 6.0 0.0 C.2 1 LIG 0.0\n')
            with self.assertRaisesRegex(ValueError, 'one molecule'):
                pipe.planar_input(mol2)

    def test_strip_source_remark(self):
        pipe = module('pipeline')
        with tempfile.TemporaryDirectory() as temp:
            pdbt = Path(temp) / 'lig.pdbt'
            pdbt.write_text('REMARK  Name = /tmp/xyz/lig.sdf\n' + atom(1, 2, 3) + '\n')
            pipe.strip_source_remark(pdbt)
            self.assertNotIn('REMARK  Name', pdbt.read_text())


class DockHelperTests(unittest.TestCase):
    def test_model_data(self):
        pipe = module('pipeline')
        with tempfile.TemporaryDirectory() as temp:
            pdbt = Path(temp) / 'out.pdbt'
            pdbt.write_text(
                'MODEL        1\nREMARK 980    VINA RESULT:    -7.5\n' + atom(1, 2, 3) +
                '\nENDMDL\nMODEL        2\nREMARK 980    VINA RESULT:    -6.1\n' +
                atom(1, 2, 3) + '\nTORSDOF 4\nENDMDL\n')
            energies, torsdof = pipe.model_data(pdbt)
            self.assertEqual(energies, [-7.5, -6.1])
            self.assertEqual(torsdof, 4)

    def test_model_data_rejects_digit_labels(self):
        pipe = module('pipeline')
        with tempfile.TemporaryDirectory() as temp:
            pdbt = Path(temp) / 'out.pdbt'
            pdbt.write_text('MODEL        1\nREMARK 980    STEP2: 42.0\nENDMDL\n')
            energies, _ = pipe.model_data(pdbt)
            self.assertEqual(energies, [])

    def test_score_column_real_headers(self):
        pipe = module('pipeline')
        # the actual v1.0.0 header carries a unit suffix — exact match misses it
        self.assertEqual(pipe.score_column(['Ligand', 'nConfs', 'vinardo score (kcal/mol)']), 2)
        # reference-state mode: prefer the corrected (normalized) column
        self.assertEqual(pipe.score_column(
            ['Ligand', 'nConfs', 'binding_energy', 'BE_ligwater', 'BE_recwater',
             'E_corrected', 'rmsd']), 5)
        self.assertEqual(pipe.score_column(['Ligand', 'nConfs', 'binding_energy']), 2)
        self.assertIsNone(pipe.score_column(['Ligand', 'nConfs', 'foo']))

    def test_parse_log_csv_rigid(self):
        pipe = module('pipeline')
        rows = [['Ligand', 'nConfs', 'vinardo score (kcal/mol)'],
                ['lig1', '3', '-7.5', '1.2'],
                ['lig2', '0', '', '']]
        results, failures, dg_mode, score_col = pipe.parse_log_csv(rows, ['lig1', 'lig2'])
        self.assertFalse(dg_mode)
        self.assertEqual(score_col, 2)
        self.assertEqual(results['lig1']['csv_score'], -7.5)
        self.assertEqual(results['lig1']['nconfs'], 3)
        self.assertEqual(results['lig1']['rmsd'], '1.2')
        self.assertIn('0 conformers', failures['lig2'])

    def test_parse_log_csv_rmsd_named_column(self):
        pipe = module('pipeline')
        rows = [['Ligand', 'nConfs', 'binding_energy', 'BE_ligwater', 'BE_recwater',
                 'E_corrected', 'rmsd'],
                ['lig1', '2', '-8.0', '-1.0', '-0.5', '-9.5', '0.83']]
        results, failures, _, score_col = pipe.parse_log_csv(rows, ['lig1'])
        self.assertEqual(results['lig1']['rmsd'], '0.83')
        self.assertEqual(score_col, 5)
        self.assertEqual(results['lig1']['reference_state']['E_corrected'], -9.5)

    def test_parse_log_csv_missing_row(self):
        pipe = module('pipeline')
        rows = [['Ligand', 'nConfs', 'Score'], ['lig1', '2', '-5.0']]
        results, failures, _, _ = pipe.parse_log_csv(rows, ['lig1', 'lig2'])
        self.assertIn('no row', failures['lig2'])

    def test_parse_log_csv_dg_mode(self):
        pipe = module('pipeline')
        rows = [['Ligand', 'E_WT', 'E_MUT', 'dG'],
                ['lig1', '-8.0', '-6.0', '2.0'],
                ['lig2', 'N/A', 'N/A', 'N/A']]
        results, failures, dg_mode, _ = pipe.parse_log_csv(rows, ['lig1', 'lig2'])
        self.assertTrue(dg_mode)
        self.assertEqual(results['lig1']['dG']['dG'], 2.0)
        self.assertIn('lig2', failures)

    def test_parse_log_csv_structural_errors(self):
        pipe = module('pipeline')
        with self.assertRaisesRegex(RuntimeError, 'header'):
            pipe.parse_log_csv([['Wrong', 'nConfs'], ['lig1', '2']], ['lig1'])
        with self.assertRaisesRegex(RuntimeError, 'duplicate|unexpected'):
            pipe.parse_log_csv([['Ligand', 'nConfs', 'Score'],
                                ['other', '1', '-1.0']], ['lig1'])

    def test_check_score(self):
        pipe = module('pipeline')
        energies = [-7.5, -6.0]
        self.assertEqual(pipe.check_score('lig', -7.51, energies), -7.51)
        self.assertEqual(pipe.check_score('lig', None, energies), -7.5)
        with self.assertRaisesRegex(RuntimeError, 'disagrees'):
            pipe.check_score('lig', -5.0, energies)

    def test_resolve_box_autobox(self):
        pipe = module('pipeline')
        with tempfile.TemporaryDirectory() as temp:
            ref = Path(temp) / 'ref_autobox.pdbt'
            ref.write_text(atom(10.0, 20.0, 30.0) + '\n' + atom(14.0, 24.0, 34.0) + '\n')
            box = pipe.resolve_box({'autobox': '2.0', 'autobox_ligand': str(ref)}, ref)
            self.assertEqual(box['center'], [12.0, 22.0, 32.0])
            self.assertEqual(box['size'], [8.0, 8.0, 8.0])

    def test_resolve_box_rejects_degenerate_reference(self):
        pipe = module('pipeline')
        with tempfile.TemporaryDirectory() as temp:
            ref = Path(temp) / 'ref.pdbt'
            ref.write_text(atom(10.0, 20.0, 30.0) + '\n' + atom(10.0, 20.0, 30.0) + '\n')
            with self.assertRaisesRegex(ValueError, 'zero extent'):
                pipe.resolve_box({'autobox': '2.0', 'autobox_ligand': str(ref)}, ref)

    def test_resolve_box_manual(self):
        pipe = module('pipeline')
        box = pipe.resolve_box({'center_x': '1', 'center_y': '2', 'center_z': '3',
                                'size_x': '10', 'size_y': '20', 'size_z': '30'}, Path('x'))
        self.assertEqual(box['center'], [1.0, 2.0, 3.0])
        self.assertEqual(box['size'], [10.0, 20.0, 30.0])

    def test_mutation_spec(self):
        pipe = module('pipeline')
        self.assertTrue(pipe.mutation_spec('A:S63T'))
        self.assertFalse(pipe.mutation_spec('A:S63S'))   # same residue: not a mutation
        self.assertFalse(pipe.mutation_spec(''))         # missing spec
        self.assertFalse(pipe.mutation_spec('S63T'))     # missing chain

    def test_centroid_outside_box(self):
        pipe = module('pipeline')
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / 'lig.pdbt'
            out.write_text('MODEL        1\n' + atom(1.0, 1.0, 1.0) + '\nENDMDL\n')
            inside = {'center': [0.0, 0.0, 0.0], 'size': [10.0, 10.0, 10.0]}
            outside = {'center': [50.0, 50.0, 50.0], 'size': [2.0, 2.0, 2.0]}
            self.assertFalse(pipe.centroid_outside_box(out, inside))
            self.assertTrue(pipe.centroid_outside_box(out, outside))

    def test_write_config_renders_pairs(self):
        pipe = module('pipeline')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.txt'
            pipe.write_config({'receptor': '/abs/rec.pdbt', 'seed': '42',
                               'verbose': ''}, path)
            self.assertEqual(path.read_text(),
                             '--receptor /abs/rec.pdbt\n--seed 42\n--verbose\n')


class AnalyseHelperTests(unittest.TestCase):
    def test_selected_model(self):
        pipe = module('pipeline')
        with tempfile.TemporaryDirectory() as temp:
            src = Path(temp) / 'out.pdbt'
            src.write_text('MODEL        1\n' + atom(1, 1, 1) + '\nENDMDL\n'
                           'MODEL        2\n' + atom(2, 2, 2) + '\nENDMDL\n')
            dest = Path(temp) / 'pose.pdbt'
            pipe.selected_model(src, dest, 2)
            self.assertIn('MODEL        2', dest.read_text())
            self.assertNotIn('MODEL        1\n', dest.read_text().split('ENDMDL')[0])
            with self.assertRaises(ValueError):
                pipe.selected_model(src, dest, 3)
            flat = Path(temp) / 'flat.pdbt'
            flat.write_text(atom(1, 1, 1) + '\n')
            pipe.selected_model(flat, dest, 1)
            with self.assertRaises(ValueError):
                pipe.selected_model(flat, dest, 2)

    def test_make_complex_assigns_free_chain(self):
        pipe = module('pipeline')
        with tempfile.TemporaryDirectory() as temp:
            rec = Path(temp) / 'rec.pdb'
            lig = Path(temp) / 'lig.pdb'
            complex_pdb = Path(temp) / 'complex.pdb'
            rec.write_text(atom(1, 2, 3, chain='A') + '\n')
            lig.write_text(atom(5.0, 6.0, 7.0, chain='X', name='C1',
                                resname='LIG', resseq=7) + '\n')
            ids = pipe.make_complex(rec, lig, complex_pdb)
            self.assertEqual(ids, {('LIG', 'B', '7')})
            self.assertIn('HETATM', complex_pdb.read_text())

    def test_parse_xml_matches_ligand_site(self):
        pipe = module('pipeline')
        xml = '''<report><bindingsites>
          <bindingsite><identifiers><hetid>LIG</hetid><chain>B</chain><position>7</position></identifiers>
            <interactions><hydrogen_bonds><hydrogen_bond><reschain>A</reschain><restype>SER</restype><resnr>63</resnr></hydrogen_bond></hydrogen_bonds></interactions>
          </bindingsite>
          <bindingsite><identifiers><hetid>HEM</hetid><chain>A</chain><position>300</position></identifiers>
            <interactions/></bindingsite>
        </bindingsites></report>'''
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'report.xml'
            path.write_text(xml)
            counts, residues = pipe.parse_xml(path, {('LIG', 'B', '7')})
            self.assertEqual(counts['hydrogen_bonds'], 1)
            self.assertEqual(counts['salt_bridges'], 0)
            self.assertEqual(residues, [('H-bonds', 'A:SER:63')])

    def test_parse_xml_rejects_ambiguous_site(self):
        pipe = module('pipeline')
        xml = '<report><bindingsites><bindingsite><interactions/></bindingsite></bindingsites></report>'
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'report.xml'
            path.write_text(xml)
            with self.assertRaisesRegex(ValueError, '0 sites|matching'):
                pipe.parse_xml(path, {('LIG', 'B', '7')})


class HelperTests(unittest.TestCase):
    def test_models_framing(self):
        pipe = module('pipeline')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'x.pdbt'
            path.write_text('ATOM a\nATOM b\n')          # flat, no MODEL
            self.assertEqual([len(b) for b in pipe.models(path)], [2])
            path.write_text('MODEL 1\nA\nENDMDL\nMODEL 2\nB\nENDMDL\n')
            self.assertEqual([len(b) for b in pipe.models(path)], [3, 3])
            path.write_text('MODEL 1\nA\n')            # unterminated
            blocks = list(pipe.models(path))
            self.assertEqual(len(blocks), 1)
            self.assertFalse(blocks[0][-1].startswith('ENDMDL'))

    def test_selected_model_empty_file_raises_valueerror(self):
        # regression: models() yields [] for empty files; block[0] used to
        # IndexError instead of the intended ValueError
        pipe = module('pipeline')
        with tempfile.TemporaryDirectory() as temp:
            empty = Path(temp) / 'empty.pdbt'
            empty.write_text('')
            with self.assertRaises(ValueError):
                pipe.selected_model(empty, Path(temp) / 'out.pdbt', 1)

    def test_to_pdbt_strategy_table(self):
        # the suffix -> conversion strategy table that defect #3 grew out of:
        # pin every branch so a new suffix can't be added to one path only
        pipe = module('pipeline')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            calls = []

            def fake_run(cmd, log, timeout):
                calls.append(cmd)
                dest = Path(cmd[cmd.index('-O') + 1])
                dest.write_text(atom(1, 2, 3) + '\n')

            warnings = []
            with patch.object(pipe, 'run_logged', side_effect=fake_run):
                # .pdbt -> copy, zero obabel calls
                src = root / 'a.pdbt'
                src.write_text(atom(1, 2, 3) + '\n')
                pipe.to_pdbt(src, root / 'a_out.pdbt', 'obabel', root / 'log', 60,
                             warnings, 'a')
                self.assertEqual(calls, [])
                self.assertEqual((root / 'a_out.pdbt').read_text(), src.read_text())
                # .smi -> gen3d two-step, exactly one molecule required
                smi = root / 'b.smi'
                smi.write_text('CCO ethanol\nCC benzene\n')
                with self.assertRaisesRegex(ValueError, 'one molecule'):
                    pipe.to_pdbt(smi, root / 'b.pdbt', 'obabel', root / 'log', 60,
                                 warnings, 'b')
                smi.write_text('CCO ethanol\n')
                pipe.to_pdbt(smi, root / 'b.pdbt', 'obabel', root / 'log', 60,
                             warnings, 'b')
                self.assertIn('--gen3d', calls[0])
                self.assertEqual(calls[0][1], '-:CCO')
                self.assertEqual(len(calls), 2)
                # planar .sdf -> --gen3d; 3D .sdf -> plain conversion
                calls.clear()
                sdf = root / 'flat.sdf'
                sdf.write_text('x\np\nc\n  1  0\n    1.0000    2.0000    0.0000 C   0 0\n$$$$\n')
                pipe.to_pdbt(sdf, root / 'f.pdbt', 'obabel', root / 'log', 60,
                             warnings, 'f')
                self.assertIn('--gen3d', calls[-1])
                self.assertIn('planar', warnings[-1])
                sdf.write_text('x\np\nc\n  1  0\n    1.0000    2.0000    3.0000 C   0 0\n$$$$\n')
                calls.clear()
                pipe.to_pdbt(sdf, root / 'f.pdbt', 'obabel', root / 'log', 60,
                             warnings, 'f')
                self.assertNotIn('--gen3d', calls[-1])
                # unknown suffix -> hard error
                with self.assertRaisesRegex(ValueError, 'unsupported'):
                    pipe.to_pdbt(root / 'x.xyz', root / 'o.pdbt', 'obabel',
                                 root / 'log', 60, warnings, 'x')

    def test_sanity_warnings(self):
        pipe = module('pipeline')
        box = {'center': [0, 0, 0], 'size': [10, 10, 10]}
        with tempfile.TemporaryDirectory() as temp:
            outdir = Path(temp)
            inside = outdir / 'in.pdbt'
            inside.write_text('MODEL 1\n' + atom(0, 0, 0) + '\nENDMDL\n')
            far = outdir / 'far.pdbt'
            far.write_text('MODEL 1\n' + atom(500, 0, 0) + '\nENDMDL\n')
            results = {'in': {'best': 0.5},           # positive best -> warn
                       'far': {'best': -5.0},          # outside box -> warn
                       'dead': {'status': 'failed'}}   # skipped
            warnings = pipe.sanity_warnings(results, box, outdir, True, False)
            self.assertEqual(len(warnings), 2)
            self.assertIn('positive', warnings[0])
            self.assertIn('outside', warnings[1])
            # dg_mode suppresses the positive-score check
            self.assertEqual(pipe.sanity_warnings(results, box, outdir, True, True),
                             [w for w in warnings if 'outside' in w])

    def test_parse_passthrough(self):
        pipe = module('pipeline')
        self.assertEqual(pipe.parse_passthrough(
            ['--seed', '7', '--center_x', '-5.5', '--vsmode',
             '--swarm.extend_pso', '10']),
            {'seed': '7', 'center_x': '-5.5', 'vsmode': '',
             'swarm.extend_pso': '10'})
        # --key=value is not vinardock syntax
        with self.assertRaisesRegex(ValueError, 'flag'):
            pipe.parse_passthrough(['--center_x=10'])
        with self.assertRaisesRegex(ValueError, 'flag'):
            pipe.parse_passthrough(['orphan'])
        with self.assertRaisesRegex(ValueError, 'flag'):
            pipe.parse_passthrough(['-x', '1'])

    def test_anchor_paths(self):
        pipe = module('pipeline')
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / 'PDBT').mkdir()
            (base / 'PDBT/rec.pdbt').write_text('x')
            flags = {'receptor': './PDBT/rec.pdbt',      # PATH_KEY: resolved
                     'ligand': 'PDBT',                    # existing dir: resolved
                     'center_x': '-5.5',                  # not a path: untouched
                     'seed': '7', 'docking_mode': 'pso_mc',
                     'out': 'results',                    # PATH_KEY, nonexistent
                     'other': 'nonexistent_file.xyz'}     # not a key, not real
            pipe.anchor_paths(flags, base)
            self.assertEqual(flags['receptor'], str(base / 'PDBT/rec.pdbt'))
            self.assertEqual(flags['ligand'], str(base / 'PDBT'))
            self.assertEqual(flags['center_x'], '-5.5')
            self.assertEqual(flags['seed'], '7')
            self.assertEqual(flags['docking_mode'], 'pso_mc')
            self.assertEqual(flags['out'], str(base / 'results'))
            self.assertEqual(flags['other'], 'nonexistent_file.xyz')

    def test_merge_into_last_wins_with_guarded_warning(self):
        pipe = module('pipeline')
        base = {'receptor': '/run/prep/rec.pdbt', 'threads': '32',
                'docking_mode': 'pso_mc'}
        warnings = []
        # plain default override (threads): applied silently
        pipe.merge_into(base, 'cli', {'threads': '8'}, warnings,
                        {'receptor'})
        self.assertEqual(base['threads'], '8')
        self.assertEqual(warnings, [])
        # guarded key (receptor): applied with a warning
        pipe.merge_into(base, 'cli', {'receptor': '/elsewhere.pdbt'},
                        warnings, {'receptor'})
        self.assertEqual(base['receptor'], '/elsewhere.pdbt')
        self.assertEqual(len(warnings), 1)
        self.assertIn('--receptor', warnings[0])
        # same value re-written: no warning
        pipe.merge_into(base, 'recipe', {'receptor': '/elsewhere.pdbt'},
                        warnings, {'receptor'})
        self.assertEqual(len(warnings), 1)

    def test_atom_xyz(self):
        pipe = module('pipeline')
        self.assertEqual(pipe.atom_xyz(atom(1.5, -2.0, 30.25)), (1.5, -2.0, 30.25))
        self.assertIsNone(pipe.atom_xyz('short'))


class ResumeTests(unittest.TestCase):
    def test_stage_done_rules(self):
        pipe = module('pipeline')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertFalse(pipe.stage_done(root, 'prepare'))
            write_status(root, 'prep', 'prepare', artifacts=['prep/rec.pdbt'],
                         finished='2026-01-01T00:10:00+00:00')
            self.assertTrue(pipe.stage_done(root, 'prepare'))
            self.assertFalse(pipe.stage_done(root, 'dock'))
            write_status(root, 'dock', 'dock', artifacts=['dock/log.txt'],
                         started='2026-01-01T00:20:00+00:00',
                         finished='2026-01-01T00:30:00+00:00')
            self.assertTrue(pipe.stage_done(root, 'dock'))
            # dock older than a re-run prepare -> stale
            write_status(root, 'prep', 'prepare', artifacts=['prep/rec.pdbt'],
                         finished='2026-01-01T00:40:00+00:00')
            self.assertFalse(pipe.stage_done(root, 'dock'))
            # missing artifact -> not reusable
            write_status(root, 'prep', 'prepare', artifacts=['prep/rec.pdbt'],
                         finished='2026-01-01T00:10:00+00:00')
            (root / 'dock/log.txt').unlink()
            self.assertFalse(pipe.stage_done(root, 'dock'))

    def test_stage_done_tolerates_z_suffix(self):
        pipe = module('pipeline')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_status(root, 'prep', 'prepare', artifacts=['prep/rec.pdbt'],
                         finished='2026-01-01T00:10:00Z')   # Z suffix variant
            write_status(root, 'dock', 'dock', artifacts=['dock/log.txt'],
                         started='2026-01-01T00:20:00+00:00',
                         finished='2026-01-01T00:30:00+00:00')
            self.assertTrue(pipe.stage_done(root, 'dock'))
            # an 'ok' status with no artifacts recorded is not reusable
            write_status(root, 'analyse', 'analyse', artifacts=[],
                         started='2026-01-01T00:40:00+00:00',
                         finished='2026-01-01T00:50:00+00:00')
            write_status(root, 'dock', 'dock', artifacts=['dock/log.txt'],
                         started='2026-01-01T00:20:00+00:00',
                         finished='2026-01-01T00:30:00+00:00')
            self.assertFalse(pipe.stage_done(root, 'analyse'))


class SetupTests(unittest.TestCase):
    def test_candidate_rejects_unlaunchable(self):
        setup = module('setup')
        with tempfile.TemporaryDirectory() as temp:
            bad = Path(temp) / 'obabel-vinardock'
            bad.write_text('#!/bin/sh\necho "GLIBC_2.38 not found" >&2\nexit 1\n')
            bad.chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, 'cannot run'):
                setup.candidate(bad, 'obabel-vinardock')

    def test_install_copy_requires_replace(self):
        setup = module('setup')
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'new'
            target = Path(temp) / 'installed'
            source.write_text('new')
            target.write_text('old')
            with self.assertRaises(FileExistsError):
                setup.install_copy(source, target)
            self.assertEqual(target.read_text(), 'old')
            setup.install_copy(source, target, replace=True)
            self.assertEqual(target.read_text(), 'new')

    def test_probe_only_checks_pwd_path_and_tools(self):
        setup = module('setup')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            other = root / 'not_searched'
            other.mkdir()
            binary = other / 'vinardock'
            binary.write_text('#!/bin/sh\necho "2Vinardo molecular docking program"\n')
            binary.chmod(0o755)
            old = Path.cwd()
            try:
                os.chdir(root)
                with patch.object(setup, 'TOOLS', root / 'tools'), patch.dict(os.environ, {'PATH': ''}):
                    # bundled bin/vinardock is always found; the test binary
                    # in <pwd> must appear too once dropped in
                    self.assertNotIn(str(binary),
                                     [c['path'] for c in setup.probe()['vinardock']])
                    binary.rename(root / 'vinardock')
                    self.assertIn(str(root / 'vinardock'),
                                  [c['path'] for c in setup.probe()['vinardock']])
            finally:
                os.chdir(old)

    def test_probe_reports_unlaunchable_binaries(self):
        setup = module('setup')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = root / 'obabel-vinardock'
            binary.write_text('#!/bin/sh\necho "GLIBC_2.38 not found" >&2\nexit 1\n')
            binary.chmod(0o755)
            old = Path.cwd()
            try:
                os.chdir(root)
                with patch.object(setup, 'TOOLS', root / 'tools'), patch.dict(os.environ, {'PATH': ''}):
                    result = setup.probe()
                    # broken binary lands in unusable, never in the good list
                    # (the bundled bin/obabel-vinardock is legitimately found)
                    self.assertEqual(result['unusable']['obabel-vinardock'][0]['path'], str(binary))
                    self.assertIn('cannot run', result['unusable']['obabel-vinardock'][0]['error'])
                    self.assertNotIn(str(binary),
                                     [c['path'] for c in result['obabel-vinardock']])
            finally:
                os.chdir(old)

class IntegrationTests(unittest.TestCase):
    """Run the real binaries end-to-end; skipped when the tools dir is absent.

    Unlike the unit tests (which use fabricated fixtures) this asserts the
    *actual* output formats — the log.csv header, REMARK 980 lines, the DOCK/
    tree, and the vinardock log text — so a format drift in a real binary
    shows up here, not in production.
    """
    TOOLS = Path.home() / '.local/share/docking-tools'
    BIN = TOOLS / 'bin'
    VENV = TOOLS / 'plip-venv' / 'bin'
    PIPELINE = ROOT / 'scripts' / 'pipeline.py'

    def setUp(self):
        missing = [t for t in ('vinardock', 'obabel-vinardock')
                   if not (self.BIN / t).is_file()]
        if missing:
            self.skipTest(f'real binaries not installed: {missing}')

    @staticmethod
    def pdb_atom(serial, name, resname, chain, resseq, x, y, z, elem):
        return (f'ATOM  {serial:>5} {name:^4} {resname:>3} {chain}{resseq:>4}    '
                f'{x:8.3f}{y:8.3f}{z:8.3f}  1.00 20.00          {elem:>2}\n')

    @classmethod
    def tiny_receptor(cls, path):
        """A few residues arranged in a ring around the origin (the box)."""
        serial = 0
        residues = [('ALA', 1), ('GLY', 2), ('SER', 3), ('VAL', 4), ('LEU', 5)]
        lines = []
        for i, (resname, seq) in enumerate(residues):
            angle = 2 * 3.14159 * i / len(residues)
            cx, cy = 9.0 * math.cos(angle), 9.0 * math.sin(angle)
            for name, elem, dx, dy, dz in (('N', 'N', 0, 0, -1.4),
                                           ('CA', 'C', 0, 0, 0),
                                           ('C', 'C', 1.4, 0, 0),
                                           ('O', 'O', 2.0, 0.9, 0)):
                serial += 1
                lines.append(cls.pdb_atom(serial, name, resname, 'A', seq,
                                          cx + dx, cy + dy, dz, elem))
        path.write_text(''.join(lines) + 'TER\nEND\n')

    def run_cli(self, *args, timeout=600):
        env = dict(os.environ,
                   PATH=f"{self.BIN}:{self.VENV}:{os.environ.get('PATH', '')}")
        result = subprocess.run([sys.executable, str(self.PIPELINE), *args],
                                capture_output=True, text=True, env=env, timeout=timeout)
        self.assertEqual(result.returncode, 0,
                         f'pipeline {" ".join(args)} failed:\n{result.stdout}\n{result.stderr}')
        return result

    def test_real_pipeline_output_formats(self):
        pipe = module('pipeline')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rec = root / 'receptor.pdb'
            self.tiny_receptor(rec)
            ligs = root / 'ligands.smi'
            ligs.write_text('CCO ethanol\nc1ccccc1 benzene\n')
            ref = root / 'ref.smi'
            ref.write_text('CC(=O)Oc1ccccc1C(=O)O aspirin\n')

            self.run_cli('prepare', '--run_dir', str(root),
                         '--prepare_receptor', str(rec),
                         '--prepare_ligand', str(ligs),
                         '--prepare_autobox_ligand', str(ref),
                         '--obabel_vinardock', str(self.BIN / 'obabel-vinardock'))
            # autobox reference must have been 3D-generated, not all-zero coords
            autobox = root / 'prep/ref_autobox.pdbt'
            self.assertTrue(autobox.is_file())
            xyz = pipe.coords(autobox)
            extents = [max(c[i] for c in xyz) - min(c[i] for c in xyz) for i in range(3)]
            self.assertTrue(all(e > 0.5 for e in extents),
                            f'autobox reference is degenerate: extents {extents}')

            self.run_cli('dock', '--run_dir', str(root), '--recipe', 'standard',
                         '--seed', '7', '--threads', '2', '--conformations', '2',
                         '--center_x', '0', '--center_y', '0', '--center_z', '0',
                         '--size_x', '25', '--size_y', '25', '--size_z', '25',
                         '--tools_dir', str(self.TOOLS))

            # real log.csv lives in the attempt's DOCK/ dir
            csv_files = list((root / 'dock/attempts').rglob('log.csv'))
            self.assertEqual(len(csv_files), 1)
            csv_lines = csv_files[0].read_text().splitlines()
            self.assertEqual(csv_lines[0].split(',')[0].strip().lower(), 'ligand')
            csv_rows = [line.split(',') for line in csv_lines]
            results, failures, dg_mode, score_col = pipe.parse_log_csv(
                csv_rows, ['ethanol', 'benzene'])
            self.assertFalse(dg_mode)
            self.assertIsNotNone(
                score_col,
                f'real log.csv header has no recognizable score column: {csv_rows[0]}')
            # the dock stage cross-checked csv score vs pdbt energies — verify
            # the recorded metrics agree (this is the review's defect #1)
            dock_state = json.loads((root / 'dock/status.json').read_text())
            self.assertEqual(dock_state['status'], 'ok')
            for name, entry in dock_state['metrics']['per_ligand'].items():
                self.assertAlmostEqual(entry['best'], min(entry['energies']),
                                       delta=0.05, msg=f'{name}: best vs energies')
                self.assertGreaterEqual(entry['nconfs'], 1)
            # real DOCK/ tree + real REMARK 980 lines
            dock_dirs = list((root / 'dock').rglob('DOCK'))
            self.assertEqual(len(dock_dirs), 1)
            poses = sorted(dock_dirs[0].rglob('*.pdbt'))
            self.assertTrue(poses)
            for pose in poses:
                energies = pipe.model_data(pose)[0]
                self.assertTrue(energies, f'{pose.name}: no REMARK 980 energies')
            # real vinardock log text exists and is non-trivial
            logs = list((root / 'dock/attempts').rglob('vinardock.log'))
            self.assertTrue(logs)
            self.assertGreater(len(logs[0].read_text()), 100)

            if (self.VENV / 'plip').is_file():
                self.run_cli('analyse', '--run_dir', str(root), '--tools_dir', str(self.TOOLS))
                report = (root / 'report.md').read_text()
                self.assertIn('Docking report', report)
                self.assertTrue(list((root / 'analysis').glob('*_report.xml')))


if __name__ == '__main__':
    unittest.main()
