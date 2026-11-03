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

rem  /nopause か 環境変数 STOP_NOPAUSE=1 で、最後の pause を飛ばす(業務ツール統合ランチャー 1.8.9 は
rem  STOP_NOPAUSE=1 を付けて呼ぶ)。/nopause は process_manager.py へは渡さない
set "NOPAUSE=%STOP_NOPAUSE%"
set "ARGS="
:args
if "%~1"=="" goto :argsdone
if /i "%~1"=="/nopause" (
    set "NOPAUSE=1"
) else (
    set ARGS=%ARGS% %1
)
shift
goto :args
:argsdone

rem  共有フォルダに置かれていても動くよう pushd を使う(start.bat と同じ)
pushd "%~dp0" || (
    echo [エラー] このフォルダに移動できませんでした: %~dp0
    if not "%NOPAUSE%"=="1" pause
    exit /b 1
)
title 梱包資材総合ツール - 停止

rem  戻り値は process_manager.py の値をそのまま返す(業務ツール統合ランチャーが読む)
rem      0 = 止めた・動いていない / 1 = 止められなかった
rem      3 = 止めなかった(処理中): 保存していない図・実行中の処理がある。--force で止める
rem  出力の1行目は理由(ランチャーが管理者の一覧と利用者への知らせに出す)
set "RC=1"
python --version >nul 2>&1
if errorlevel 1 (
    echo [エラー] Python が見つかりません。
    goto :failed
)

python "%~dp0process_manager.py" --all %ARGS%
set "RC=%errorlevel%"
if not "%RC%"=="0" (
    echo.
    echo 止めなかったもの・止められなかったものがあります。上のメッセージを確認してください。
    echo 保存していない図・実行中の処理があるときは、捨てて・中断してよければ次を実行してください:
    echo     stop.bat --force
    echo.
    goto :failed
)
popd
endlocal
exit /b 0

:failed
if not "%NOPAUSE%"=="1" pause
popd
endlocal & exit /b %RC%
