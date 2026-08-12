@echo off
rem  --- keep this file in CP932 (Shift-JIS) with CRLF line endings ---
rem  Keep everything above the chcp line ASCII; see start.bat for why.
chcp 932 >nul 2>&1
rem ===================================================================
rem  梱包資材総合ツール (Web版) 停止
rem
rem  このアプリだけを止めます。同じPCで動く他の Python アプリは
rem  影響を受けません(基盤仕様書 2.8)。
rem ===================================================================
setlocal

rem  共有フォルダに置かれていても動くよう pushd を使う(start.bat と同じ)
pushd "%~dp0" || (
    echo [エラー] このフォルダに移動できませんでした: %~dp0
    pause
    exit /b 1
)
title 梱包資材総合ツール - 停止

python --version >nul 2>&1
if errorlevel 1 (
    echo [エラー] Python が見つかりません。
    goto :failed
)

python process_manager.py --all %*
if errorlevel 1 (
    echo.
    echo 止められなかったものがあります。上のメッセージを確認してください。
    echo 実行中の処理があるときは、中断してよければ次を実行してください:
    echo     stop.bat --force
    echo.
    goto :failed
)
popd
endlocal
exit /b 0

:failed
pause
popd
endlocal
exit /b 1
