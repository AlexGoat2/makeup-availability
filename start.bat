@echo off
cd /d "%~dp0"
title Bijenkorf stock checker
".env\Scripts\python.exe" main.py
pause
