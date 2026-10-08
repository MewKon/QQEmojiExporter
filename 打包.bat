@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================
echo   打包 QQ 表情包提取器（目录版）
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 python
  pause
  exit /b 1
)

python -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
  echo [提示] 未安装 PyInstaller，正在安装 ...
  python -m pip install pyinstaller
  if errorlevel 1 (
    echo [错误] 安装失败，请手动执行： python -m pip install pyinstaller
    pause
    exit /b 1
  )
)

echo 开始打包 ...
python tools\build.py --onedir

echo.
echo 产物： QQEmojiExporter\QQEmojiExporter.exe
echo 缓存： QQEmojiExporter\.cache（索引、缩略图、导出的 zip 都在这里）
echo.
pause
