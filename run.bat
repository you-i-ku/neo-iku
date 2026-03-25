@echo off
chcp 65001 >nul 2>&1

:: 仮想環境チェック
if not exist ".venv\Scripts\activate.bat" (
    echo [エラー] 仮想環境が見つかりません。先に install.bat を実行してください。
    pause
    exit /b 1
)

:: アクティベート
call .venv\Scripts\activate.bat

:: LM Studioの確認
echo neo-iku を起動します。
echo （LM Studio が localhost:1234 で起動していることを確認してください）
echo.

:: 起動（cmd /cでサブシェル実行 → Ctrl+Cがバッチ終了確認をスキップ）
cd AI
cmd /c python run.py
