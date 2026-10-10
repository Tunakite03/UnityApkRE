@echo off
rem Run the uare CLI straight from a checkout or an installed Claude Code plugin (Windows).
rem cmd re-parses arguments: wrap regex patterns in double quotes, e.g. "(?i)shop|buy".
setlocal
set "ROOT=%~dp0.."
if defined UARE_PYTHON (set "PY=%UARE_PYTHON%") else (set "PY=py -3")
if defined PYTHONPATH (set "PYTHONPATH=%ROOT%\src;%PYTHONPATH%") else (set "PYTHONPATH=%ROOT%\src")
set "PYTHONDONTWRITEBYTECODE=1"
rem The current directory is removed from sys.path so local files cannot shadow uare or the stdlib.
%PY% -c "import sys; sys.path[:] = [p for p in sys.path if p not in ('', '.')]; import runpy; runpy.run_module('uare', run_name='__main__', alter_sys=True)" %*
exit /b %ERRORLEVEL%
