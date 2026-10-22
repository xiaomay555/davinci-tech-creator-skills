"""Offline check that a read-only NAS cannot discard a paid ASR response."""
import contextlib
import base64
import hashlib
import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import doubao_direct_asr as app


class DirectAsrTests(unittest.TestCase):
    def test_result_survives_failed_nas_verification_write(self):
        with tempfile.TemporaryDirectory() as root:
            audio = Path(root) / "source.mp3"
            audio.write_bytes(b"test audio")
            output = Path(root) / "result.json"
            result = {"audio_info": {"duration": 1000}, "result": {"utterances": [
                {"text": "你", "words": [{"text": "你", "start_time": 0, "end_time": 100}]}
            ]}}
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(app, "load_key", return_value="test-only"), \
                 patch.object(app, "recognize", return_value=result), \
                 patch.object(app, "verified_for_current_key", return_value=False), \
                 patch.object(app, "mark_verified", side_effect=PermissionError("read only")), \
                 patch("sys.argv", ["direct", "--audio", str(audio), "--output", str(output)]), \
                 contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                app.main()
            self.assertTrue(output.is_file())
            self.assertIn('"status": "complete"', stdout.getvalue())
            self.assertIn("结果已保存在本机", stderr.getvalue())

    def test_mismatched_upload_bytes_are_rejected_before_request(self):
        with tempfile.TemporaryDirectory() as root:
            audio = Path(root) / "source.mp3"
            audio.write_bytes(b"wrong audio")
            expected = hashlib.sha256(b"expected audio").hexdigest()
            with patch.object(app, "validate_key", return_value="test-only"), \
                 patch.object(app, "urlopen", side_effect=AssertionError("must not call API")) as request:
                with self.assertRaisesRegex(ValueError, "待上传音频与来源校验不一致"):
                    app.recognize(audio, "test-only", expected_audio_sha256=expected)
                request.assert_not_called()

    def test_verified_bytes_are_the_actual_uploaded_payload(self):
        with tempfile.TemporaryDirectory() as root:
            audio = Path(root) / "source.mp3"
            content = b"expected audio"
            audio.write_bytes(content)
            response = Mock()
            response.headers = {"X-Api-Status-Code": "20000000"}
            response.read.return_value = b'{}'
            def fake_urlopen(request, **_kwargs):
                payload = json.loads(request.data)
                self.assertEqual(base64.b64decode(payload["audio"]["data"]), content)
                audio.write_bytes(b"changed after immutable payload")
                return contextlib.nullcontext(response)
            with patch.object(app, "validate_key", return_value="test-only"), \
                 patch.object(app, "urlopen", side_effect=fake_urlopen):
                self.assertEqual(app.recognize(audio, "test-only", expected_audio_sha256=hashlib.sha256(content).hexdigest()), {})

    def test_incomplete_word_timing_is_rejected(self):
        result = {"audio_info": {"duration": 1000}, "result": {"utterances": [
            {"text": "你好", "words": [{"text": "你", "start_time": 0, "end_time": 100}]}
        ]}}
        with self.assertRaisesRegex(RuntimeError, "不完整对应"):
            app.count_timed_tokens(result)

    def test_missing_entire_utterance_is_rejected(self):
        result = {"audio_info": {"duration": 1000}, "result": {"text": "你。好。", "utterances": [
            {"text": "你。", "words": [{"text": "你", "start_time": 0, "end_time": 100}]}
        ]}}
        with self.assertRaisesRegex(RuntimeError, "全文与逐句"):
            app.count_timed_tokens(result)

    def test_invalid_structures_and_boolean_time_are_rejected(self):
        for result in ([], {"audio_info": None, "result": {}},
                       {"audio_info": {"duration": True}, "result": {"utterances": []}},
                       {"audio_info": {"duration": 1000}, "result": {"utterances": [
                           {"text": "你", "words": [{"text": "你", "start_time": False, "end_time": 100}]}
                       ]}}):
            with self.subTest(result=result), self.assertRaises(RuntimeError):
                app.count_timed_tokens(result)

    def test_invalid_key_is_rejected_without_echo_or_request(self):
        with patch.object(app, "urlopen", side_effect=AssertionError("must not call API")):
            key = "secret-key-not-for-logs中文"
            with self.assertRaises(ValueError) as raised:
                app.recognize("unread.mp3", key)
            self.assertNotIn(key, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
