#!/bin/sh
# ============================================================
# 一键 CI（M4）—— POSIX sh 参考版。
#
# !! 注意：本项目的 Windows 开发机上**子进程跑 bash/sh 会崩**（历史环境项，见
# 工作指导 §10.5），本脚本在本机沙箱不准执行验证；Windows 一律用 scripts/ci.cmd。
# 本文件仅供真实 POSIX shell 环境（Linux/CI 容器）参考，逻辑与 ci.cmd 逐条对应。
# ============================================================
set -u
cd "$(dirname "$0")/.." || exit 2

# Windows venv 布局是 .venv/Scripts/python.exe，POSIX 是 .venv/bin/python
if [ -x ".venv/Scripts/python.exe" ]; then
    PY=.venv/Scripts/python.exe
elif [ -x ".venv/bin/python" ]; then
    PY=.venv/bin/python
else
    echo "[CI] 找不到虚拟环境解释器（.venv）—— 请先按 README 快速开始建 .venv"
    exit 2
fi

echo "[CI 1/3] compileall src（静态可编译）..."
"$PY" -X utf8 -m compileall -q src || { echo "[CI 1/3] FAIL - compileall src"; exit 1; }
echo "[CI 1/3] OK - compileall src"

echo "[CI 2/3] unittest discover（离线测试套件全量）..."
"$PY" -X utf8 -m unittest discover -s tests -p "test_*.py" || {
    echo "[CI 2/3] FAIL - unittest discover"; exit 1; }
echo "[CI 2/3] OK - unittest discover"

echo "[CI 3/3] mock_run.py（模拟上游闸门，28 条）..."
"$PY" -X utf8 .tmp/verify-2026-10-22/mock_run.py || {
    echo "[CI 3/3] FAIL - mock_run.py 闸门"; exit 1; }
echo "[CI 3/3] OK - mock_run.py 闸门"

echo "[CI] 结果：全绿（compileall + unittest + mock 闸门）"
exit 0
