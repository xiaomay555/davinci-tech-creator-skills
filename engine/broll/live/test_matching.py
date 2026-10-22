import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from broll.live import matching
from broll.live._fixtures import snapshot
from broll.live.timeline import build_model
from broll.live._util import digest


def matches():
    return {'version':3,'segments':[{'spot':'B01','lines':[1,1],'shots':[{'end':100,'candidates':[{'segment':'s','reason':'对应动作'}]}]}],'face':[]}


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.model=build_model(snapshot());self.catalog={'s':{'segment':'s','file':'/media/a.mov','usable_start':0,'usable_end':600}}
        self.value=matches()

    def reject(self, value):
        with self.assertRaises(ValueError):matching.validate_matches(value,self.model,self.catalog)

    def test_draft_fields_limits_warning_and_no_mutation(self):
        profile={k:list(range(10)) for k in ('semantic_guidance','positive','negative')}
        model=copy.deepcopy(self.model)
        draft=matching.build_match_draft(self.model,self.catalog,profile)
        self.assertEqual(draft['profile_hash'],digest(profile))
        for key in profile:self.assertEqual(len(draft[key]),6)
        self.assertEqual(draft['lines'][0]['v1_segments'][0]['index'],1)
        self.assertEqual(draft['content_warning'],matching.WARNING);self.assertEqual(model,self.model)

    def test_reject_top_schema(self):
        for value in ([],dict(self.value,extra=1),dict(self.value,version=True),dict(self.value,version=2),dict(self.value,segments={}),dict(self.value,face=None)):
            with self.subTest(value=value):self.reject(value)

    def test_reject_lines(self):
        for lines in ([0,1],[1,7],[2,1],[True,1],[1,2,3],'1-2'):
            value=matches();value['segments'][0]['lines']=lines;self.reject(value)

    def test_reject_spot_duplicate_overlap(self):
        for spot in ('',[], 'x'*65):
            value=matches();value['segments'][0]['spot']=spot;self.reject(value)
        value=matches();value['segments']*=2;self.reject(value)
        value=matches();value['segments'].append(dict(copy.deepcopy(value['segments'][0]),spot='B02'));self.reject(value)

    def test_reject_shot_schema(self):
        for shots in ([],{},[{'end':90,'candidates':[]}],[{'end':True,'candidates':[]}],[{'end':100,'candidates':[],'start':0}], [{'end':25,'candidates':[]},{'end':100,'candidates':[]}]):
            value=matches();value['segments'][0]['shots']=shots;self.reject(value)

    def test_multiple_shots_valid_and_last_end(self):
        value=matches();row=value['segments'][0];row['lines']=[1,3]
        row['shots']=[{'end':n,'candidates':[]} for n in (100,200,300)]
        self.assertEqual(matching.validate_matches(value,self.model,self.catalog),value)
        row['shots'][-1]['end']=299;self.reject(value)

    def test_reject_candidate_schema(self):
        for candidate in ('s',{'segment':'missing','reason':'对应'}, {'segment':'s','reason':'对应','extra':1}, {'segment':'s','reason':'对应','source_start':True}):
            value=matches();value['segments'][0]['shots'][0]['candidates']=[candidate];self.reject(value)
        value=matches();value['segments'][0]['shots'][0]['candidates']*=4;self.reject(value)

    def test_reject_reason_limits(self):
        for reason in ('', ' ', '英文'*31,'english',[]):
            value=matches();value['segments'][0]['shots'][0]['candidates'][0]['reason']=reason;self.reject(value)

    def test_reject_cross_reserved_boundary(self):
        self.model['reserved']=[[50,60]];self.reject(matches())

    def test_reject_face_schema_and_duplicates(self):
        for faces in ([{'lines':[0,1]}],[{'lines':[1,1],'reason':'x'}],[{'lines':[1,1]}]*2):
            value=matches();value['face']=faces;self.reject(value)

    def test_quality_unknown_kept_all_hard_values_filtered(self):
        for field,bad in (('focus','missed'),('subject','cut_off'),('people','host'),('people','person')):
            with self.subTest(field=field,bad=bad):
                self.catalog['s'][field]=bad;out=matching.enforce_matches(matches(),self.model,self.catalog)
                self.assertEqual(out['compliance']['filtered_candidates'],1);del self.catalog['s'][field]
        self.catalog['s']['focus']=None
        self.assertEqual(matching.enforce_matches(matches(),self.model,self.catalog)['compliance']['rate'],1)

    def test_blacklist_and_parent_segment(self):
        for key in ('s','parent'):
            self.catalog['s']['parent_segment']='parent'
            out=matching.enforce_matches(matches(),self.model,self.catalog,{'blacklist':[key]})
            self.assertEqual(out['compliance']['rate'],0);self.assertIn('黑名单',out['report'])

    def test_force_reserve_face_duration_and_insufficient(self):
        for profile,face,hi,label in (({'reserved':[{'start':0,'end':100}]},[],600,'保留位'),({},[{'lines':[1,1]}],600,'露脸句'),({'parameters':{'min_frames':101}},[],600,'下限'),({'parameters':{'max_frames':90}},[],600,'上限'),({},[],90,'不足')):
            with self.subTest(label=label):
                value=matches();value['face']=face;self.catalog['s']['usable_end']=hi
                out=matching.enforce_matches(value,self.model,self.catalog,profile)
                self.assertEqual(out['compliance']['filtered_candidates'],1);self.assertIn(label,out['report'])

    def test_count_all_raw_candidates_even_protected(self):
        self.model['reserved']=[[0,100]];self.catalog['s']['people']='host'
        out=matching.enforce_matches(matches(),self.model,self.catalog)
        self.assertEqual(out['compliance']['considered'],1);self.assertEqual(len(out['filtered'][0]['reasons']),2)

    def test_plan_delegates_preserves_audit_profile_hash(self):
        original=copy.deepcopy(self.value)
        out=matching.plan_matches(self.value,self.model,self.catalog)
        self.assertEqual(out['placements'][0]['status'],'placed');self.assertEqual(out['placements'][0]['end'],100)
        self.assertEqual(self.value,original);self.assertEqual(out['profile_hash'],digest({}))
        out=matching.plan_matches(self.value,self.model,self.catalog,{'blacklist':['s']})
        self.assertEqual(out['placements'][0]['status'],'skipped');self.assertEqual(out['compliance']['considered'],1)
