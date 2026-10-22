"""Prepare a pinned, isolated RVM runtime without touching projects or media."""
from pathlib import Path
import subprocess
import sys
import urllib.request
import hashlib
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from engine.fx.matte import environment_root, environment_check, WEIGHTS_SHA256, CODE_COMMIT


def main():
    root=environment_root();root.mkdir(parents=True,exist_ok=True)
    python=root/'venv/bin/python'
    if not python.is_file():subprocess.run(['python3.12','-m','venv',str(root/'venv')],check=True)
    subprocess.run([str(python),'-m','pip','install',*sys.argv[1:],
                    'torch==2.14.1','torchvision==0.29.1','numpy','pillow'],check=True)
    repo=root/'RobustVideoMatting'
    if not repo.exists():
        subprocess.run(['git','clone','https://github.com/PeterL1n/RobustVideoMatting',str(repo)],check=True)
        subprocess.run(['git','-C',str(repo),'checkout',CODE_COMMIT],check=True)
    else:
        commit=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
        if not commit.startswith(CODE_COMMIT):raise RuntimeError('已有RVM目录版本不同，保留不改')
    weights=root/'weights/rvm_mobilenetv3.pth';weights.parent.mkdir(exist_ok=True)
    if not weights.is_file():
        tmp=weights.with_suffix('.download')
        urllib.request.urlretrieve('https://github.com/PeterL1n/RobustVideoMatting/releases/download/v1.0.0/rvm_mobilenetv3.pth',tmp)
        if hashlib.sha256(tmp.read_bytes()).hexdigest()!=WEIGHTS_SHA256:raise RuntimeError('模型SHA-256不符')
        tmp.replace(weights)
    ready,reason=environment_check(root)
    if not ready:raise RuntimeError(reason)
    print('RVM独立环境与模型校验通过')


if __name__=='__main__':main()
