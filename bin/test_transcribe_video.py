"""Offline recovery and privacy checks; these tests never call Doubao."""
import argparse
import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import transcribe_video as app


RESULT = {"audio_info": {"duration": 1000}, "result": {"utterances": [
    {"start_time": 0, "text": "你", "words": [{"text": "你", "start_time": 0, "end_time": 100}]}
]}}


class TranscribeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / "test.mp4"
        self.video.write_bytes(b"test video")
        self.work = self.root / "private-run"
        self.parser = argparse.ArgumentParser()

    def run_offline(self, recognize=None, extract=None, retry_uncertain=False):
        def fake_extract(_video, audio, _ffmpeg, _audio_stream):
            audio.write_bytes(b"test audio")
            os.chmod(audio, 0o644)

        with patch.object(app.shutil, "which", return_value="ffmpeg"), \
             patch.object(app, "probe_duration", return_value=(1.0, 1)), \
             patch.object(app, "extract_audio", side_effect=extract or fake_extract), \
             patch.object(app, "load_key", return_value="test-only"), \
             patch.object(app, "verified_for_current_key", return_value=True), \
             patch.object(app, "recognize", side_effect=recognize or (lambda *_, **_kwargs: RESULT)) as call:
            app.run(self.video, self.work, self.parser, retry_uncertain=retry_uncertain)
            return call.call_count

    def test_result_is_private_and_reused_without_second_api_call(self):
        self.assertEqual(self.run_offline(), 1)
        self.assertEqual(self.run_offline(recognize=AssertionError("paid API called twice")), 0)
        self.assertEqual((self.work / "doubao_turbo.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.work / "source_audio.mp3").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.work / "转写预览.txt").stat().st_mode & 0o777, 0o600)

    def test_saved_result_without_manifest_finishes_without_api(self):
        with patch.object(app, "write_private_text", side_effect=OSError("interrupted after response")):
            with self.assertRaises(OSError):
                self.run_offline()
        self.assertEqual(self.run_offline(recognize=AssertionError("paid API called twice")), 0)
        self.assertEqual(json.loads((self.work / "transcription_manifest.json").read_text())["stage"],
                         "transcribed_needs_review")

    def test_legacy_partial_audio_does_not_block_retry(self):
        self.work.mkdir(mode=0o700)
        app.save_result(self.work / "source_manifest.json", {**app.source_record(self.video), "sha256": app.file_sha256(self.video)})
        (self.work / "source_audio.partial.mp3").write_bytes(b"interrupted")
        self.assertEqual(self.run_offline(), 1)

    def test_orphaned_atomic_temp_does_not_block_retry(self):
        self.work.mkdir(mode=0o700)
        (self.work / ".doubao-result-interrupted").write_bytes(b"incomplete")
        self.assertEqual(self.run_offline(), 1)

    def test_public_existing_directory_is_rejected(self):
        self.work.mkdir(mode=0o755)
        with self.assertRaises(SystemExit):
            self.run_offline()

    def test_existing_lock_prevents_parallel_paid_call(self):
        self.work.mkdir(mode=0o700)
        fd = os.open(self.work / ".transcribe.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(SystemExit):
                self.run_offline(recognize=AssertionError("parallel paid call"))
        finally:
            os.close(fd)

    def test_source_change_preserves_paid_response_but_no_success_manifest(self):
        def change_source(_audio, _key, **_kwargs):
            self.video.write_bytes(b"changed video")
            return RESULT
        with self.assertRaisesRegex(RuntimeError, "结果已保存在本机"):
            self.run_offline(recognize=change_source)
        self.assertTrue((self.work / "doubao_turbo.json").exists())
        self.assertFalse((self.work / "transcription_manifest.json").exists())

    def test_audio_change_preserves_paid_response_but_no_success_manifest(self):
        def change_audio(audio, _key, expected_audio_sha256=None):
            self.assertEqual(expected_audio_sha256, app.file_sha256(audio))
            audio.write_bytes(b"wrong audio")
            return RESULT
        with self.assertRaisesRegex(RuntimeError, "提取音频发生变化"):
            self.run_offline(recognize=change_audio)
        self.assertTrue((self.work / "doubao_turbo.json").is_file())
        self.assertFalse((self.work / "transcription_manifest.json").exists())
        with self.assertRaises(SystemExit):
            self.run_offline(recognize=AssertionError("must not resubmit"))

    def test_incomplete_timestamps_preserve_raw_paid_response(self):
        incomplete = {"audio_info": {"duration": 1000}, "result": {"utterances": [
            {"start_time": 0, "text": "你好", "words": [{"text": "你", "start_time": 0, "end_time": 100}]}
        ]}}
        with self.assertRaisesRegex(RuntimeError, "不完整对应"):
            self.run_offline(recognize=lambda *_, **_kwargs: incomplete)
        self.assertTrue((self.work / "doubao_turbo.json").exists())
        self.assertFalse((self.work / "transcription_manifest.json").exists())

    def test_audio_only_file_is_not_accepted_as_video(self):
        stderr = "Duration: 00:00:01.00, start: 0.000000\nStream #0:0: Audio: pcm_s16le"
        with patch.object(app.subprocess, "run", return_value=CompletedProcess([], 1, "", stderr)):
            with self.assertRaisesRegex(ValueError, "视频和音频流"):
                app.probe_duration(self.video, "ffmpeg")

    def test_multitrack_video_requires_explicit_selection(self):
        with patch.object(app.shutil, "which", return_value="ffmpeg"), \
             patch.object(app, "probe_duration", return_value=(1.0, 2)):
            with self.assertRaises(SystemExit):
                app.run(self.video, self.work, self.parser)
        self.assertFalse((self.work / "source_audio.mp3").exists())

    def test_selected_audio_stream_is_recorded(self):
        def fake_extract(_video, audio, _ffmpeg, stream):
            self.assertEqual(stream, 1)
            audio.write_bytes(b"test audio")
        with patch.object(app.shutil, "which", return_value="ffmpeg"), \
             patch.object(app, "probe_duration", return_value=(1.0, 2)), \
             patch.object(app, "extract_audio", side_effect=fake_extract), \
             patch.object(app, "load_key", return_value="test-only"), \
             patch.object(app, "verified_for_current_key", return_value=True), \
             patch.object(app, "recognize", return_value=RESULT):
            app.run(self.video, self.work, self.parser, requested_audio_stream=1)
        self.assertEqual(json.loads((self.work / "source_manifest.json").read_text())["audio_stream"], 1)

    def test_source_content_changed_with_same_size_and_mtime_is_rejected(self):
        self.run_offline()
        stamp = self.video.stat()
        self.video.write_bytes(b"fake video")
        os.utime(self.video, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        with self.assertRaises(SystemExit):
            self.run_offline(recognize=AssertionError("must not resubmit"))

    def test_modified_audio_is_not_used(self):
        self.run_offline()
        (self.work / "source_audio.mp3").write_bytes(b"another audio")
        with self.assertRaises(SystemExit):
            self.run_offline(recognize=AssertionError("must not resubmit"))

    def test_source_hash_detects_change_even_if_filesystem_metadata_matches(self):
        self.run_offline()
        original_record = app.source_record(self.video)
        self.video.write_bytes(b"fake video")
        with patch.object(app, "source_record", return_value=original_record), self.assertRaises(SystemExit):
            self.run_offline(recognize=AssertionError("must not resubmit"))

    def test_modified_result_is_not_used(self):
        self.run_offline()
        result_path = self.work / "doubao_turbo.json"
        result_path.write_text(result_path.read_text().replace("你", "我"))
        with self.assertRaises(SystemExit):
            self.run_offline(recognize=AssertionError("must not resubmit"))

    def test_modified_result_before_manifest_is_not_used(self):
        with patch.object(app, "write_private_text", side_effect=OSError("interrupted after response")):
            with self.assertRaises(OSError):
                self.run_offline()
        result_path = self.work / "doubao_turbo.json"
        result_path.write_text(result_path.read_text().replace("你", "我"))
        with self.assertRaises(SystemExit):
            self.run_offline(recognize=AssertionError("must not resubmit"))

    def test_legacy_result_without_binding_is_preserved_not_reused(self):
        self.work.mkdir(mode=0o700)
        app.save_result(self.work / "source_manifest.json", app.source_record(self.video))
        app.save_result(self.work / "doubao_turbo.json", RESULT)
        with self.assertRaises(SystemExit):
            self.run_offline(recognize=AssertionError("must not resubmit"))
        self.assertEqual(json.loads((self.work / "doubao_turbo.json").read_text()), RESULT)

    def test_audio_symlink_is_rejected_without_changing_external_file(self):
        self.work.mkdir(mode=0o700)
        external = self.root / "external.mp3"
        external.write_bytes(b"keep me")
        os.chmod(external, 0o644)
        (self.work / "source_audio.mp3").symlink_to(external)
        with self.assertRaisesRegex(RuntimeError, "不能是符号链接"):
            self.run_offline()
        self.assertEqual(external.stat().st_mode & 0o777, 0o644)
        self.assertEqual(external.read_bytes(), b"keep me")

    def test_failed_request_needs_explicit_retry_to_avoid_duplicate_charge(self):
        with self.assertRaisesRegex(TimeoutError, "no response"):
            self.run_offline(recognize=TimeoutError("no response"))
        with self.assertRaises(SystemExit):
            self.run_offline(recognize=AssertionError("must not resubmit"))
        self.assertEqual(self.run_offline(retry_uncertain=True), 1)
        self.assertEqual(self.run_offline(recognize=AssertionError("must reuse")), 0)

    def test_fast_mode_never_hashes_entire_video(self):
        original=app.file_sha256
        def bounded(path):
            if path==self.video:raise AssertionError('full video read in fast mode')
            return original(path)
        with patch.object(app,'file_sha256',side_effect=bounded):
            self.run_offline()
            self.run_offline(recognize=AssertionError('must reuse'))
        timing=json.loads((self.work/'timings.json').read_text())
        self.assertFalse(timing['api_called'])
        self.assertIn('revalidate_source_audio_seconds',timing['stages'])

    def test_audio_change_outside_video_samples_is_rejected(self):
        self.run_offline()
        record=app.source_record(self.video)
        fingerprint=app.source_fingerprint(self.video)
        def changed_audio(_video,audio,*_):audio.write_bytes(b'changed source audio')
        with patch.object(app,'source_record',return_value=record), \
             patch.object(app,'source_fingerprint',return_value=fingerprint),self.assertRaises(SystemExit):
            self.run_offline(extract=changed_audio,recognize=AssertionError('must not resubmit'))

    def same_source_reextracted(self, decoded):
        self.run_offline()
        record=app.source_record(self.video)
        fingerprint=app.source_fingerprint(self.video)
        def other_encoder(_video,audio,*_):audio.write_bytes(b'same sound, other ffmpeg build')
        with patch.object(app,'source_record',return_value=record), \
             patch.object(app,'source_fingerprint',return_value=fingerprint), \
             patch.object(app,'decoded_pcm',side_effect=decoded):
            return self.run_offline(extract=other_encoder,recognize=AssertionError('must not resubmit'))

    def test_other_ffmpeg_build_with_same_sound_reuses_result(self):
        sound=app.array('h',[0,1000,-1000,500]*400)
        self.same_source_reextracted(lambda path,_:sound)

    def test_other_ffmpeg_build_with_different_sound_is_rejected(self):
        sounds={}
        def decoded(path,_):
            return sounds.setdefault(path.name.startswith('.source-validation'),
                                     app.array('h',[0,1000,-1000,500]*400 if path.name.startswith('.source') else [700,-200,0,-900]*400))
        with self.assertRaises(SystemExit):self.same_source_reextracted(decoded)

    def test_one_redubbed_second_is_not_accepted_as_the_same_sound(self):
        original=app.array('h',[(i*37)%2000-1000 for i in range(16000*5)])
        changed=app.array('h',original)
        changed[16000*2:16000*3]=app.array('h',[(i*91)%1500-750 for i in range(16000)])
        sounds={True:changed,False:original}
        with self.assertRaises(SystemExit):
            self.same_source_reextracted(lambda path,_:sounds[path.name.startswith('.source')])

    def test_strict_mode_still_uses_complete_source_hash(self):
        with patch.object(app.shutil,'which',return_value='ffmpeg'), \
             patch.object(app,'probe_duration',return_value=(1.,1)), \
             patch.object(app,'extract_audio',side_effect=lambda _,p,*args:p.write_bytes(b'test audio')), \
             patch.object(app,'load_key',return_value='test-only'), \
             patch.object(app,'verified_for_current_key',return_value=True),patch.object(app,'recognize',return_value=RESULT):
            app.run(self.video,self.work,self.parser,full_source_check=True)
        manifest=json.loads((self.work/'transcription_manifest.json').read_text())
        self.assertEqual(manifest['source_verification'],'full_sha256')
        self.assertEqual(manifest['full_source_sha256_verified_this_run'],app.file_sha256(self.video))


if __name__ == "__main__":
    unittest.main()
