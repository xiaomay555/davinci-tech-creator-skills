#!/usr/bin/env python3
"""B-roll：实时读取、匹配、演练/写入、局部调整、学习与撤销。"""
import argparse
import json
import os
from pathlib import Path
import sys

if __name__=='__main__' and not sys.flags.utf8_mode:
    os.execvpe(sys.executable,[sys.executable,'-X','utf8',*sys.argv],dict(os.environ,PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1'))
sys.path.insert(0,str(Path(__file__).absolute().parents[1]))
from engine.broll.live import service
from engine.broll.live.writer import undo
from engine.broll.live.review_live import identify
from engine.broll.live.resolve_adapter import timecode_frame


class ChineseParser(argparse.ArgumentParser):
    def __init__(self,*args,**kwargs):
        kwargs['add_help']=False
        super().__init__(*args,**kwargs)
        self._positionals.title='命令与位置参数';self._optionals.title='选项'
        self.add_argument('-h','--help',action='help',help='显示帮助并退出')
    def format_help(self):
        return super().format_help().replace('usage:','用法：')
    def error(self, message):
        raise ValueError('参数不正确：'+message.replace('required','必填').replace('unrecognized arguments','未识别参数').replace('not allowed with argument','不能同时使用'))


def parser():
    p=ChineseParser(description=__doc__)
    subs=p.add_subparsers(dest='command',required=True,parser_class=ChineseParser)
    for name in ('status','plan','apply','adjust','undo','learn','profile','at','clean'):
        cmd=subs.add_parser(name)
        cmd.add_argument('--config',default=str(Path.home()/'.config/ai-talking-head-edit/broll.json'))
        cmd.add_argument('--timeline');cmd.add_argument('--project-dir');cmd.add_argument('--creator')
        if name=='plan':
            cmd.add_argument('--folder',action='append',default=[]);cmd.add_argument('--descriptions');cmd.add_argument('--supplements')
            cmd.add_argument('--more',action='store_true',help='加入其他项目/素材库的第二层候选')
        if name=='apply':cmd.add_argument('--matches',required=True)
        if name in ('apply','adjust','undo'):cmd.add_argument('--apply',action='store_true')
        if name=='adjust':
            cmd.add_argument('--spot',required=True,action='append')
            group=cmd.add_mutually_exclusive_group(required=True)
            group.add_argument('--use',type=int,choices=(1,2,3));group.add_argument('--segment')
            group.add_argument('--remove',action='store_true');group.add_argument('--reserve',action='store_true')
            group.add_argument('--face',choices=('on','off'));group.add_argument('--audio',choices=('on','off'))
        if name=='undo':cmd.add_argument('--run')
        if name=='learn':
            cmd.add_argument('--approve',action='store_true');cmd.add_argument('--verdict');cmd.add_argument('--note')
            group=cmd.add_mutually_exclusive_group();group.add_argument('--forget',choices=('last',));group.add_argument('--rule')
        if name=='at':
            cmd.add_argument('timecode',nargs='?');cmd.add_argument('--playhead',action='store_true')
        if name=='clean':cmd.add_argument('--yes',action='store_true')
    return p


def main(argv=None, preflight=service.preflight, session_type=service.Session):
    try:
        args=parser().parse_args(argv)
        if args.command=='at' and bool(args.timecode)==bool(args.playhead):raise ValueError('at请选择一个时间码或 --playhead')
        # Local defaults stay outside the checkout; a config file may point to shared storage.
        data_root=Path(os.environ.get('AI_EDIT_DATA_DIR',str(Path.home()/'.local/share/ai-talking-head-edit'))).expanduser()
        cfg={'nas_mount':str(data_root),'ledger_index':str(data_root/'broll/台账索引.json'),
             'learning_root':str(data_root/'broll'),'creator':None,'allow_postgres_apply':False}
        cfg.update(service.read_json(args.config,{}))
        cfg['selected_timeline']=args.timeline
        io,env=preflight(cfg,args.timeline)
        active=io.p.GetCurrentTimeline()
        tc=active.GetCurrentTimecode() if args.command=='at' and args.playhead else getattr(args,'timecode',None)
        # Review line mapping requires a registered main-line ledger. Use explicit
        # --project-dir plus --timeline main when current line is a viewing copy.
        project_dir=args.project_dir
        if args.command=='at':io,project_dir=service.main_for_review(io,cfg,project_dir)
        session=session_type(io,cfg,project_dir,args.creator,
                             readonly=args.command in ('status','profile','at'),
                             learn_on_open=args.command in ('plan','apply','adjust','learn'))
        command=args.command
        if command=='status':out={'工程':io.name,'时间线':io.main_name,'数据库':env['数据库'],'工作目录':str(session.work),'位置数':len(session.ledger['spots'])}
        elif command=='plan':out=session.plan(args.folder,args.descriptions,args.supplements,more=args.more)
        elif command=='apply':out=session.apply(service.read_json(args.matches),args.apply)
        elif command=='adjust':out=session.adjust(args.spot,use=args.use,segment=args.segment,remove=args.remove,reserve=args.reserve,face=args.face,audio=args.audio,apply=args.apply)
        elif command=='undo':
            out=undo(io,session.ledger,session.path,run=args.run,apply=args.apply,config=cfg)
            if out.get('applied'):session.ledger=out.pop('ledger');session.save()
        elif command=='learn':out=session.learning_command(args.approve,args.verdict,args.note,args.forget,args.rule)
        elif command=='profile':out={'学习功能':'试用','偏好':session.profile}
        elif command=='at':out={'对应位置':identify(session.ledger,active.GetName(),tc,active.GetSetting('timelineFrameRate')),'说明':'不唯一时请从最多3个编号中选择；修改请回原主线'}
        else:out=session.clean(args.yes)
        print(session.summary)
        for name in session.pending[:3]:print('上一条《'+name+'》下次打开时会自动学')
        print(json.dumps(out,ensure_ascii=False,indent=2))
        return 0
    except (OSError,ValueError,RuntimeError,KeyError,TypeError,ImportError,subprocess_timeout()) as exc:
        print('出错了：'+str(exc)+'\n下一步：按上述原因处理后重试；不要重复未核验的写入。',file=sys.stderr)
        return 2


def subprocess_timeout():
    import subprocess
    return subprocess.TimeoutExpired


if __name__=='__main__':sys.exit(main())
