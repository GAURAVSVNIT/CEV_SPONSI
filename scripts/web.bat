@echo off
rem The browser UI on http://127.0.0.1:8765 -- the approval queue, the prospect
rem list and the agent. Leave this window open while you use it.
cd /d %~dp0..
rem Prefer the project venv: a bare `python` can be the Microsoft Store alias.
set PY=python
if exist .venv\Scripts\python.exe set PY=.venv\Scripts\python.exe
%PY% run.py web --open %*
