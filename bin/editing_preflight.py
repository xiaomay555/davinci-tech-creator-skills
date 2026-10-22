"""Check installed alignment dependencies without loading/downloading a model."""
import json
import os
import subprocess
from pathlib import Path


def alignment_runtime(python=None):
    default=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))/'tools/ai-edit-align/bin/python'
    path=Path(python or os.environ.get('AI_EDIT_ALIGN_PYTHON',str(default))).expanduser()
    result={'alignment_python':str(path),'alignment_dependencies_ready':False,'alignment_error':None}
    if not path.is_file() or not os.access(path,os.X_OK):
        result['alignment_error']='缺少本机对齐环境；用 --align-python 或 AI_EDIT_ALIGN_PYTHON 指定已有 qwen_asr Python'
        return result
    try:
        check=subprocess.run([str(path),'-c',
            'import qwen_asr, torch, transformers, soundfile, accelerate; '
            'import json, importlib.metadata as m; '
            'print(json.dumps({k:m.version(k) for k in ["qwen-asr","torch","transformers","soundfile","accelerate"]}))'],
            capture_output=True,text=True,timeout=30)
        if check.returncode:
            result['alignment_error']='对齐依赖无法导入，请检查该 Python 的 qwen_asr/torch/transformers/soundfile/accelerate'
        else:
            result['alignment_versions']=json.loads(check.stdout.strip().splitlines()[-1])
            result['alignment_dependencies_ready']=True
    except (OSError,subprocess.TimeoutExpired,ValueError,IndexError):
        result['alignment_error']='对齐依赖检查失败或超过 30 秒；没有下载/加载模型'
    return result
