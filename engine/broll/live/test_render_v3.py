import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from broll import media
from broll.live.placement import clean_candidates

CUBE='TITLE "identity"\nLUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n'


def generate(path, hdr=False):
    args=['ffmpeg','-v','error','-y','-f','lavfi','-i',"nullsrc=size=64x48:rate=30000/1001:duration=4,geq=lum='16+N':cb=128:cr=128"]
    if hdr:args+=['-vf','setparams=color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc']
    subprocess.run(args+['-c:v','libx264','-crf','0','-pix_fmt','yuv420p',str(path)],check=True,capture_output=True)


class RenderV3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.source=self.base/'a.mov';generate(self.source)
        st=self.source.stat();self.c={'kind':'video','file_path':str(self.source),'size':st.st_size,'mtime_ns':st.st_mtime_ns,'usable_start':0,'usable_end':120}

    def render(self,start=30,n=60,**kw):
        return media.render_live_clip(self.c,self.base/'clips','B01',start,n,resolution=(64,48),**kw)

    def test_rehearsal_uses_actual_in_mid_last_and_one_photo(self):
        shot=dict(spot='B01',file=str(self.source),kind='video',start=100,end=160,source_start=30)
        photo=self.base/'photo.jpg'
        media.command(['ffmpeg','-v','error','-y','-i',str(self.source),'-frames:v','1',str(photo)])
        call=media.command
        seeks=[]
        def record(args):
            if '-ss' in args:seeks.append(float(args[args.index('-ss')+1]))
            return call(args)
        with patch.object(media,'command',side_effect=record):
            output=media.rehearsal_sheet([shot,dict(shot,kind='photo',file=str(photo))],self.base/'check.jpg')
        self.assertEqual(len(seeks),3)
        for actual,frame in zip(seeks,(30,59,89)):
            self.assertAlmostEqual(actual,frame*1001/30000)
        info=media.probe(output,'photo')
        self.assertEqual((info['width'],info['height']),(1440,540))

    def test_asymmetric_handles_boundaries_sidecar(self):
        result=self.render(10,90);m=result['metadata']
        self.assertEqual((m['before_frames'],m['after_frames']),(10,20))
        self.assertEqual(result['startFrame'],10);self.assertEqual(m['total_frames'],120)
        self.assertEqual(json.loads(Path(result['path']).with_suffix('.json').read_text()),m)
        self.assertRegex(Path(result['path']).name,r'^a\.mov__B01_[0-9a-f]{8}\.mov$')
        self.assertLessEqual(m['render_start_seconds']+m['total_frames']*1001/30000,media.probe(self.source)['duration']+1e-7)

    def test_handle_max_and_zero_handle(self):
        result=self.render(60,30);self.assertEqual(result['metadata']['before_frames'],59)
        self.assertLessEqual(result['metadata']['before_seconds'],2)
        self.c.update(usable_start=30,usable_end=90)
        result=self.render();self.assertEqual(result['metadata']['total_frames'],60);self.assertEqual(result['startFrame'],0)

    def test_insufficient_and_out_of_range_rejected(self):
        for start,n in ((0,121),(110,20),(-1,20),(True,20),(10,0)):
            with self.subTest(start=start,n=n),self.assertRaises(ValueError):self.render(start,n)
        self.c['usable_start']=30
        with self.assertRaisesRegex(ValueError,'可用范围'):self.render(20,20)
        self.assertFalse((self.base/'clips').exists())

    def test_no_tpad_and_nonrepeated_first_last_frames(self):
        with patch.object(media,'command',wraps=media.command) as call:result=self.render(0,120)
        commands=[c.args[0] for c in call.call_args_list if c.args[0][0]=='ffmpeg']
        self.assertTrue(commands);self.assertFalse(any('tpad' in ' '.join(c) for c in commands))
        hashes=subprocess.run(['ffmpeg','-v','error','-i',result['path'],'-f','framemd5','-'],check=True,capture_output=True,text=True).stdout
        values=[r.split(',')[-1].strip() for r in hashes.splitlines() if not r.startswith('#')]
        self.assertEqual(len(values),120);self.assertNotEqual(values[0],values[1]);self.assertNotEqual(values[-1],values[-2])

    def test_cache_reuse_hash_identity_changes(self):
        first=self.render();path=Path(first['path']);stamp=path.stat().st_mtime_ns
        with patch.object(media,'command',wraps=media.command) as command:
            self.assertEqual(self.render(),first)
        self.assertFalse(any(c.args[0][0]=='ffmpeg' for c in command.call_args_list));self.assertEqual(stamp,path.stat().st_mtime_ns)
        self.assertNotEqual(self.render(31)['path'],first['path'])
        self.c['usable_start']=20
        self.assertNotEqual(self.render()['path'],first['path'])

    def test_photo_loop(self):
        photo=self.base/'p.png'
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=red:s=64x48','-frames:v','1',str(photo)],check=True,capture_output=True)
        self.c={'kind':'photo','file_path':str(photo)}
        with patch.object(media,'command',wraps=media.command) as call:result=self.render(0,10)
        self.assertEqual(result['metadata']['total_frames'],128)
        self.assertTrue(any('-loop' in c.args[0] for c in call.call_args_list))

    def test_hdr_709(self):
        hdr=self.base/'hdr.mov';generate(hdr,True);self.c.update(file_path=str(hdr));self.c.pop('size');self.c.pop('mtime_ns')
        result=self.render();self.assertTrue(result['metadata']['hdr_to_709']);self.assertFalse(media.probe(result['path'])['hdr'])

    def test_lut_baked_mutually_exclusive_hdr_and_hash(self):
        lut=self.base/'Sony.cube';lut.write_text(CUBE)
        with patch.object(media,'hdr_filter',side_effect=AssertionError('HDR after LUT')):
            first=self.render(lut_path=lut)
        self.assertEqual(len(first['metadata']['lut']['sha256']),64)
        lut.write_text(CUBE+'# changed\n');second=self.render(lut_path=lut)
        self.assertNotEqual(first['path'],second['path'])

    def test_log_requires_lut(self):
        self.c['is_log']=True
        with self.assertRaisesRegex(ValueError,'跳过'):self.render()

    def test_changed_source_and_lut_during_render(self):
        lut=self.base/'Sony.cube';lut.write_text(CUBE);real=media.command
        def mutate(args):
            result=real(args)
            if args[0]=='ffmpeg':lut.write_text(CUBE+'# mutated\n')
            return result
        with patch.object(media,'command',side_effect=mutate),self.assertRaisesRegex(RuntimeError,'LUT.*改动'):self.render(lut_path=lut)
        self.assertFalse(list((self.base/'clips').glob('*.mov')))
        self.c['size']+=1
        with self.assertRaisesRegex(RuntimeError,'身份'):self.render()

    def test_clean_after_render_keeps_referenced(self):
        result=self.render();files=list((self.base/'clips').glob('*'))
        self.assertEqual(clean_candidates(files,{result['path']}),[])
        self.assertEqual(len(clean_candidates(files,set())),2)

    def test_short_decoded_output_never_padded(self):
        real=media.command
        def short(args):
            if args[0]=='ffmpeg' and '-frames:v' in args:
                args=list(args);args[args.index('-frames:v')+1]='1'
            return real(args)
        with patch.object(media,'command',side_effect=short),self.assertRaisesRegex(RuntimeError,'禁止补末帧'):self.render()
        self.assertFalse(list((self.base/'clips').glob('*.mov')))

    def test_other_rate_and_vfr_render_actual(self):
        for mode in ('59.94','vfr'):
            source=self.base/(mode+'.mov')
            args=['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=64x48:rate=60000/1001:duration=4']
            if mode=='vfr':args+=['-vf',"select='if(lt(t,2),1,not(mod(n,2)))'",'-fps_mode','vfr']
            subprocess.run(args+['-c:v','libx264',str(source)],check=True,capture_output=True)
            self.c={'kind':'video','file_path':str(source),'usable_start':0,'usable_end':119}
            result=self.render(30,60)
            self.assertEqual(media.probe(result['path'])['fps_num'],30000)
            self.assertFalse(media.probe(result['path'])['vfr'])

    def test_lut_path_chinese_and_apostrophe(self):
        lut=self.base/"Sony's 中文.cube";lut.write_text(CUBE)
        self.assertTrue(Path(self.render(lut_path=lut)['path']).is_file())

    def test_changed_source_during_generation(self):
        real=media.command
        def mutate(args):
            result=real(args)
            if args[0]=='ffmpeg':self.source.write_bytes(b'changed')
            return result
        with patch.object(media,'command',side_effect=mutate),self.assertRaisesRegex(RuntimeError,'素材.*改动'):self.render()
        self.assertFalse(list((self.base/'clips').glob('*.mov')))

    def test_default_actual_handles_and_used_range_guard(self):
        self.c.pop('usable_start');self.c.pop('usable_end')
        result=self.render(30,60)
        self.assertEqual((result['metadata']['before_frames'],result['metadata']['after_frames']),(30,30))
        self.c['available_ranges']=[[0,.5],[2.5,4.004]]
        with self.assertRaisesRegex(ValueError,'已用源区间'):self.render(30,60)

    def test_named_input_lut_cannot_be_silently_ignored(self):
        self.c['input_lut']='Sony'
        with self.assertRaisesRegex(ValueError,'尚未解析'):self.render()
