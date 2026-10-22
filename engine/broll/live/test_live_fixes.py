import unittest
from engine.broll.live import resolve_adapter


class Media:
    def __init__(self, props): self.props = props
    def GetClipProperty(self): return self.props
    def GetUniqueId(self): return self.props['File Path']


class Folder:
    def __init__(self, clips): self.clips = clips
    def GetName(self): return 'Master'
    def GetClipList(self): return self.clips
    def GetSubFolderList(self): return []


class Project:
    def __init__(self, clips): self.folder = Folder(clips)
    def GetMediaPool(self): return self
    def GetRootFolder(self): return self.folder


class LocalizedMediaTypes(unittest.TestCase):
    def test_chinese_resolve_type_names_from_live_readback(self):
        # 2026-10-13 实机：中文界面 Type 为 '视频 + 音频'（带空格）、'静帧'。
        clips = [Media({'File Path': '/m/C3820.MP4', 'Type': '视频 + 音频', 'Frames': '135', 'FPS': 29.97}),
                 Media({'File Path': '/m/a.HEIC', 'Type': '静帧', 'Frames': '1', 'FPS': 29.97}),
                 Media({'File Path': '/m/b.mov', 'Type': 'Video + Audio', 'Frames': '30', 'FPS': 29.97}),
                 Media({'File Path': '/m/c.mp3', 'Type': '音频', 'Frames': '', 'Duration': '00:00:10:13', 'FPS': 29.97})]
        kinds = [r['kind'] for r in resolve_adapter.media_pool(Project(clips))]
        self.assertEqual(kinds, ['video', 'photo', 'video', 'audio'])


if __name__ == '__main__':
    unittest.main()
