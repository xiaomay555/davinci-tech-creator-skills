"""T8 reporting and protection reasons, entirely offline."""
from copy import deepcopy
from pathlib import Path
import unittest
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from engine.broll.live._fixtures import snapshot, item, with_ai
from engine.broll.live.timeline import build_model, record_to_source
from engine.broll.live.matching import plan_matches, validate_matches, enforce_matches
from engine.broll.live.report import placement_report
from engine.broll import index


def request(lines=(1,1),end=100):
    return {'version':3,'segments':[{'spot':'T8_B01','lines':list(lines),'shots':[
        {'end':end,'candidates':[{'segment':'s','reason':'对应口播','source_start':30}]}]}],'face':[]}


CAT={'s':dict(segment='s',file='/media/sample.mov',kind='video',usable_start=0,usable_end=600)}


class ReportTests(unittest.TestCase):
    def test_single_specific_reason_for_every_protection_category(self):
        for kind,label in [('deleted','她删'),('replaced_own','她换'),('disabled','她停用'),
                           ('trimmed','她剪掉'),('new_reserved','章节停用V1'),('用户点名保留','点名留空')]:
            with self.subTest(kind=kind):
                model=build_model(snapshot(),{'reserved':[{'start':0,'end':100,'reason':kind}]})
                out=plan_matches(request(),model,CAT)
                self.assertEqual(out['placements'][0]['reason'],f'保留位（{label}）')
        snap=snapshot();snap['items'].append(item('用户',0,100))
        self.assertEqual(plan_matches(request(),build_model(snap),CAT)['placements'][0]['reason'],'用户画面')
        value=request();value['face']=[{'lines':[1,1]}]
        self.assertEqual(plan_matches(value,build_model(snapshot()),CAT)['placements'][0]['reason'],'露脸')
        snap,book=with_ai();value=request()
        self.assertEqual(plan_matches(value,build_model(snap,book),CAT,ledger=book)['placements'][0]['reason'],'已有AI')
        snap=snapshot();snap['items'][0]['enabled']=False
        self.assertEqual(plan_matches(request(),build_model(snap),CAT)['placements'][0]['reason'],'保留位（章节停用V1）')

    def test_learned_delete_and_replace_status_disambiguate_old_reason(self):
        snap,book=with_ai()
        for status,label in [('deleted','她删'),('replaced_own','她换')]:
            book['spots']['0']['status']=status
            book['reserved']=[{'spot':'0','source_ranges':book['spots']['0']['semantic']['source_ranges'],'reason':'她删除/替换过的AI位置'}]
            p=plan_matches(request(),build_model(snap,book),CAT,ledger=book)
            self.assertEqual(p['placements'][0]['reason'],f'保留位（{label}）')

    def test_short_trim_reports_actual_and_minimum_frames(self):
        snap=snapshot();snap['items'].append(item('用户',0,32))
        out=plan_matches(request(),build_model(snap),CAT)
        self.assertEqual(out['placements'][0]['reason'],'裁后太短（68帧<最短75帧）')

    def test_report_contains_source_and_coverage_and_filter_rules(self):
        model=build_model(snapshot());catalog=deepcopy(CAT);catalog['s']['people']='host'
        plan=plan_matches(request(),model,catalog)
        out=placement_report(plan,model,catalog,request())
        row=out['skipped'][0]
        self.assertEqual(row['summary'],'00:00:00:00｜句子1｜sample.mov｜30帧（00:00:01:00）')
        self.assertEqual(out['compliance']['rate'],0)
        self.assertIn('people=host',out['report']);self.assertIn('覆盖率：0.0% → 0.0%',out['report'])

    def test_matching_errors_name_spot_segment_and_cause(self):
        for change in (lambda r:r['shots'][0]['candidates'][0].update(extra=True),
                       lambda r:r['shots'][0]['candidates'][0].update(source_start=-1),
                       lambda r:r['shots'][0]['candidates'][0].update(reason='english')):
            value=request();change(value['segments'][0])
            with self.assertRaisesRegex(ValueError,'spot T8_B01.*段 s'):
                validate_matches(value,build_model(snapshot()),CAT)
        value=request();value['segments'][0]['lines']=[0,1]
        with self.assertRaisesRegex(ValueError,'spot T8_B01.*行范围'):
            validate_matches(value,build_model(snapshot()),CAT)

    def test_supplement_error_identifies_segment(self):
        with self.assertRaisesRegex(ValueError,'补充段 bad-id.*字段不符'):
            index.merge_supplements({'files':{}},[{'segment':'bad-id','description':'错误字段'}])

    def test_people_sample_exemption_in_hard_enforcement(self):
        for people,use,rate in [('person','sample',1),('host','sample',0),('person','operation',0),('person','scene',0)]:
            cat={'s':dict(CAT['s'],people=people,use=use)}
            self.assertEqual(enforce_matches(request(),build_model(snapshot()),cat)['compliance']['rate'],rate)

    def test_docs_require_sdk_reopen_and_actual_rehearsal_inspection(self):
        refs=Path(__file__).resolve().parents[3]/'skills/b-roll/references'
        writing=(refs/'写入与核验.md').read_text()
        visual=(refs/'看图与匹配.md').read_text()
        for word in ('SDK','b-roll/fx','重开','读回'):self.assertIn(word,writing)
        for word in ('写入前必须打开','三帧不能保证','不能凭画面有菜单就认定是电影预设','演练核图'):self.assertIn(word,visual)
