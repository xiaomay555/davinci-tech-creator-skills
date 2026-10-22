import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from broll import index, media
from broll.test_index import VALUE
from broll.live import candidates, matching, placement
from broll.live.timeline import build_model
from broll.live._fixtures import snapshot, item
from broll.live.test_render_v3 import generate


class PipelineV3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.source=self.base/'a.mov';generate(self.source)
        self.pool=[{'file_path':str(self.source),'kind':'video','fps':29.97,'duration':media.probe(self.source)['duration'],'vfr':False,'input_lut':None}]
        index.build_index_files(self.pool,self.base/'索引')
        self.value=media.read_json(self.base/'索引/index.json');self.model=build_model(snapshot())

    def catalog(self, profile=None):
        files=candidates.collect_candidates(self.model,self.pool,profile)
        return candidates.build_catalog(self.value,files)

    def match(self,catalog):
        return {'version':3,'segments':[{'spot':'B01','lines':[1,1],'shots':[{'end':100,'candidates':[{'segment':next(iter(catalog)),'reason':'对应本句画面'}]}]}],'face':[]}

    def test_index_match_arrange_direct(self):
        catalog=self.catalog();value=self.match(catalog)
        draft=matching.build_match_draft(self.model,catalog)
        self.assertIn('focus',draft['catalog'][0]);self.assertIsNone(draft['catalog'][0]['focus'])
        pending=matching.supplement_ids(value,catalog);self.assertEqual(pending,[next(iter(catalog))])
        rows=[dict(segment=pending[0],**VALUE,subject='centered',focus='sharp',people='none',use='scene',sample_of='')]
        index.build_index_files(self.pool,self.base/'索引',descriptions=rows)
        self.value=media.read_json(self.base/'索引/index.json');catalog=self.catalog()
        self.assertEqual(matching.supplement_ids(value,catalog),[])
        plan=matching.plan_matches(value,self.model,catalog)
        placed=plan['placements'][0];self.assertEqual(placed['status'],'placed')
        decision=placement.prepare_placement(catalog[placed['segment']],placed['source_start'],placed['end']-placed['start'])
        self.assertEqual(decision['method'],'direct');self.assertFalse(plan['flashes'])

    def test_pool_external_direct_and_hdr_derivative_metadata(self):
        catalog=self.catalog();segment=next(iter(catalog));catalog[segment]['in_media_pool']=False
        plan=matching.plan_matches(self.match(catalog),self.model,catalog);placed=plan['placements'][0]
        self.assertEqual(placed['status'],'placed')
        decision=placement.prepare_placement(catalog[segment],placed['source_start'],placed['end']-placed['start'],lut_dirs=[])
        self.assertEqual(decision['method'],'direct')
        catalog[segment]['hdr']=True
        decision=placement.prepare_placement(catalog[segment],placed['source_start'],placed['end']-placed['start'],lut_dirs=[])
        self.assertEqual(decision['method'],'render')
        result=media.render_live_clip(catalog[segment],self.base/'B-roll片段',placed['spot'],placed['source_start'],placed['end']-placed['start'],resolution=(64,48),lut_path=decision['lut_path'])
        self.assertEqual(result['frames'],100);self.assertEqual(result['startFrame'],0)
        self.assertEqual(result['metadata']['after_frames'],20)
        self.assertEqual(placement.clean_candidates([result['path'],str(Path(result['path']).with_suffix('.json'))],{result['path']}),[])

    def test_profile_changes_only_related_candidate(self):
        catalog=self.catalog();ident=next(iter(catalog));catalog['good']=dict(catalog[ident],segment='good',people='none')
        catalog[ident]['people']='host';value=self.match(catalog)
        value['segments'][0]['shots'][0]['candidates'].append({'segment':'good','reason':'安全备用'})
        out=matching.plan_matches(value,self.model,catalog)
        self.assertEqual(out['placements'][0]['segment'],'good');self.assertEqual(out['compliance']['rate'],.5)
        self.assertIn(ident,out['report'])

    def test_used_ranges_survive_into_catalog_and_never_render_across(self):
        snap=snapshot();snap['items'].append(item('自己',0,30,path=str(self.source),source_start=30))
        files=candidates.collect_candidates(snap,self.pool);catalog=candidates.build_catalog(self.value,files)
        self.assertEqual(len(catalog),2)
        spans=sorted((c['usable_start'],c['usable_end']) for c in catalog.values())
        self.assertEqual(spans,[(0,15),(75,120)])
        second=next(c for c in catalog.values() if c['usable_start']==75)
        result=media.render_live_clip(second,self.base/'clips','B02',75,30,resolution=(64,48))
        self.assertEqual(result['metadata']['before_frames'],0);self.assertEqual(result['metadata']['after_frames'],15)
