@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动 BOQ Intelligence 工程量清单智能价格匹配平台...
start "" cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:8000/"
python run.py
pause
