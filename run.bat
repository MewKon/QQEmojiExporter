@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================
echo   QQ 表情包提取器（桌面版）
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 python，请先安装 Python 3.9 及以上版本
  echo        安装时请勾选 "Add Python to PATH"
  pause
  exit /b 1
)

python -c "import PIL, customtkinter" >nul 2>nul
if errorlevel 1 (
  echo [提示] 缺少依赖 Pillow / CustomTkinter，正在安装 ...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [错误] 依赖安装失败，请手动执行: python -m pip install -r requirements.txt
    pause
    exit /b 1
  )
)

python -c "import tkinter" >nul 2>nul
if errorlevel 1 (
  echo [错误] 当前 Python 未包含 tkinter，无法显示界面。
  echo        可改用命令行：python app.py --scan
  pause
  exit /b 1
)

echo 正在启动桌面程序 ...
python app.py %*
if errorlevel 1 pause
