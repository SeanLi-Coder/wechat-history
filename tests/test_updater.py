"""Exercise signed delta updates using synthetic applications and signing keys."""

import base64
import copy
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import random
import shutil
import stat
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.build_update import build_update, load_private_key
from vault import updater


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="shiliao-update-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.key = Ed25519PrivateKey.generate()
        self.public = base64.b64encode(self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
        self.base = self.root / "Installed App.app"
        self.target = self.root / "Target App.app"
        self.make_bundle(self.base, "0.8.0", {"stable.bin": random.Random(123).randbytes(512 * 1024),
                                            "changed.txt": b"old", "removed.txt": b"removed"})
        self.make_bundle(self.target, "0.8.1", {"stable.bin": (self.base / "Contents/Resources/stable.bin").read_bytes(),
                                              "changed.txt": b"new", "added.txt": b"new file"})
        for bundle in (self.base, self.target):
            (bundle / "Contents/Resources/link").symlink_to("changed.txt")
        self.base_zip, self.target_zip = self.root / "base.app.zip", self.root / "target.app.zip"
        self.package(self.base, self.base_zip)
        self.package(self.target, self.target_zip)
        self.output = self.root / "release"
        self.metrics = build_update(self.base_zip, self.target_zip, self.output, self.key,
                                    "https://github.com/SeanLi-Coder/wechat-history/releases/download/v0.8.1", 801)
        self.envelope = json.loads((self.output / "update-arm64.json").read_text())
        self.release = self.envelope["signed"]
        self.delta = next(self.output.glob('*.delta.zip'))
        self.state = self.root / "state"

    def make_bundle(self, bundle, version, resources):
        (bundle / "Contents/MacOS").mkdir(parents=True)
        (bundle / "Contents/Resources/python/bin").mkdir(parents=True)
        info = {"CFBundleIdentifier": updater.APP_ID, "CFBundleShortVersionString": version,
                "CFBundleExecutable": "Shiliao", "CFBundlePackageType": "APPL",
                "ShiliaoBundledPython": "python/bin/python3", "LSMinimumSystemVersion": "15.0"}
        (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
        for name in ("Contents/MacOS/Shiliao", "Contents/Resources/python/bin/python3"):
            (bundle / name).write_bytes(b"synthetic executable")
            (bundle / name).chmod(0o755)
        for name, data in resources.items():
            (bundle / "Contents/Resources" / name).write_bytes(data)

    def package(self, bundle, path):
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for p in sorted(bundle.rglob('*')):
                if p.is_symlink():
                    member = zipfile.ZipInfo(bundle.name + "/" + p.relative_to(bundle).as_posix())
                    member.create_system = 3
                    member.external_attr = (stat.S_IFLNK | stat.S_IMODE(p.lstat().st_mode)) << 16
                    archive.writestr(member, os.readlink(p))
                elif p.is_file():
                    archive.write(p, bundle.name + "/" + p.relative_to(bundle).as_posix())

    def sign(self, release):
        return {"signed": release, "signature": base64.b64encode(self.key.sign(updater.canonical(release))).decode()}

    def instance(self):
        with patch.object(updater.platform, 'system', return_value='Darwin'), patch.object(updater.platform, 'machine', return_value='arm64'):
            return updater.Updater(self.state, self.base, public_key_b64=self.public)

    def fake_fetch(self, url, destination, limit, progress=None):
        if url == updater.FEED_URL:
            data = updater.canonical(self.envelope)
        elif url == self.release['full']['url']:
            data = self.target_zip.read_bytes()
        elif url == self.release['deltas'][0]['asset']['url']:
            data = self.delta.read_bytes()
        else:
            raise AssertionError('Unexpected synthetic URL')
        self.assertLessEqual(len(data), limit)
        Path(destination).write_bytes(data)
        if progress:
            progress(len(data))

    def ready(self):
        app = self.instance()
        with patch.object(updater, 'fetch', side_effect=self.fake_fetch), patch.object(updater, 'verify_bundle'):
            app.check()
            app.download()
        return app

    def test_delta_roundtrip_is_real_and_much_smaller(self):
        release = updater.verify_release(self.envelope, self.public)
        self.assertLess(self.metrics['delta_bytes'], self.metrics['full_bytes'] // 20)
        self.assertEqual(self.metrics['changed_files'], 3)
        with zipfile.ZipFile(self.delta) as archive:
            self.assertNotIn('payload/Contents/Resources/stable.bin', archive.namelist())
            self.assertNotIn('payload/Contents/Resources/removed.txt', archive.namelist())
        destination = self.root / 'Rebuilt.app'
        with patch.object(updater, 'verify_bundle') as verify:
            updater.reconstruct(self.base, self.delta, destination, release, 'delta')
        self.assertEqual(updater.inventory(destination), updater.inventory(self.target))
        self.assertEqual(os.readlink(destination / 'Contents/Resources/link'), 'changed.txt')
        self.assertFalse((destination / 'Contents/Resources/removed.txt').exists())
        self.assertEqual((self.base / 'Contents/Resources/changed.txt').read_bytes(), b'old')
        verify.assert_called_once_with(destination, '0.8.1')

    def test_full_roundtrip(self):
        destination = self.root / 'Full.app'
        with patch.object(updater, 'verify_bundle'):
            updater.reconstruct(self.base, self.target_zip, destination, self.release, 'full')
        self.assertEqual(updater.inventory(destination), self.release['target']['files'])

    def test_ditto_style_unflagged_utf8_names_have_identical_base_tree(self):
        base, target = self.root / '拾聊旧.app', self.root / '拾聊新.app'
        shutil.copytree(self.base, base, symlinks=True);shutil.copytree(self.target, target, symlinks=True)
        for folder in (base, target):(folder / 'Contents/Resources/照片.txt').write_bytes(b'unchanged')
        before, after = self.root / 'chinese-before.zip', self.root / 'chinese-after.zip'
        for folder, path in ((base, before), (target, after)):
            self.package(folder, path)
            data = bytearray(path.read_bytes())
            with zipfile.ZipFile(path) as archive:
                position = archive.start_dir
                for member in archive.infolist():
                    flag = struct.unpack_from('<H', data, member.header_offset + 6)[0]
                    struct.pack_into('<H', data, member.header_offset + 6, flag & ~0x800)
                    flag = struct.unpack_from('<H', data, position + 8)[0]
                    struct.pack_into('<H', data, position + 8, flag & ~0x800)
                    name_length, extra_length, comment_length = struct.unpack_from('<HHH', data, position + 28)
                    position += 46 + name_length + extra_length + comment_length
            path.write_bytes(data)
            self.assertEqual(updater.zip_inventory(path)[1], updater.inventory(folder))
        output = self.root / 'chinese-release'
        build_update(before, after, output, self.key,
                     'https://github.com/SeanLi-Coder/wechat-history/releases/download/v0.8.1', 801)
        release = json.loads((output / 'update-arm64.json').read_text())['signed']
        self.assertEqual(release['deltas'][0]['base_tree_sha256'], updater.tree_digest(updater.inventory(base)))
        with patch.object(updater, 'verify_bundle'):
            updater.reconstruct(base, next(output.glob('*.delta.zip')), self.root / '还原.app', release, 'delta')
        self.assertEqual(updater.inventory(self.root / '还原.app'), updater.inventory(target))

    def test_signature_tampering_and_wrong_key_rejected(self):
        changed = copy.deepcopy(self.envelope)
        changed['signed']['version'] = '9.9.9'
        for envelope, public in [(changed, self.public), (self.envelope, base64.b64encode(bytes(32)).decode())]:
            with self.assertRaisesRegex(updater.UpdateError, 'invalid_update_signature'):
                updater.verify_release(envelope, public)

    def test_expired_wrong_arch_and_sequence_rollback_rejected(self):
        for field, value in [('expires', '2001-01-01T00:00:00+00:00'), ('architecture', 'x86_64'), ('app_id', 'wrong.app')]:
            with self.subTest(field=field):
                release = copy.deepcopy(self.release);release[field] = value
                with self.assertRaises(updater.UpdateError):
                    updater.verify_release(self.sign(release), self.public)
        with self.assertRaisesRegex(updater.UpdateError, 'rollback'):
            updater.verify_release(self.envelope, self.public, minimum_sequence=802)

    def test_duplicate_json_and_nonfinite_values_rejected(self):
        for raw in [b'{"schema":1,"schema":2}', b'{"x":NaN}', b'{"x":Infinity}']:
            with self.assertRaises(updater.UpdateError):updater.parse_json(raw)

    def test_path_and_symlink_parent_escape_rejected(self):
        for value in ['../secret', '/absolute', 'Contents/../secret', 'Contents/x\ny', 'Contents/x\\y']:
            with self.subTest(value=value), self.assertRaises(updater.UpdateError):updater.safe_path(value)
        bad = [{'path': 'Contents/link', 'type': 'symlink', 'mode': 0o777, 'target': '../outside'}]
        with self.assertRaises(updater.UpdateError):updater.validate_tree(bad)
        bad = [{'path': 'Contents/link', 'type': 'symlink', 'mode': 0o777, 'target': 'other'},
               {'path': 'Contents/link/data', 'type': 'file', 'mode': 0o644, 'size': 0, 'sha256': hashlib.sha256(b'').hexdigest()}]
        with self.assertRaisesRegex(updater.UpdateError, 'parent'):updater.validate_tree(bad)

    def test_case_insensitive_duplicate_and_setuid_rejected(self):
        row = {'path': 'Contents/Data', 'type': 'file', 'mode': 0o644, 'size': 0, 'sha256': hashlib.sha256(b'').hexdigest()}
        with self.assertRaises(updater.UpdateError):updater.validate_tree([row, {**row, 'path': 'Contents/data'}])
        with self.assertRaises(updater.UpdateError):updater.validate_tree([{**row, 'mode': 0o4755}])

    def test_changed_reused_file_aborts_without_partial_target(self):
        (self.base / 'Contents/Resources/stable.bin').write_bytes(b'changed after selection')
        destination = self.root / 'Failed.app'
        with patch.object(updater, 'verify_bundle'), self.assertRaises(updater.UpdateError):
            updater.reconstruct(self.base, self.delta, destination, self.release, 'delta')
        self.assertFalse(destination.exists())

    def test_malicious_patch_member_rejected_and_cleaned(self):
        bad = self.root / 'bad.zip'
        with zipfile.ZipFile(bad, 'w') as archive:archive.writestr('payload/../../outside', b'bad')
        destination = self.root / 'Failed.app'
        with self.assertRaises(updater.UpdateError):
            updater.reconstruct(self.base, bad, destination, self.release, 'delta')
        self.assertFalse(destination.exists());self.assertFalse((self.root / 'outside').exists())

    def test_existing_destination_never_overwritten(self):
        original = updater.inventory(self.base)
        with self.assertRaisesRegex(updater.UpdateError, 'target_exists'):
            updater.reconstruct(self.base, self.delta, self.base, self.release, 'delta')
        self.assertEqual(updater.inventory(self.base), original)

    def test_check_and_download_only_stage_and_cached_ready_avoids_network(self):
        app = self.ready()
        self.assertEqual(app.status()['status'], 'ready')
        self.assertEqual(app.status()['download_kind'], 'delta')
        self.assertEqual(app.status()['downloaded_bytes'], self.metrics['delta_bytes'])
        self.assertEqual((self.base / 'Contents/Resources/changed.txt').read_bytes(), b'old')
        with patch.object(updater, 'fetch') as fetch, patch.object(updater, 'verify_bundle'):
            self.assertEqual(app.download()['status'], 'ready')
            fetch.assert_not_called()
        self.assertEqual(self.instance().status()['status'], 'ready')

    def test_changed_base_automatically_uses_full_package(self):
        (self.base / 'Contents/Resources/stable.bin').write_bytes(b'modified local base')
        app = self.ready()
        self.assertEqual(app.status()['download_kind'], 'full')
        self.assertEqual(updater.inventory(app.root / 'pending.app'), updater.inventory(self.target))

    def test_package_checksum_failure_cannot_prepare_or_modify_app(self):
        app = self.instance()
        with patch.object(updater, 'fetch', side_effect=self.fake_fetch):app.check()
        def corrupt(url, path, limit, progress=None):Path(path).write_bytes(b'corrupt')
        with patch.object(updater, 'fetch', side_effect=corrupt), self.assertRaisesRegex(updater.UpdateError, 'package_hash_mismatch'):
            app.download()
        self.assertFalse((app.root / 'pending.app').exists())
        self.assertEqual((self.base / 'Contents/Resources/changed.txt').read_bytes(), b'old')

    def test_previous_trusted_sequence_persists_across_instances(self):
        app = self.instance()
        with patch.object(updater, 'fetch', side_effect=self.fake_fetch):app.check()
        release = copy.deepcopy(self.release);release['sequence'] = 800
        self.envelope = self.sign(release)
        with patch.object(updater, 'fetch', side_effect=self.fake_fetch), self.assertRaisesRegex(updater.UpdateError, 'rollback'):
            self.instance().check()

    def test_same_sequence_cannot_be_reused_for_different_signed_metadata(self):
        app = self.instance()
        with patch.object(updater, 'fetch', side_effect=self.fake_fetch):app.check()
        release = copy.deepcopy(self.release)
        release['expires'] = (datetime.now(timezone.utc) + timedelta(days=200)).isoformat()
        self.envelope = self.sign(release)
        with patch.object(updater, 'fetch', side_effect=self.fake_fetch), self.assertRaisesRegex(updater.UpdateError, 'sequence_conflict'):
            self.instance().check()

    def test_install_rejects_busy_operation_and_keeps_old_app(self):
        app = self.ready()
        descriptor = os.open(self.state / 'operation.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(updater, 'verify_bundle'), patch.object(updater.subprocess, 'Popen') as spawn:
                with self.assertRaisesRegex(updater.UpdateError, 'operation_busy'):app.launch_install(parent_pid=0)
                spawn.assert_not_called()
        finally:os.close(descriptor)
        self.assertEqual((self.base / 'Contents/Resources/changed.txt').read_bytes(), b'old')

    def test_install_rechecks_staged_files_before_spawning(self):
        app = self.ready()
        (app.root / 'pending.app/Contents/Resources/added.txt').write_bytes(b'tampered')
        with patch.object(updater.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(updater.UpdateError, 'staged_bundle_changed'):app.launch_install(parent_pid=0)
            spawn.assert_not_called()

    def test_install_spawns_independent_shell_with_inherited_operation_lock(self):
        app = self.ready()
        with patch.object(updater, 'verify_bundle'), patch.object(updater.subprocess, 'Popen') as spawn:
            spawn.return_value.pid = 123
            result = app.launch_install(parent_pid=0)
        self.assertEqual(result['status'], 'installing')
        self.assertEqual(spawn.call_args.args[0][0], '/bin/sh')
        self.assertEqual(len(spawn.call_args.kwargs['pass_fds']), 1)
        self.assertTrue(spawn.call_args.kwargs['start_new_session'])
        self.assertEqual((self.base / 'Contents/Resources/changed.txt').read_bytes(), b'old')

    def test_real_waiting_helper_retains_operation_lock_after_parent_closes_descriptor(self):
        app = self.ready()
        real_spawn = subprocess.Popen
        children = []
        def spawn(*args, **kwargs):
            child = real_spawn(*args, **kwargs);children.append(child);return child
        other = None
        try:
            with patch.object(updater, 'verify_bundle'), patch.object(updater.subprocess, 'Popen', side_effect=spawn):
                app.launch_install(parent_pid=os.getpid())
            other = os.open(self.state / 'operation.lock', os.O_RDWR)
            with self.assertRaises(BlockingIOError):fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual((self.base / 'Contents/Resources/changed.txt').read_bytes(), b'old')
        finally:
            for child in children:
                child.terminate();child.wait(timeout=5)
            if other is not None:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.close(other)

    def test_url_restrictions_and_no_credentials(self):
        for url in ['http://github.com/SeanLi-Coder/wechat-history/releases/download/x/a',
                    'https://example.com/a', 'https://github.com/another/repo/releases/download/x/a',
                    'https://user:password@github.com/SeanLi-Coder/wechat-history/releases/download/x/a',
                    'https://github.com:444/SeanLi-Coder/wechat-history/releases/download/x/a']:
            with self.subTest(url=url), self.assertRaises(updater.UpdateError):updater._url(url)

    def test_bundle_architecture_query_uses_input_first_and_exact_tokens(self):
        binaries = [str(self.base / 'Contents/MacOS/Shiliao'),
                    str(self.base / 'Contents/Resources/python/bin/python3')]
        for output in ['arm64\n', 'x86_64 arm64\n']:
            calls = []
            def run(arguments, **kwargs):
                calls.append(arguments)
                if arguments[0] == '/usr/bin/lipo':
                    self.assertIn(arguments[1], binaries)
                    self.assertEqual(arguments[2:], ['-archs'])
                    self.assertTrue(kwargs['text'])
                    return subprocess.CompletedProcess(arguments, 0, stdout=output, stderr='')
                return subprocess.CompletedProcess(arguments, 0, stdout='', stderr='')
            with self.subTest(output=output), patch.object(updater.subprocess, 'run', side_effect=run):
                updater.verify_bundle(self.base, '0.8.0')
            self.assertEqual([call[1] for call in calls[:2]], binaries)
            self.assertEqual(calls[-1][0], '/usr/bin/codesign')

    def test_bundle_rejects_wrong_architecture_in_either_executable(self):
        for outputs in [('x86_64\n',), ('arm64e\n',), ('notarm64\n',), ('\n',), ('arm64\n', 'x86_64\n')]:
            responses = iter(outputs)
            def run(arguments, **kwargs):
                self.assertEqual(arguments[0], '/usr/bin/lipo')
                return subprocess.CompletedProcess(arguments, 0, stdout=next(responses), stderr='')
            with self.subTest(outputs=outputs), patch.object(updater.subprocess, 'run', side_effect=run):
                with self.assertRaisesRegex(updater.UpdateError, 'bundle_signature_or_identity_invalid'):
                    updater.verify_bundle(self.base, '0.8.0')

    def test_build_refuses_existing_outputs_and_older_version(self):
        with self.assertRaises(ValueError):
            build_update(self.base_zip, self.target_zip, self.output, self.key,
                         'https://github.com/SeanLi-Coder/wechat-history/releases/download/v0.8.1', 801)
        with self.assertRaises(ValueError):
            build_update(self.target_zip, self.base_zip, self.root / 'older', self.key,
                         'https://github.com/SeanLi-Coder/wechat-history/releases/download/v0.8.0', 802)

    def test_signing_key_requires_owner_only_permissions_and_no_symlink(self):
        path = self.root / 'synthetic-key.bin'
        path.write_bytes(self.key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                               serialization.NoEncryption()))
        path.chmod(0o600)
        self.assertIsInstance(load_private_key(path), Ed25519PrivateKey)
        path.chmod(0o644)
        with self.assertRaises(ValueError):load_private_key(path)
        link = self.root / 'key-link';link.symlink_to(path)
        with self.assertRaises(ValueError):load_private_key(link)

    def run_helper(self, fail_final_verify=False, tamper=False):
        holder = self.root / "installation's staging"
        holder.mkdir();staged = holder / 'new.app';rollback = holder / 'previous.app'
        shutil.copytree(self.target, staged, symlinks=True)
        rows = updater.inventory(staged)
        sums = holder / 'checksums'
        sums.write_text(''.join(row['sha256']+'  '+row['path']+'\n' for row in rows if row['type']=='file'))
        result = holder / 'result.json'
        lock = os.open(self.root / 'test.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            script = updater.install_script(self.base, staged, rollback, sums, result, 0, lock, '0.8.1', rows)
            verifier = holder / 'verify.sh'
            verifier.write_text('#!/bin/sh\nlast=""\nfor item in "$@"; do last="$item"; done\n' +
                                ('[ "$last" != ' + __import__('shlex').quote(str(self.base)) + ' ]\n' if fail_final_verify else 'exit 0\n'))
            script = script.replace('/usr/bin/codesign', '/bin/sh '+__import__('shlex').quote(str(verifier)))
            script = script.replace('/usr/bin/open', '/usr/bin/true')
            helper = holder / 'helper.sh';helper.write_text(script)
            if tamper:(staged / 'Contents/Resources/added.txt').write_bytes(b'changed')
            run = subprocess.run(['/bin/sh', str(helper)], pass_fds=(lock,), capture_output=True)
        finally:os.close(lock)
        return run, result, rollback

    def test_helper_replaces_bundle_and_retains_recovery_copy(self):
        run, result, rollback = self.run_helper()
        self.assertEqual(run.returncode, 0, run.stderr.decode())
        self.assertEqual((self.base / 'Contents/Resources/changed.txt').read_bytes(), b'new')
        self.assertEqual((rollback / 'Contents/Resources/changed.txt').read_bytes(), b'old')
        self.assertEqual(json.loads(result.read_text())['status'], 'installed')

    def test_helper_rolls_back_when_final_validation_fails(self):
        run, result, rollback = self.run_helper(fail_final_verify=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual((self.base / 'Contents/Resources/changed.txt').read_bytes(), b'old')
        self.assertEqual(json.loads(result.read_text())['error'], 'installation_failed')

    def test_helper_rejects_staging_changed_during_parent_exit_wait(self):
        run, result, rollback = self.run_helper(tamper=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual((self.base / 'Contents/Resources/changed.txt').read_bytes(), b'old')
        self.assertFalse(rollback.exists())


@unittest.skipUnless(__import__('platform').system() == 'Darwin' and __import__('platform').machine() == 'arm64',
                     'Native signature verification requires an Apple Silicon Mac')
class NativeBundleVerificationTests(unittest.TestCase):
    def test_real_ad_hoc_signature_and_arm64_architecture_then_tamper(self):
        with tempfile.TemporaryDirectory(prefix='shiliao-update-native-test-') as temporary:
            root = Path(temporary);bundle = root / 'Synthetic.app'
            (bundle / 'Contents/MacOS').mkdir(parents=True)
            (bundle / 'Contents/Resources/python/bin').mkdir(parents=True)
            info = {'CFBundleIdentifier': updater.APP_ID, 'CFBundleShortVersionString': '0.8.1',
                    'CFBundleExecutable': 'Shiliao', 'CFBundlePackageType': 'APPL',
                    'ShiliaoBundledPython': 'python/bin/python3'}
            (bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
            source = root / 'main.c';source.write_text('int main(void) { return 0; }\n')
            executable = bundle / 'Contents/MacOS/Shiliao'
            subprocess.run(['/usr/bin/clang', '-arch', 'arm64', str(source), '-o', str(executable)], check=True, capture_output=True)
            shutil.copy2(executable, bundle / 'Contents/Resources/python/bin/python3')
            resource = bundle / 'Contents/Resources/test.txt';resource.write_text('synthetic')
            subprocess.run(['/usr/bin/codesign', '--force', '--deep', '--sign', '-', str(bundle)], check=True, capture_output=True)
            updater.verify_bundle(bundle, '0.8.1')
            resource.write_text('tampered')
            with self.assertRaises(updater.UpdateError):updater.verify_bundle(bundle, '0.8.1')


if __name__ == '__main__':
    unittest.main()
