@echo off
rem Check every secret and integration live. Double-click friendly:
rem the window stays open until you press a key.
cd /d %~dp0..
rem Prefer the project venv: a bare `python` can be the Microsoft Store alias.
set PY=python
if exist .venv\Scripts\python.exe set PY=.venv\Scripts\python.exe
%PY% run.py doctor
pause
