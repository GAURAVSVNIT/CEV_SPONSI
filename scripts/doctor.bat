@echo off
rem Check every secret and integration live. Double-click friendly:
rem the window stays open until you press a key.
cd /d %~dp0..
python run.py doctor
pause
