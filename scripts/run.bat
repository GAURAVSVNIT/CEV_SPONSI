@echo off
rem The daily run. First argument = --limit (default 5). Nothing is sent;
rem drafts land in Telegram and wait for a tap.
cd /d %~dp0..
rem Prefer the project venv: a bare `python` can be the Microsoft Store alias.
set PY=python
if exist .venv\Scripts\python.exe set PY=.venv\Scripts\python.exe
set LIMIT=%1
if "%LIMIT%"=="" set LIMIT=5
%PY% run.py run --limit %LIMIT%
