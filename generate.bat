@echo off
rem Lint the repository, then build, assemble and smoke-test the toolset for
rem this machine's platform. Result: out\frice-tools-<version>-<platform>.zip
rem
rem Set PYTHON to choose the interpreter (default: the py launcher, then python).
rem Python 3.12 or newer is required.

setlocal
cd /d "%~dp0"

if not defined PYTHON (
    where py >nul 2>nul && set "PYTHON=py -3"
)
if not defined PYTHON (
    where python >nul 2>nul && set "PYTHON=python"
)
if not defined PYTHON (
    echo generate.bat: Python 3.12 or newer is required 1>&2
    exit /b 1
)

for %%s in (lint build assemble smoke) do (
    echo == %%s
    %PYTHON% scripts\tools.py %%s || exit /b 1
)