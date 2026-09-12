@echo off
rem ---------------------------------------------------------------------------
rem 启动 AI 绘画执行器(GUI)。纯 headless 作画不需要本文件, 直接:
rem   python -X utf8 bridge_stdio.py      外部 agent 通过 stdio 驱动作画
rem
rem 解释器查找顺序(不写死任何绝对路径, 换机器可直接使用):
rem   1. 环境变量 AI_DRAWING_PYTHON 指向的解释器
rem   2. 项目根 .python-path 文件中的路径(本机私有配置, 不随项目分发)
rem   3. 项目内虚拟环境 .venv\Scripts\python.exe
rem   4. PATH 上的 python
rem   5. PATH 上的 py -3 (Windows 启动器)
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "PYEXE="

if defined AI_DRAWING_PYTHON set "PYEXE=%AI_DRAWING_PYTHON%"

if not defined PYEXE (
    if exist "%~dp0.python-path" set /p PYEXE=<"%~dp0.python-path"
)

if not defined PYEXE (
    if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"
)

if not defined PYEXE (
    where python >nul 2>nul && set "PYEXE=python"
)

if not defined PYEXE (
    where py >nul 2>nul && set "PYEXE=py -3"
)

if not defined PYEXE (
    echo [ERROR] 未找到可用的 Python 解释器。
    echo   请安装 Python 3.9+ 并加入 PATH,
    echo   或设置环境变量 AI_DRAWING_PYTHON,
    echo   或把解释器路径写入项目根 .python-path 文件。
    pause
    exit /b 1
)

echo [run] %PYEXE% -X utf8 -m app.main
%PYEXE% -X utf8 -m app.main

if errorlevel 1 (
    echo.
    echo [ERROR] 启动失败。若提示缺少模块, 请先安装依赖:
    echo    %PYEXE% -m pip install -r requirements.txt
    pause
)
