@echo off
rem  --- keep this file in CP932 (Shift-JIS) with CRLF line endings ---
rem  Everything above the chcp line must stay ASCII. cmd.exe parses a .bat
rem  with the *console* code page, so the page has to be set before the
rem  first non-ASCII byte - including the bytes in these comments.
rem  In CP932 a trail byte can be 0x5C or 0x40, so U+8868 / U+30BD / U+30A1
rem  turn into a literal backslash or at-sign under a different code page.
rem  tests/test_launch_files.py enforces the encoding and this ordering.
chcp 932 >nul 2>&1
rem ===================================================================
rem  梱包資材総合ツール (Web版) 診断起動
rem
rem  普段は Start.vbs を使ってください。こちらは「起動しないとき」に
rem  原因を見るためのもので、コンソールを開いたまま経過を表示します。
rem  (基盤仕様書 2.1「通常起動と原因調査用起動を分ける」)
rem
rem  引数はそのまま start_app.py へ渡します。よく使うのは次の3つ:
rem      start.bat                    この端末に許されたモードで開く
rem      start.bat --mode material    モードを指定する(2つ並べて開くとき)
rem      start.bat --check            環境の確認だけして終わる
rem ===================================================================
setlocal

rem  共有フォルダ(\\サーバ\...)は現在地にできないので pushd を使う。
rem  pushd は一時的にドライブ文字を割り当てるため、共有に置いても動く
pushd "%~dp0" || (
    echo [エラー] このフォルダに移動できませんでした: %~dp0
    pause
    exit /b 1
)
title 梱包資材総合ツール - 診断起動 (この窓は閉じないでください)

python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo [エラー] Python が見つかりません。
    echo.
    echo   https://www.python.org/downloads/ からインストールしてください。
    echo   インストーラの最初の画面で
    echo   「Add python.exe to PATH」に必ずチェックを入れてください。
    echo.
    goto :failed
)

echo === 実行環境の確認 ===
python start_app.py --check
if errorlevel 1 (
    echo.
    echo 上のメッセージを確認してください。
    goto :failed
)

echo.
echo === 起動 ===
echo この窓を閉じるとアプリも終了します。
echo 終了するときは stop.bat を実行するか、画面の「終了」を押してください。
echo.
rem  引数はそのまま渡す。**モードの対応表をここに二重に持たない** ──
rem  片方だけ直すと、資材モードが黙って現場モードで起動する
python start_app.py %*
if errorlevel 1 (
    echo.
    echo [エラー] 起動に失敗しました。上のメッセージを確認してください。
    echo          ログ: %LOCALAPPDATA%\PackagingTool\logs の最新ファイル
    echo.
    goto :failed
)

echo.
echo 終了しました。この窓は閉じてかまいません。
pause
popd
endlocal
exit /b 0

:failed
pause
popd
endlocal
exit /b 1
