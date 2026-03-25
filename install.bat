@echo off
chcp 65001 >nul 2>&1
echo ==========================================
echo   neo-iku インストーラー
echo ==========================================
echo.

:: Python検出
where python >nul 2>&1
if errorlevel 1 (
    echo [エラー] Pythonが見つかりません。
    echo https://www.python.org/downloads/ からインストールしてください。
    pause
    exit /b 1
)

:: Pythonバージョン表示
for /f "tokens=*" %%i in ('python --version 2^>^&1') do set PYVER=%%i
echo 検出: %PYVER%
echo.

:: 仮想環境作成
if not exist ".venv" (
    echo 仮想環境を作成中...
    python -m venv .venv
    if errorlevel 1 (
        echo [エラー] 仮想環境の作成に失敗しました。
        pause
        exit /b 1
    )
    echo 仮想環境を作成しました。
) else (
    echo 仮想環境は既に存在します。
)
echo.

:: 仮想環境をアクティベート
call .venv\Scripts\activate.bat

:: pip更新
echo pipを更新中...
python -m pip install --upgrade pip --quiet

:: 依存パッケージインストール
echo 依存パッケージをインストール中...
pip install -r AI\requirements.txt
if errorlevel 1 (
    echo [エラー] パッケージのインストールに失敗しました。
    pause
    exit /b 1
)

echo.
echo ==========================================
echo   インストール完了！
echo   run.bat をダブルクリックで起動できます。
echo ==========================================
pause
