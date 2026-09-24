@echo off
rem  Make the distribution folder (see scripts\make_dist.py).
rem  ASCII only: cmd.exe reads .bat with the console code page.
pushd "%~dp0.." || exit /b 1
python scripts\make_dist.py %*
set RC=%ERRORLEVEL%
popd
pause
exit /b %RC%
