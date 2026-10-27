@echo off
rem Long-poll Telegram so the card buttons and /commands react instantly.
rem Leave this window open, or register it with Task Scheduler "at log on".
cd /d %~dp0..
rem Prefer the project venv: a bare `python` can be the Microsoft Store alias.
set PY=python
if exist .venv\Scripts\python.exe set PY=.venv\Scripts\python.exe
%PY% run.py serve
