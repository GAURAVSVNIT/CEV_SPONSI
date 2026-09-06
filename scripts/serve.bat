@echo off
rem Long-poll Telegram so the card buttons and /commands react instantly.
rem Leave this window open, or register it with Task Scheduler "at log on".
cd /d %~dp0..
python run.py serve
