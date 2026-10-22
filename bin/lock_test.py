#!/usr/bin/env python3
"""Check whether file locks hold across two Macs on the same NAS share.

On Mac A: python3 bin/lock_test.py hold <NAS 上的文件夹>   (holds the lock for 90 s)
On Mac B, within those 90 s: python3 bin/lock_test.py try <同一个文件夹>
"""
import fcntl
import sys
import time
from pathlib import Path

action, folder = sys.argv[1], Path(sys.argv[2])
path = folder / '.cross_mac_lock_test'
with path.open('a+') as handle:
    if action == 'hold':
        fcntl.flock(handle, fcntl.LOCK_EX)
        print('已在本机锁住 90 秒；请在另一台 Mac 运行 try', flush=True)
        time.sleep(90)
        print('结束')
    else:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            print('文件锁在两台 Mac 之间【无效】：同一个项目只能在一台 Mac 上剪')
            fcntl.flock(handle, fcntl.LOCK_UN)
        except (BlockingIOError, PermissionError):  # 另一台 Mac 持有时 NAS 报 Permission denied
            print('文件锁在两台 Mac 之间【有效】')
if action == 'hold':
    path.unlink(missing_ok=True)
