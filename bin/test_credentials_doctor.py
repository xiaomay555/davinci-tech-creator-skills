"""Credential and dependency diagnostics with temporary dummy files only."""
import hashlib
import http.client
import json
import re
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import doctor
import doubao_credentials as credentials


class CredentialAndDoctorTests(unittest.TestCase):
    def test_verification_matches_used_key_during_rotation(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "verification.json"
            old_key, new_key = "dummy-old-key-000000000", "dummy-new-key-000000000"
            marker.write_text(json.dumps({"resource_id": "turbo", "key_sha256": hashlib.sha256(new_key.encode()).hexdigest()}))
            with patch.object(credentials, "VERIFIED_FILE", marker), \
                 patch.object(credentials, "load_key", return_value=new_key):
                self.assertTrue(credentials.verified_for_current_key("turbo"))
                self.assertFalse(credentials.verified_for_current_key("turbo", old_key))

    def test_invalid_marker_does_not_crash_diagnostic(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "verification.json"
            marker.write_text("[]")
            with patch.object(credentials, "VERIFIED_FILE", marker):
                self.assertFalse(credentials.verified_for_current_key("turbo", "dummy-key-0000000000000"))

    def test_reading_key_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "actual"
            target.write_text("dummy-key-0000000000000")
            link = Path(folder) / "key"
            link.symlink_to(target)
            with patch.object(credentials, "KEY_FILE", link), self.assertRaises(RuntimeError):
                credentials.load_key()

    def test_ffmpeg_path_alone_does_not_mean_extraction_works(self):
        with patch.object(doctor.shutil, "which", return_value="/dummy/ffmpeg"), \
             patch.object(doctor.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "aac only", "")):
            _path, ready, reason = doctor.working_ffmpeg()
            self.assertFalse(ready)
            self.assertIn("libmp3lame", reason)

    def test_ffmpeg_timeout_is_reported_without_hanging(self):
        with patch.object(doctor.shutil, "which", return_value="/dummy/ffmpeg"), \
             patch.object(doctor.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffmpeg", 10)):
            self.assertFalse(doctor.working_ffmpeg()[1])

    def test_hdr_check_requires_the_actual_zscale_filter(self):
        with patch.object(doctor.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, "Filters:\n .S zscale V->V Convert colorspace\n", "")):
            self.assertTrue(doctor.ffmpeg_has_zscale('/dummy/ffmpeg'))
        with patch.object(doctor.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, "Filters:\n .. scale V->V resize (zscale unavailable)\n", "")):
            self.assertFalse(doctor.ffmpeg_has_zscale('/dummy/ffmpeg'))

    def test_hdr_check_reports_missing_failed_and_timed_out_ffmpeg(self):
        self.assertFalse(doctor.ffmpeg_has_zscale(None))
        with patch.object(doctor.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "error")):
            self.assertFalse(doctor.ffmpeg_has_zscale('/dummy/ffmpeg'))
        with patch.object(doctor.subprocess, "run", side_effect=subprocess.TimeoutExpired('ffmpeg', 10)):
            self.assertFalse(doctor.ffmpeg_has_zscale('/dummy/ffmpeg'))

    def test_atomic_key_failure_keeps_previous_key(self):
        with tempfile.TemporaryDirectory() as folder:
            key = Path(folder) / "secrets" / "key"
            with patch.object(credentials, "KEY_FILE", key):
                credentials.save_key("dummy-old-key-000000000")
                with patch.object(credentials.os, "replace", side_effect=OSError("disconnected")), self.assertRaises(OSError):
                    credentials.save_key("dummy-new-key-000000000")
                self.assertEqual(key.read_text().strip(), "dummy-old-key-000000000")
                self.assertEqual(key.stat().st_mode & 0o777, 0o600)
                self.assertEqual(key.parent.stat().st_mode & 0o777, 0o700)

    def test_local_key_page_rejects_cross_site_and_bad_requests(self):
        servers, ready = [], threading.Event()
        real_server = credentials.ThreadingHTTPServer

        def capture_server(*args, **kwargs):
            server = real_server(*args, **kwargs)
            servers.append(server)
            ready.set()
            return server

        with tempfile.TemporaryDirectory() as folder:
            key = Path(folder) / "secrets" / "key"
            with patch.object(credentials, "KEY_FILE", key), \
                 patch.object(credentials, "ThreadingHTTPServer", side_effect=capture_server), \
                 patch("builtins.print"):
                thread = threading.Thread(target=credentials.serve, daemon=True)
                thread.start()
                self.assertTrue(ready.wait(5))
                server = servers[0]
                host = f"127.0.0.1:{server.server_port}"

                def request(method, path, body=None, headers=None):
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                    try:
                        connection.request(method, path, body=body, headers=headers or {})
                        response = connection.getresponse()
                        return response.status, response.read().decode()
                    finally:
                        connection.close()

                try:
                    status, page = request("GET", "/")
                    self.assertEqual(status, 200)
                    token = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
                    self.assertEqual(request("GET", "/", headers={"Host": "elsewhere.test"})[0], 403)
                    body = urlencode({"csrf": token, "key": "dummy-key-0000000000000"})
                    self.assertEqual(request("POST", "/save", body, {"Origin": "https://elsewhere.test"})[0], 403)
                    self.assertFalse(key.exists())
                    self.assertEqual(request("POST", "/save", "", {"Content-Length": "wrong"})[0], 400)
                    self.assertEqual(request("POST", "/save", urlencode({"csrf": "错误", "key": "dummy-key-0000000000000"}))[0], 403)
                    self.assertEqual(request("POST", "/save", body, {"Origin": f"http://{host}"})[0], 200)
                    self.assertEqual(key.stat().st_mode & 0o777, 0o600)
                finally:
                    server.shutdown()
                    thread.join(5)
                    self.assertFalse(thread.is_alive())


class DoctorSummaryTests(unittest.TestCase):
    def test_missing_items_name_the_next_step(self):
        checks = {'shared_skill_ready': True,
                  'skills': {name: {'skill_source_present': True, 'shared_skill_ready': True}
                             for name in ('ai-edit', 'a-roll', 'b-roll')},
                  'ffmpeg_can_extract_mp3': True, 'shared_doubao_key_present': True,
                  'shared_doubao_key_access_error': None, 'shared_doubao_key_api_verified': True,
                  'alignment_dependencies_ready': False, 'subtitle_dependencies_ready': True,
                  'resolve_app': '/Applications/DaVinci Resolve', 'resolve_mcp_server': 's', 'resolve_mcp_python': 'p'}
        self.assertEqual(doctor.missing_items(checks), ['声学对齐环境缺失：运行 bash bin/setup_mac.sh'])
        text = doctor.summary({**checks, 'shared_version': 'v', 'video_transcription_ready': True,
                               'missing': doctor.missing_items(checks), 'end_to_end_ready': False})
        self.assertIn('可以完整剪辑并放进达芬奇：否', text)

    def skill_checks(self, missing_source=None, missing_link=None, wrong_link=None):
        with tempfile.TemporaryDirectory() as folder:
            root, codex_home = Path(folder) / 'workspace', Path(folder) / 'codex'
            (codex_home / 'skills').mkdir(parents=True)
            for name in ('ai-edit', 'a-roll', 'b-roll'):
                source = root / 'skills' / name
                source.mkdir(parents=True)
                if name != missing_source:
                    (source / 'SKILL.md').write_text('dummy skill')
                if name != missing_link:
                    (codex_home / 'skills' / name).symlink_to(root / 'elsewhere' if name == wrong_link else source)
            return doctor.collect_skill_checks(root, codex_home)

    def otherwise_ready(self, skill_checks):
        return {**skill_checks, 'ffmpeg_can_extract_mp3': True, 'shared_doubao_key_present': True,
                'shared_doubao_key_access_error': None, 'shared_doubao_key_api_verified': True,
                'alignment_dependencies_ready': True, 'subtitle_dependencies_ready': True,
                'resolve_app': 'app', 'resolve_mcp_server': 'server', 'resolve_mcp_python': 'python'}

    def test_all_three_skill_links_are_ready(self):
        checks = self.skill_checks()
        self.assertEqual(set(checks['skills']), {'ai-edit', 'a-roll', 'b-roll'})
        self.assertTrue(checks['skill_source_present'])
        self.assertTrue(checks['skill_installed_as_nas_link'])
        self.assertTrue(checks['shared_skill_ready'])
        self.assertEqual(doctor.missing_items(self.otherwise_ready(checks)), [])

    def test_missing_hdr_filter_names_the_full_formula(self):
        checks = {**self.otherwise_ready(self.skill_checks()), 'ffmpeg_has_zscale': False}
        missing = doctor.missing_items(checks)
        self.assertEqual(len(missing), 1)
        self.assertIn('ffmpeg-full', missing[0])
        text = doctor.summary({**checks, 'shared_version': 'v', 'video_transcription_ready': True,
                               'missing': missing, 'end_to_end_ready': False})
        self.assertIn('ffmpeg HDR 转换（zscale）：未就绪', text)

    def test_missing_b_roll_link_is_not_ready_and_names_b_roll(self):
        checks = self.skill_checks(missing_link='b-roll')
        self.assertTrue(checks['skill_source_present'])
        self.assertFalse(checks['skill_installed_as_nas_link'])
        self.assertFalse(checks['shared_skill_ready'])
        missing = doctor.missing_items(self.otherwise_ready(checks))
        self.assertEqual(missing, ['Skill b-roll 未正确连接：运行 bash bin/setup_mac.sh'])
        text = doctor.summary({**checks, 'shared_version': 'v', 'video_transcription_ready': True,
                               'missing': missing, 'end_to_end_ready': False})
        self.assertIn('Skill b-roll：未就绪', text)
        self.assertIn('还缺：Skill b-roll 未正确连接', text)

    def test_missing_source_is_not_ready_even_with_correct_link(self):
        checks = self.skill_checks(missing_source='b-roll')
        self.assertFalse(checks['skill_source_present'])
        self.assertTrue(checks['skill_installed_as_nas_link'])
        self.assertFalse(checks['shared_skill_ready'])
        self.assertEqual(doctor.missing_items(self.otherwise_ready(checks)),
                         ['Skill b-roll 源文件缺失：运行 bash bin/setup_mac.sh'])

    def test_link_to_elsewhere_is_not_ready(self):
        checks = self.skill_checks(wrong_link='a-roll')
        self.assertFalse(checks['skill_installed_as_nas_link'])
        self.assertFalse(checks['shared_skill_ready'])
        self.assertEqual(doctor.missing_items(self.otherwise_ready(checks)),
                         ['Skill a-roll 未正确连接：运行 bash bin/setup_mac.sh'])


if __name__ == "__main__":
    unittest.main()
