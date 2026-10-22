from pathlib import Path
import importlib.util
import sys
import unittest
from unittest.mock import Mock
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
spec=importlib.util.spec_from_file_location('broll_cli',Path(__file__).with_name('broll.py'));cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)

class CLITests(unittest.TestCase):
    def test_all_command_parameters(self):
        for args in (['status'],['plan','--folder','素材'],['apply','--matches','匹配.json'],['adjust','--spot','S01','--use','2'],['adjust','--spot','S01','--face','on'],['adjust','--spot','S01','--audio','off'],['undo','--run','run'],['learn','--approve','--note','保留'],['learn','--forget','last'],['profile'],['at','--playhead'],['at','01:00:01:00'],['clean','--yes']):
            self.assertTrue(cli.parser().parse_args(args).command)
    def test_mutually_exclusive_adjust(self):
        with self.assertRaises(ValueError):cli.parser().parse_args(['adjust','--spot','S01','--use','2','--remove'])
    def test_default_is_dryrun(self):
        self.assertFalse(cli.parser().parse_args(['apply','--matches','a.json']).apply)
        self.assertFalse(cli.parser().parse_args(['undo']).apply)
    def test_more_is_explicit(self):
        self.assertFalse(cli.parser().parse_args(['plan']).more)
        self.assertTrue(cli.parser().parse_args(['plan','--more']).more)
    def test_chinese_error_and_next_step(self):
        err=StringIO();called=Mock()
        with redirect_stderr(err):code=cli.main(['apply'],preflight=called)
        self.assertEqual(code,2);self.assertIn('出错了',err.getvalue());self.assertIn('下一步',err.getvalue());called.assert_not_called()
    def test_each_command_runs_preflight_before_session(self):
        for args in (['status'],['plan'],['apply','--matches','x'],['adjust','--spot','1','--remove'],['undo'],['learn'],['profile'],['at','--playhead'],['clean']):
            called=Mock(side_effect=RuntimeError('环境未准备好'));session=Mock()
            with redirect_stderr(StringIO()):self.assertEqual(cli.main(args,preflight=called,session_type=session),2)
            called.assert_called_once();session.assert_not_called()
    def test_at_requires_exactly_one_input(self):
        for args in (['at'],['at','00:00:00:00','--playhead']):
            with redirect_stderr(StringIO()):self.assertEqual(cli.main(args,preflight=Mock()),2)
