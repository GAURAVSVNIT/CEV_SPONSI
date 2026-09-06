@echo off
rem The daily run. First argument = --limit (default 5). Nothing is sent;
rem drafts land in Telegram and wait for a tap.
cd /d %~dp0..
set LIMIT=%1
if "%LIMIT%"=="" set LIMIT=5
python run.py run --limit %LIMIT%
