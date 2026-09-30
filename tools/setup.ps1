# 録音文字起こしツール セットアップ（Windows）
#   1. Python を確認（無ければ winget で Python 3.12 を入れるか案内）
#   2. .venv を作り requirements.txt を入れる
#   3. モデル（既定 large-v3-turbo）を models\ に取得
# setup.bat から実行する。やり直しても安全（済んでいる手順は飛ばす）。
param([string]$Model = 'large-v3-turbo')

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Find-Python {
    foreach ($cmd in @(@('py', '-3.12'), @('py', '-3'), @('python'))) {
        $exe = Get-Command $cmd[0] -ErrorAction SilentlyContinue
        if (-not $exe) { continue }
        $args_ = @($cmd | Select-Object -Skip 1)
        try {
            $ver = & $cmd[0] @args_ -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        } catch { continue }
        if ($LASTEXITCODE -eq 0 -and $ver -and [version]$ver -ge [version]'3.10') {
            return , $cmd
        }
    }
    return $null
}

Write-Host '==== 録音文字起こしツール セットアップ ====' -ForegroundColor Cyan

# 1. Python
$py = Find-Python
if (-not $py) {
    Write-Host 'Python 3.10 以降が見つかりません。' -ForegroundColor Yellow
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        $ans = Read-Host 'winget で Python 3.12 をインストールしますか？ (Y/N)'
        if ($ans -match '^[Yy]') {
            winget install -e --id Python.Python.3.12 --scope user --accept-source-agreements --accept-package-agreements
            $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
            $py = Find-Python
        }
    }
    if (-not $py) {
        Write-Host 'https://www.python.org/downloads/ から Python 3.12 を入れて、setup.bat をもう一度実行してください。' -ForegroundColor Red
        exit 1
    }
}
Write-Host "[1/3] Python: $($py -join ' ')"

# 2. 仮想環境とパッケージ
$venvPy = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Write-Host '[2/3] 仮想環境 .venv を作成中...'
    $pyArgs = @($py | Select-Object -Skip 1)
    & $py[0] @pyArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '仮想環境の作成に失敗しました。' }
}
Write-Host '[2/3] パッケージをインストール中（数分かかります）...'
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host 'パッケージのインストールに失敗しました。プロキシ環境の場合は HTTPS_PROXY を設定して再実行してください。' -ForegroundColor Red
    exit 1
}

# 3. モデル
Write-Host "[3/3] モデル $Model を確認中..."
& $venvPy tools\download_model.py $Model
if ($LASTEXITCODE -ne 0) { Write-Host 'モデルの取得に失敗しました。' -ForegroundColor Red; exit 1 }

Write-Host ''
Write-Host '==== セットアップ完了 ====' -ForegroundColor Green
Write-Host '  run_fw.bat       … 音声ファイルを文字起こし'
Write-Host '  run_meeting.bat  … Web会議を録音して文字起こし'
