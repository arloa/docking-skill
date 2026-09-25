import argparse
import importlib.util
import json
import os
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


class PipelineTests(unittest.TestCase):
    def test_recipe_guard_and_missing_slot(self):
        run = module('run')
        with tempfile.TemporaryDirectory() as temp:
            recipe = Path(temp) / 'custom.conf'
            recipe.write_text('# requires: box\n--flexres.res {flexres} # if-set: flexres\n'
                              '--flexres.autoflex {cutoff} # if-unset: flexres\n')
            flags, requires = run.recipe_flags(recipe, {'flexres': 'A:S63S'})
            self.assertEqual(flags, {'flexres.res': 'A:S63S'})
            self.assertEqual(requires, ['box'])
            with self.assertRaisesRegex(ValueError, 'missing slot cutoff'):
                run.recipe_flags(recipe, {})
            flags, _ = run.recipe_flags(recipe, {'cutoff': '7.0'})
            self.assertEqual(flags, {'flexres.autoflex': '7.0'})

    def test_deep_search_requires_user_template(self):
        with self.assertRaisesRegex(ValueError, 'not supplied'):
            module('run').recipe_flags(ROOT / 'recipes/deep-search.conf', {})

    def test_values_cannot_inject_config_lines(self):
        with self.assertRaises(ValueError):
            module('run').kv_pairs(['center_x=1\n--seed 42'])
        with self.assertRaises(ValueError):
            module('run').kv_pairs(['center_x=1 # ignored'])

    def test_gate_verifies_hash_and_limits_paths(self):
        workflow = module('workflow')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / 'prep'
            folder.mkdir()
            sample = folder / 'receptor.pdbt'
            sample.write_text('test')
            state = {'stage': 'prepare', 'status': 'ok', 'metrics': {},
                     'artifacts': [{'path': 'prep/receptor.pdbt', 'sha256': workflow.digest(sample)}]}
            (folder / 'status.json').write_text(json.dumps(state))
            self.assertEqual(workflow.check_gate(root, 'prepare'), state)
            sample.write_text('modified')
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                workflow.check_gate(root, 'prepare')
            state['artifacts'][0]['path'] = '../outside'
            (folder / 'status.json').write_text(json.dumps(state))
            with self.assertRaises((OSError, ValueError)):
                workflow.check_gate(root, 'prepare')

    def test_install_rejects_unlaunchable_binary_and_requires_replace(self):
        setup = module('setup')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bad = root / 'obabel-vinardock'
            bad.write_text('#!/bin/sh\necho "GLIBC_2.38 not found" >&2\nexit 1\n')
            bad.chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, 'cannot run'):
                setup.candidate(bad, 'obabel-vinardock')
            target = root / 'installed'
            target.write_text('old')
            with self.assertRaises(FileExistsError):
                setup.copy_verified(bad, target)
            self.assertEqual(target.read_text(), 'old')
            setup.copy_verified(bad, target, replace=True)
            self.assertEqual(target.read_text(), bad.read_text())

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
                    self.assertEqual(setup.probe()['vinardock'], [])
                    self.assertEqual(setup.probe()['obabel-vinardock'], [])
                    binary.rename(root / 'vinardock')
                    self.assertEqual(setup.probe()['vinardock'][0]['path'], str(root / 'vinardock'))
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
                    self.assertEqual(result['obabel-vinardock'], [])
                    self.assertEqual(result['unusable']['obabel-vinardock'][0]['path'], str(binary))
                    self.assertIn('cannot run', result['unusable']['obabel-vinardock'][0]['error'])
            finally:
                os.chdir(old)

    def test_release_assets_cover_both_obabel_builds_and_order_by_glibc(self):
        setup = module('setup')
        for name in ('vinardock', 'obabel-vinardock'):
            modern, legacy, minimum = setup.ASSETS[name]
            self.assertIn(modern, setup.CHECKSUMS)
            self.assertIn(legacy, setup.CHECKSUMS)
            self.assertNotEqual(modern, legacy)
        with patch.object(setup, 'libc_version', return_value=(2, 35)):
            self.assertEqual(setup.release_assets('obabel-vinardock')[0],
                             'obabel-vinardock-linux-amd64-ubuntu22.04')
            self.assertEqual(setup.release_assets('vinardock')[0], 'vinardock-linux-amd64-ubuntu22.04')
        with patch.object(setup, 'libc_version', return_value=(2, 39)):
            self.assertEqual(setup.release_assets('obabel-vinardock')[0], 'obabel-vinardock-linux-amd64')
            self.assertEqual(setup.release_assets('vinardock')[0], 'vinardock-linux-amd64')
        with patch.object(setup, 'libc_version', return_value=()):
            self.assertEqual(setup.release_assets('obabel-vinardock')[0], 'obabel-vinardock-linux-amd64')

    def test_download_falls_back_to_launchable_asset(self):
        setup = module('setup')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            release = root / 'release'
            release.mkdir()
            broken = release / 'obabel-vinardock-linux-amd64'
            broken.write_text('#!/bin/sh\necho "GLIBC_2.38 not found" >&2\nexit 1\n')
            broken.chmod(0o755)
            working = release / 'obabel-vinardock-linux-amd64-ubuntu22.04'
            working.write_text('#!/bin/sh\necho "Open Babel 3.1.1"\n')
            working.chmod(0o755)
            target = root / 'installed'

            def fake_fetch(url, path, expected):
                path.write_bytes((release / Path(url).name).read_bytes())

            with patch.object(setup, 'libc_version', return_value=(2, 39)), \
                    patch.object(setup, 'fetch', side_effect=fake_fetch):
                self.assertEqual(setup.download_binary('obabel-vinardock', target),
                                 'obabel-vinardock-linux-amd64-ubuntu22.04')
            self.assertEqual(target.read_text(), working.read_text())
            working.write_text(broken.read_text())
            working.chmod(0o755)
            with patch.object(setup, 'libc_version', return_value=(2, 39)), \
                    patch.object(setup, 'fetch', side_effect=fake_fetch):
                with self.assertRaisesRegex(RuntimeError, 'no released'):
                    setup.download_binary('obabel-vinardock', root / 'fresh')


    def test_initialise_references_inputs_in_place(self):
        workflow = module('workflow')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            receptor = root / 'rec.pdb'
            receptor.write_text('ATOM\n')
            ligand = root / 'lig.sdf'
            ligand.write_text('ligand\n')
            recipe = root / 'recipe.conf'
            recipe.write_text('--seed {seed}\n')
            run = root / 'run'
            args = argparse.Namespace(receptor=receptor, ligand=[ligand], slot=[], settings=[],
                                      autobox_ligand=None, seed=1, threads=1, conformations=9,
                                      drop_hetatm=False)
            manifest = workflow.initialise(args, {}, recipe, run)
            self.assertFalse((run / 'inputs').exists())
            self.assertEqual(manifest['inputs']['receptor'],
                             {'path': str(receptor), 'sha256': workflow.digest(receptor)})
            self.assertEqual([item['path'] for item in manifest['inputs']['ligands']], [str(ligand)])
            workflow.validate_resume(args, run, {}, recipe)
            ligand.write_text('changed\n')
            with self.assertRaisesRegex(ValueError, 'input contents changed'):
                workflow.validate_resume(args, run, {}, recipe)


if __name__ == '__main__':
    unittest.main()
