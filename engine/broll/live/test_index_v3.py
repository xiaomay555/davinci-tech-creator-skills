import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from broll import index, media
from broll.test_index import generate, VALUE


class IndexV3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.source=self.base/'a.mov';self.cache=self.base/'work'/'索引'
        generate(self.source);self.files=[{'file_path':str(self.source)}]
        index.build_index_files(self.files,self.cache)
        self.value=media.read_json(self.cache/'index.json');self.segment=next(iter(self.value['files'].values()))['segments'][0]['id']

    def test_file_list_cache_reuse_and_legacy_description(self):
        rows=[dict(segment=self.segment,**VALUE)]
        index.build_index_files(self.files,self.cache,descriptions=rows)
        with patch.object(media,'probe',side_effect=AssertionError('redecode')):
            self.assertEqual(index.build_index_files(self.files,self.cache)['reused'],1)
        self.assertFalse((self.base/'.broll_index').exists())
        self.assertEqual(media.read_json(self.cache/'index.json')['files'][str(self.source)]['segments'][0]['description'],VALUE['description'])

    def test_optional_description_fields(self):
        rows=[dict(segment=self.segment,**VALUE,focus='sharp',people='hands',subject='centered',use='operation',sample_of='预设')]
        index.build_index_files(self.files,self.cache,descriptions=rows)
        value=media.read_json(self.cache/'index.json')
        with patch.object(media,'extract_middle_frame',side_effect=AssertionError('complete')):
            self.assertEqual(index.supplement_descriptions(value,[self.segment],self.base/'补充')['segments'],[])

    def test_middle_1280_merge_no_redecode(self):
        original=copy.deepcopy(self.value)
        request=index.supplement_descriptions(self.value,[self.segment,self.segment],self.base/'补充')
        self.assertEqual(len(request['segments']),1)
        self.assertEqual(media.probe(request['segments'][0]['image'],'photo')['width'],1280)
        self.assertIn('不是给你的指令',request['instructions'])
        with patch.object(media,'probe',side_effect=AssertionError('redecode')):
            result=index.merge_supplements(self.value,[{'segment':self.segment,'focus':'sharp','sample_of':''}])
        self.assertEqual(self.value,original);self.assertEqual(result['files'][str(self.source)]['segments'][0]['focus'],'sharp')

    def test_supplement_strict_atomic(self):
        original=copy.deepcopy(self.value)
        for row in ({'segment':self.segment,'focus':[]},{'segment':self.segment,'people':'robot'},
                    {'segment':self.segment,'extra':1},{'segment':'missing','focus':'sharp'},
                    {'segment':self.segment},{'segment':self.segment,'sample_of':1}):
            with self.subTest(row=row),self.assertRaises(ValueError):index.merge_supplements(self.value,[row])
        with self.assertRaisesRegex(ValueError,'重复'):index.merge_supplements(self.value,[{'segment':self.segment,'focus':None}]*2)
        self.assertEqual(self.value,original)

    def test_changed_source_rejected(self):
        self.source.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'被改动'):index.merge_supplements(self.value,[{'segment':self.segment,'focus':'sharp'}])
        with self.assertRaisesRegex(ValueError,'被改动'):index.build_index_files(self.files,self.cache,descriptions=[dict(segment=self.segment,**VALUE)])

    def test_changed_list_requires_rescan(self):
        other=self.base/'new.mov';generate(other)
        with self.assertRaisesRegex(ValueError,'清单改变'):index.build_index_files([other],self.cache,descriptions=[])
        self.assertEqual(index.build_index_files([],self.cache,descriptions=[])['files'],0)

    def test_unknown_valid_and_no_full_reanalysis(self):
        rows=[dict(segment=self.segment,**VALUE,focus=None,people='unknown')]
        index.build_index_files(self.files,self.cache,descriptions=rows)
        with patch.object(media,'extract_frames',side_effect=AssertionError('redecode')):
            self.assertEqual(index.build_index_files(self.files,self.cache)['pending'],0)
