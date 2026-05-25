@echo off
setlocal
cd /d "%~dp0"
py -3.10 -m pytest
