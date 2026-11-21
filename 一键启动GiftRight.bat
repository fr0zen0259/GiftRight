@echo off
chcp 65001 >nul
title GiftRight 智能心意工坊启动器
echo ========================================================
echo         GiftRight · 送对了，比送贵了重要
echo ========================================================
echo.
echo 正在启动后端服务 (端口 8787)...
start "GiftRight-Backend (8787)" cmd /k "cd /d "%~dp0" && python app.py"

echo 正在打开前端页面...
timeout /t 2 >nul
start http://127.0.0.1:8787

echo.
echo ========================================================
echo  服务启动完成！
echo  - 访问地址：http://127.0.0.1:8787
echo ========================================================
echo.
pause
