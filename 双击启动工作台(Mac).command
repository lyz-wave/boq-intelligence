#!/bin/zsh
cd "$(dirname "$0")" || exit 1
echo "正在启动 BOQ Intelligence 工程量清单智能价格匹配平台..."
if lsof -i :8000 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "检测到 8000 端口服务已在运行，正在打开工作台..."
  open "http://127.0.0.1:8000/"
  exit 0
fi
(
  for i in {1..40}; do
    if curl -s http://127.0.0.1:8000/api/overview >/dev/null 2>&1; then
      open "http://127.0.0.1:8000/"
      break
    fi
    sleep 0.3
  done
) &
python3 run.py
