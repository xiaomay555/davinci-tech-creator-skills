from pathlib import Path
import tempfile
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from broll.live.placement import choose_placement, select_lut, clean_candidates, prepare_placement


def row(**kw):
    return dict({'kind':'video','fps':29.97,'in_media_pool':True,'vfr':False,'file_path':'/source/a.mov'},**kw)


class PlacementTests(unittest.TestCase):
    def test_direct_native_frame_conversion(self):
        result=choose_placement(row(),90,100)
        self.assertEqual(result['method'],'direct');self.assertEqual(result['source_start_frame'],90)
        result=choose_placement(row(fps=59.94),90,100,direct_fps=[29.97,59.94])
        self.assertEqual(result['method'],'direct');self.assertEqual(result['source_start_frame'],180)

    def test_render_reasons(self):
        for change in ({'vfr':True},{'hdr':True},{'fps':59.94},{'fps':30}):
            with self.subTest(change=change):self.assertEqual(choose_placement(row(**change),0,100)['method'],'render')

    def test_photos_and_pool_external_ordinary_sources_use_original(self):
        for change in ({'kind':'photo'}, {'in_media_pool':False}):
            self.assertEqual(choose_placement(row(**change),30,100)['method'],'direct')
        self.assertEqual(choose_placement(row(kind='photo'),30,100)['source_start_frame'],0)

    def test_probe_flags(self):
        self.assertEqual(choose_placement(row(probe={'hdr':True}),0,100)['method'],'render')
        for n in (-1,True):
            with self.assertRaises(ValueError):choose_placement(row(),n,100)

    def test_recursive_lut_filename(self):
        with tempfile.TemporaryDirectory() as name:
            base=Path(name);folder=base/'sony';folder.mkdir();lut=folder/'Sony.CUBE';lut.write_text('cube')
            result=select_lut(row(input_lut='sony/Sony'),lut_dirs=[base])
            self.assertEqual(result['status'],'ok');self.assertEqual(Path(result['lut_path']),lut.resolve())

    def test_log_missing_and_named_lut_not_found(self):
        for candidate in (row(is_log=True),row(input_lut='missing')):
            result=select_lut(candidate,lut_dirs=[]);self.assertEqual(result['status'],'skip');self.assertIn('跳过',result['reason'])
        self.assertEqual(select_lut(row(input_lut='None'),lut_dirs=[])['status'],'ok')

    def test_external_folder_rule_and_ambiguous(self):
        with tempfile.TemporaryDirectory() as name:
            base=Path(name);(base/'Sony.cube').write_text('cube');(base/'Other.cube').write_text('cube')
            r=row(in_media_pool=False,is_log=True,file_path='/source/a.mov')
            rules=[{'folder':'/','lut':'Other'},{'folder':'/source','lut':'Sony'}]
            self.assertEqual(Path(select_lut(r,folder_rules=rules,lut_dirs=[base])['lut_path']).name,'Sony.cube')
            sub=base/'nested';sub.mkdir();(sub/'Sony.cube').write_text('duplicate')
            self.assertEqual(select_lut(r,folder_rules=rules,lut_dirs=[base])['status'],'skip')

    def test_clean_reference_pairs_only_no_io(self):
        files=['/clips/a.mov','/clips/a.json','/clips/b.mov','/clips/b.json','/clips/noise.txt','/clips/待清理/c.mov']
        result=clean_candidates(files,{'/clips/a.mov'})
        self.assertEqual([r['path'] for r in result],['/clips/b.mov','/clips/b.json'])
        self.assertEqual(result[0]['destination'],'/clips/待清理/b.mov')

    def test_prepare_skips_log_keeps_direct_pool_lut(self):
        self.assertEqual(prepare_placement(row(is_log=True),0,100)['method'],'skip')
        self.assertEqual(prepare_placement(row(is_log=True,input_lut='Sony'),0,100,lut_dirs=[])['method'],'direct')
        self.assertEqual(prepare_placement(row(fps=30,input_lut='missing'),0,100,lut_dirs=[])['method'],'skip')

    def test_unknown_vfr_not_assumed_constant(self):
        c=row();del c['vfr']
        self.assertEqual(choose_placement(c,0,100)['method'],'render')
