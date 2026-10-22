#!/bin/bash
# Run every offline regression; exit non-zero if anything fails or is skipped. Never calls paid APIs or Resolve.
set -u
cd "$(dirname "$0")/.." || exit 1
export PYTHONDONTWRITEBYTECODE=1
unset AI_EDIT_RUN_DIR  # a leftover project setting would make tests read a real project
status=0
check_python() {  # $1 = group name, $2 = directory
  out=$(cd "$2" && python3 -m unittest discover -p 'test_*.py' 2>&1)
  if ! echo "$out" | grep -q '^OK'; then echo "FAIL $1"; echo "$out" | grep -E '^(FAIL|ERROR):' | head -10; status=1
  elif echo "$out" | grep -q 'skipped='; then echo "FAIL $1：有测试被跳过（缺 ffmpeg 或 node？）"; status=1
  elif ! echo "$out" | grep -qE '^Ran [1-9]'; then echo "FAIL $1：一个测试都没找到"; status=1; fi
}
check_python 'bin 测试' bin
check_python 'engine 测试' engine/src
check_python 'broll 测试' engine/broll
check_python 'fx 测试' engine/fx
for f in engine/src/test_*.cjs; do
  out=$(node "$f" 2>&1)
  if [ $? -ne 0 ] || ! echo "$out" | grep -q '__ALL_TESTS_DONE__'; then echo "FAIL $f（失败或中途卡住）"; status=1; fi
done
[ $status -eq 0 ] && echo '全部测试通过' || echo '有测试未通过，不要存档'
exit $status
