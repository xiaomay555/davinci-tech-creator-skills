import contextlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import editing_preflight
import edit_project


class PreflightTests(unittest.TestCase):
    def test_missing_alignment_python_fails_without_running(self):
        with patch('editing_preflight.subprocess.run') as process:
            result=editing_preflight.alignment_runtime('/missing/python')
        self.assertFalse(result['alignment_dependencies_ready']);process.assert_not_called()

    def test_alignment_checks_imports_and_reports_versions(self):
        with patch('editing_preflight.subprocess.run',return_value=subprocess.CompletedProcess([],0,'{"qwen-asr":"test"}\n','')):
            result=editing_preflight.alignment_runtime(sys.executable)
        self.assertTrue(result['alignment_dependencies_ready']);self.assertEqual(result['alignment_versions']['qwen-asr'],'test')

    def test_alignment_import_failure_is_controlled(self):
        with patch('editing_preflight.subprocess.run',return_value=subprocess.CompletedProcess([],1,'','missing dependency')):
            self.assertFalse(editing_preflight.alignment_runtime(sys.executable)['alignment_dependencies_ready'])

    def test_delivery_dependency_failure_precedes_setup(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(sys,'argv',['edit_project','deliver','--project-dir',tmp]), \
             patch('prepare_mvp_delivery.exclusive_lock',return_value=contextlib.nullcontext()), \
             patch('run_mvp.resolve_mcp_runtime',return_value=(Path(tmp),Path(sys.executable),Path(tmp)/'server')), \
             patch('run_mvp.node_runtime',side_effect=RuntimeError('missing subtitles')),patch.object(edit_project,'execute') as execute:
            with self.assertRaisesRegex(RuntimeError,'missing subtitles'):edit_project.main()
            # Only the offline proofread check ran; nothing touched Resolve.
            self.assertEqual([c.args[0] for c in execute.call_args_list],['script_check.py'])

    def test_init_missing_alignment_stops_before_paid_transcription(self):
        with tempfile.TemporaryDirectory() as tmp:
            video=Path(tmp)/'video.mp4';video.write_bytes(b'fixture');project=Path(tmp)/'new'
            info={'tracks':[{'handler':'vide','fps':{'num':30000,'den':1001},'constant_presentation_duration':True},{'handler':'soun'}]}
            with patch.object(sys,'argv',['edit_project','init','--project-dir',str(project),'--video',str(video)]),patch.object(edit_project,'probe',return_value=info), \
                 patch.object(edit_project,'alignment_runtime',return_value={'alignment_dependencies_ready':False,'alignment_error':'missing align'}),patch.object(edit_project.subprocess,'run') as paid:
                with self.assertRaises(SystemExit):edit_project.main()
                paid.assert_not_called();self.assertFalse(project.exists())

    def test_init_multi_audio_stops_before_paid_transcription(self):
        with tempfile.TemporaryDirectory() as tmp:
            video=Path(tmp)/'video.mp4';video.write_bytes(b'fixture')
            info={'tracks':[{'handler':'vide','fps':{'num':30000,'den':1001},'constant_presentation_duration':True}]+[{'handler':'soun'}]*2}
            with patch.object(sys,'argv',['edit_project','init','--project-dir',str(Path(tmp)/'new'),'--video',str(video)]),patch.object(edit_project,'probe',return_value=info),patch.object(edit_project.subprocess,'run') as paid:
                with self.assertRaisesRegex(ValueError,'单音轨'):edit_project.main()
                paid.assert_not_called()

if __name__=='__main__':unittest.main()
