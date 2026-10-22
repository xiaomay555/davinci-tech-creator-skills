#!/usr/bin/env python3
"""花字音效：读查、计划、演练/执行、分解核对与定稿入库。"""
import argparse
import json
import os
from pathlib import Path
import sys

# Resolve's Python bridge changes the locale; make Chinese paths reliable on every Mac.
if not sys.flags.utf8_mode:
    os.execvpe(sys.executable,[sys.executable,'-X','utf8',*sys.argv],dict(os.environ,PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1'))
os.environ['PYTHONUTF8']='1'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from engine.fx.connect import connect
from engine.fx.snapshot import snapshot, markers, still, write_json
from engine.fx.check import check
from engine.fx.plan import validation_report
from engine.fx.learn import learn, DEFAULT_CASE_ROOT
from engine.fx.executor import run, decompose_check
from engine.fx.direct import NeedsTarget
from engine.fx.ledger import load
from engine.fx.review import review
from engine.fx.paths import default_work_dir


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    for name in ('snapshot','markers','still','learn'):
        p=commands.add_parser(name);p.add_argument('--timeline',required=True)
        if name=='snapshot':p.add_argument('--out',required=True)
        elif name=='still':p.add_argument('--frames',required=True);p.add_argument('--out-dir',required=True)
        elif name=='learn':
            p.add_argument('--case-root',default=DEFAULT_CASE_ROOT);p.add_argument('--name');p.add_argument('--work-dir')
    for name in ('build','place','decompose-check','review','cleanup'):
        p=commands.add_parser(name)
        p.add_argument('--project',required=True);p.add_argument('--timeline',required=True)
        p.add_argument('--work-dir',required=name not in ('build','place'))
        if name in ('build','place'):
            p.add_argument('--plan',required=True);p.add_argument('--apply',action='store_true')
            p.add_argument('--mode',choices=('auto','small','batch'),default='auto',help='默认识别已有少量小修；small不重开、不自动审片；整批新做用batch')
            p.add_argument('--project-dir',help='新工作目录的项目根目录；不提供则从计划位置向上识别')
            p.add_argument('--batch',help='新工作目录的批次名，默认计划文件名')
            p.add_argument('--allow-unsafe',action='store_true',help='显式跳过安全距离违规并在输出留记录')
            p.add_argument('--resume',action='store_true',help='设置视频源目标轨后，从操作记录断点继续，不重放已有花字')
        if name=='build':
            p.add_argument('--defer-place',action='store_true',help='在主线副本直接放花字；审完后用 place 在主线重放')
            review_options=p.add_mutually_exclusive_group()
            review_options.add_argument('--review',action='store_true',help='整批制作后额外创建审片时间线，仅用户要求时使用')
            review_options.add_argument('--no-review',action='store_true',help='兼容旧命令：不创建审片时间线')
        if name=='cleanup':p.add_argument('--apply',action='store_true')
        if name=='review':
            p.add_argument('--pad',type=int,default=30);p.add_argument('--merge-gap',type=int,default=30)
    p=commands.add_parser('check');p.add_argument('snapshot');p.add_argument('--plan')
    p=commands.add_parser('plan');p.add_argument('--validate',required=True)
    p.add_argument('--snapshot',help='可选时间线快照，读取分辨率与字幕区；未提供时用计划分辨率和默认字幕区')
    p.add_argument('--allow-unsafe',action='store_true')
    p.add_argument('--prepare-out',help='用快照与计划transcripts映射首字，合并连续清单，输出供核对的新计划')
    args=parser.parse_args()
    read=lambda p:json.loads(Path(p).read_text(encoding='utf-8'))
    try:
        if args.command=='plan':
            plan=read(args.validate);tl=read(args.snapshot)['timeline'] if args.snapshot else None
            if args.prepare_out:
                if tl is None:raise ValueError('--prepare-out 需要 --snapshot')
                from engine.fx.timing import prepare_timing, continuous_states
                plan=continuous_states(prepare_timing(plan,tl))
                from engine.fx.layout import prepare_layout
                plan=prepare_layout(plan,tl)
                if Path(args.prepare_out).exists():raise FileExistsError('准备计划输出已存在，不覆盖')
                write_json(args.prepare_out,plan)
            result=validation_report(plan,tl,args.allow_unsafe)
            errors=result['errors']
        elif args.command=='check':
            result=check(read(args.snapshot),read(args.plan) if args.plan else None);errors=result['findings']
        else:
            r,p,t=connect(args.timeline);errors=[]
            if args.command in ('build','place'):
                args.work_dir=args.work_dir or str(default_work_dir(args.plan,args.project_dir,args.batch))
                result=run(r,p,t,args.project,args.plan,args.work_dir,args.apply,args.command,getattr(args,'defer_place',False),args.allow_unsafe,args.resume,mode=args.mode)
                if result.get('allowed') is False:errors=[result['message']]
                if args.command=='build' and result.get('applied') and result.get('mode')!='small' and args.review and not args.no_review:
                    # build saves and reopens; reacquire all SDK handles, also for the viewing copy.
                    r,p,t=connect(result.get('built_timeline',args.timeline))
                    try:result['review']=review(r,p,t,args.project,args.work_dir)
                    except (OSError,ValueError,RuntimeError,KeyError,TypeError) as exc:
                        raise RuntimeError('花字已成功保存，但自动审片失败；请检查现场后单独运行 review，不要重复 build：'+str(exc)) from exc
            elif args.command=='cleanup':
                from engine.fx.cleanup import cleanup
                result=cleanup(r,p,t,args.project,args.work_dir,args.apply)
            elif args.command=='review':
                result=review(r,p,t,args.project,args.work_dir,args.pad,args.merge_gap)
            elif args.command=='decompose-check':
                result=decompose_check(r,p,t,args.project,args.work_dir)
                errors=result['errors']
            elif args.command=='snapshot':
                result=snapshot(r,p,t);write_json(args.out,result)
                result={'out':args.out,'project':result['project'],'timeline':result['timeline']['name'],'items':len(result['timeline']['items'])}
            elif args.command=='markers':
                rows=markers(snapshot(r,p,t));result={'count':len(rows),'markers':rows}
                for row in rows:print(f'{row["timecode"]} | {row.get("color","")} | {row.get("name","")} | {row.get("note","")}',file=sys.stderr)
            elif args.command=='still':result=still(r,p,t,[int(v) for v in args.frames.split(',')],args.out_dir)
            else:result=learn(r,p,t,args.case_root,args.name,load(args.work_dir) if args.work_dir else None)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 2 if errors else 0
    except NeedsTarget as exc:
        print(json.dumps({'needs_target':exc.track,'message':str(exc),'exit_code':3},ensure_ascii=False),file=sys.stderr)
        return 3
    except (OSError,ValueError,RuntimeError,KeyError,TypeError) as exc:
        print(json.dumps({'error':'操作停止：'+str(exc)},ensure_ascii=False),file=sys.stderr)
        return 2


if __name__=='__main__':sys.exit(main())
