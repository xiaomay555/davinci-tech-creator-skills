import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
import edit_project


class ReviewServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run_dir = Path(self.tmp.name)

    def write(self, **info):
        (self.run_dir / '审核网址.txt').write_text(json.dumps(info))

    def test_no_file_or_dead_process_means_not_running(self):
        self.assertIsNone(edit_project.service_info(self.run_dir))
        self.write(url='u', pid=999999, host=socket.gethostname())
        self.assertIsNone(edit_project.service_info(self.run_dir))

    def test_unrelated_live_process_is_not_mistaken_for_the_service(self):
        self.write(url='u', pid=os.getpid(), host=socket.gethostname())
        self.assertIsNone(edit_project.service_info(self.run_dir))

    def test_service_on_the_other_mac_is_reported(self):
        self.write(url='u', pid=1, host='另一台Mac')
        self.assertTrue(edit_project.service_info(self.run_dir)['other_host'])

    def test_free_port_skips_a_port_in_use(self):
        with socket.socket() as busy:
            busy.bind(('127.0.0.1', 0)); busy.listen()
            port = busy.getsockname()[1]
            self.assertNotEqual(edit_project.free_port(port), port)


class CleanTests(unittest.TestCase):
    def test_only_regenerable_files_are_listed_and_delivery_packages_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / '审核'
            (run / 'mvp_deliveries' / 'Clip_old').mkdir(parents=True)
            (run / 'mvp_deliveries' / 'Clip_new').mkdir()
            (run / 'latest_delivery.json').write_text(json.dumps({'package': str(run / 'mvp_deliveries' / 'Clip_new')}))
            for name in ('audio_asr.wav', 'character_alignment.json', 'review_v2_state.json'):
                (run / name).write_text('x')
            (run / 'review_state_backups').mkdir()
            for i in range(22):
                (run / 'review_state_backups' / f'{i:02d}.json').write_text('x'); os.utime(run / 'review_state_backups' / f'{i:02d}.json', (i, i))
            (run.parent/'B-roll片段').mkdir()
            (run.parent/'B-roll片段'/'keep.mov').write_bytes(b'clip')
            (run/'mvp_deliveries/Clip_old/broll_timeline.json').write_text(json.dumps({'items':[{'media':{'path':str(run.parent/'B-roll片段/keep.mov')}}]}))
            listed = {p.relative_to(run).as_posix() for p, _, _ in edit_project.cleanable(run)}
            # delivery packages are never listed: they hold the pre-import backup and the hand-edit check data
            self.assertEqual(listed, {'audio_asr.wav', 'review_state_backups/00.json', 'review_state_backups/01.json'})

    def test_clean_lists_only_unreferenced_clips_and_partial_and_requires_yes(self):
        from unittest.mock import patch
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp)/'审核';run.mkdir();clips=run.parent/'B-roll片段';clips.mkdir()
            for name in ('keep.mov','old.mov','.clip-one.partial'): (clips/name).write_bytes(b'clip')
            package=run/'mvp_deliveries/old';package.mkdir(parents=True)
            (package/'broll_timeline.json').write_text(json.dumps({'items':[{'media':{'path':str(clips/'keep.mov')}}]}))
            listed={p.name for p,_,_ in edit_project.cleanable(run)}
            self.assertEqual(listed,{'old.mov','.clip-one.partial'})
            with patch.object(edit_project,'service_info',return_value=None),contextlib.redirect_stdout(io.StringIO()):
                edit_project.clean(run,False)
                self.assertTrue((clips/'old.mov').exists());self.assertTrue((clips/'.clip-one.partial').exists())
                edit_project.clean(run,True)
            self.assertEqual({p.name for p in clips.iterdir()},{'keep.mov'})
            self.assertTrue((package/'broll_timeline.json').exists())

    def test_unreadable_clip_references_abort_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp)/'审核';package=run/'mvp_deliveries/old';package.mkdir(parents=True)
            clips=run.parent/'B-roll片段';clips.mkdir();(clips/'keep.mov').write_bytes(b'clip')
            (package/'broll_timeline.json').write_text('{')
            with self.assertRaisesRegex(ValueError,'停止清理'):edit_project.cleanable(run)
            self.assertTrue((clips/'keep.mov').exists())


class CleanLockTests(unittest.TestCase):
    def test_clean_refuses_while_a_delivery_runs(self):
        import sys
        sys.path.insert(0, str(Path(edit_project.__file__).resolve().parents[1] / 'engine' / 'src'))
        from prepare_mvp_delivery import exclusive_lock
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / 'audio_asr.wav').write_text('x'); (run / 'character_alignment.json').write_text('{}')
            with exclusive_lock(run / '.delivering.lock'), self.assertRaises(SystemExit):
                edit_project.clean(run, True)
            self.assertTrue((run / 'audio_asr.wav').exists())


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run_dir = Path(self.tmp.name)

    def test_finish_needs_selections_first(self):
        with self.assertRaisesRegex(SystemExit, 'review_decisions'):
            edit_project.finish(self.run_dir.parent, self.run_dir, None)

    def test_failed_background_alignment_is_reported_in_chinese(self):
        (self.run_dir / 'align.log').write_text('Loading model\nRuntimeError: out of memory\n')
        with self.assertRaisesRegex(SystemExit, '声学对齐没有完成：RuntimeError: out of memory'):
            edit_project.wait_for_alignment(self.run_dir)


if __name__ == '__main__':
    unittest.main()
