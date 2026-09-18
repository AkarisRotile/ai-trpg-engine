@echo off
REM 双击这个就能打包。真正的逻辑在 build.ps1 里。
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*
if errorlevel 1 (
  echo.
  echo 打包失败，上面的信息可以拿给 AI 看。
)
pause
