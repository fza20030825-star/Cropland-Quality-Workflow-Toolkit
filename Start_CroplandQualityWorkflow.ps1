$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot

$PrintOnly = $args -contains "--print-python"
$ResetCache = $args -contains "--reset-python-cache"
$AppName = "CroplandQualityWorkflowToolkit"
$CacheDir = if ($env:CQWT_CACHE_DIR) { $env:CQWT_CACHE_DIR } else { Join-Path $env:LOCALAPPDATA $AppName }
$CacheFile = Join-Path $CacheDir "arcgis_python_path.txt"

function Normalize-InputPath {
    param([string]$PathText)
    if ([string]::IsNullOrWhiteSpace($PathText)) {
        return ""
    }
    return [Environment]::ExpandEnvironmentVariables($PathText.Trim().Trim('"'))
}

function Test-ArcPyPython {
    param([string]$PythonPath)
    $candidate = Normalize-InputPath $PythonPath
    if (-not $candidate) {
        return $false
    }
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        return $false
    }
    & $candidate -c "import arcpy" *> $null
    return ($LASTEXITCODE -eq 0)
}

function New-PythonCandidate {
    param(
        [string]$PythonPath,
        [string]$Source
    )
    $candidate = Normalize-InputPath $PythonPath
    if (-not (Test-ArcPyPython $candidate)) {
        return $null
    }
    $resolved = (Resolve-Path -LiteralPath $candidate).Path
    return [pscustomobject]@{
        Path = $resolved
        Source = $Source
    }
}

function Save-PythonCache {
    param($Candidate)
    if (-not $Candidate) {
        return
    }
    New-Item -ItemType Directory -Force -Path $CacheDir *> $null
    Set-Content -LiteralPath $CacheFile -Value $Candidate.Path -Encoding UTF8
}

function Load-CachedPython {
    if (-not (Test-Path -LiteralPath $CacheFile -PathType Leaf)) {
        return $null
    }
    $cached = (Get-Content -LiteralPath $CacheFile -TotalCount 1 -ErrorAction SilentlyContinue)
    $candidate = New-PythonCandidate $cached "缓存"
    if ($candidate) {
        return $candidate
    }
    Write-Host "已缓存的 ArcGIS Pro Python 路径失效，将重新搜索："
    Write-Host "  $cached"
    Write-Host ""
    return $null
}

function Search-ArcPyPython {
    Write-Host "正在自动查找可用的 ArcGIS Pro Python 环境..."
    $candidates = @()
    if ($env:LOCALAPPDATA) {
        $candidates += Join-Path $env:LOCALAPPDATA "Programs\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"
    }
    if ($env:ProgramFiles) {
        $candidates += Join-Path $env:ProgramFiles "ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"
    }
    if (${env:ProgramFiles(x86)}) {
        $candidates += Join-Path ${env:ProgramFiles(x86)} "ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"
    }

    foreach ($path in $candidates | Select-Object -Unique) {
        $candidate = New-PythonCandidate $path "自动搜索"
        if ($candidate) {
            return $candidate
        }
    }

    $pathPythons = Get-Command python.exe -All -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -Unique
    foreach ($path in $pathPythons) {
        $candidate = New-PythonCandidate $path "PATH"
        if ($candidate) {
            return $candidate
        }
    }

    Write-Host "自动搜索未找到可导入 arcpy 的 Python。"
    Write-Host ""
    return $null
}

function Search-InArcGISInstallDir {
    param([string]$InstallDir)
    $base = Normalize-InputPath $InstallDir
    if (-not $base) {
        return $null
    }
    if ((Split-Path -Leaf $base) -ieq "python.exe") {
        return New-PythonCandidate $base "用户指定的 python.exe"
    }
    if (-not (Test-Path -LiteralPath $base -PathType Container)) {
        return $null
    }

    $fixedCandidates = @(
        (Join-Path $base "python.exe"),
        (Join-Path $base "envs\arcgispro-py3\python.exe"),
        (Join-Path $base "Python\envs\arcgispro-py3\python.exe"),
        (Join-Path $base "bin\Python\envs\arcgispro-py3\python.exe")
    )
    foreach ($path in $fixedCandidates) {
        $candidate = New-PythonCandidate $path "用户选择的 ArcGIS Pro 安装目录"
        if ($candidate) {
            return $candidate
        }
    }

    $pythonFiles = Get-ChildItem -LiteralPath $base -Filter python.exe -Recurse -File -ErrorAction SilentlyContinue
    foreach ($file in $pythonFiles) {
        $candidate = New-PythonCandidate $file.FullName "用户选择的 ArcGIS Pro 安装目录"
        if ($candidate) {
            return $candidate
        }
    }
    return $null
}

function Prompt-ArcGISInstallDir {
    Write-Host ""
    Write-Host "请粘贴 ArcGIS Pro 安装目录。"
    Write-Host "如果不知道，常见目录是：C:\Program Files\ArcGIS\Pro"
    $inputPath = Read-Host "ArcGIS Pro 安装目录"
    $candidate = Search-InArcGISInstallDir $inputPath
    if (-not $candidate) {
        Write-Host "该目录下没有找到可导入 arcpy 的 ArcGIS Pro Python。"
        Write-Host ""
    }
    return $candidate
}

function Prompt-ExactPython {
    Write-Host ""
    Write-Host "请粘贴精确的 python.exe 路径。"
    $inputPath = Read-Host "python.exe 路径"
    $candidate = New-PythonCandidate $inputPath "用户指定"
    if (-not $candidate) {
        Write-Host "该 python.exe 无法导入 arcpy，不是可用的 ArcGIS Pro Python 环境。"
        Write-Host ""
    }
    return $candidate
}

function Confirm-PythonCandidate {
    param($Candidate)
    $current = $Candidate
    while ($true) {
        Write-Host ""
        Write-Host "已找到可导入 arcpy 的 Python 环境（来源：$($current.Source)）："
        Write-Host "  $($current.Path)"
        Write-Host ""
        Write-Host "如果该环境被额外安装过很多包，可能存在依赖冲突。建议使用 ArcGIS Pro 自带的 arcgispro-py3 环境，或专门为本工具准备的干净克隆环境。"
        Write-Host ""
        Write-Host "[Y] 使用当前环境并启动"
        Write-Host "[1] 输入 ArcGIS Pro 安装目录"
        Write-Host "[2] 输入精确的 python.exe 路径"
        Write-Host "[3] 重新自动搜索"
        Write-Host "[Q] 退出"
        Write-Host ""
        $choice = (Read-Host "请选择 Y / 1 / 2 / 3 / Q，然后按回车").Trim()
        if (-not $choice) {
            continue
        }
        switch ($choice.Substring(0, 1).ToUpperInvariant()) {
            "Y" { return $current }
            "1" {
                $next = Prompt-ArcGISInstallDir
                if ($next) {
                    $current = $next
                }
            }
            "2" {
                $next = Prompt-ExactPython
                if ($next) {
                    $current = $next
                }
            }
            "3" {
                $next = Search-ArcPyPython
                if ($next) {
                    $current = $next
                }
            }
            "Q" { return $null }
            default { Write-Host "输入无效，请重新选择。" }
        }
    }
}

function Manual-PythonLoop {
    while ($true) {
        Write-Host "未确认可用的 ArcGIS Pro Python 环境。"
        Write-Host ""
        Write-Host "请按顺序尝试下面的方式。找到并确认正确环境前，本窗口会停留在这里。"
        Write-Host ""
        Write-Host "[1] 输入 ArcGIS Pro 安装目录，例如 C:\Program Files\ArcGIS\Pro"
        Write-Host "[2] 输入精确的 python.exe 路径，例如 C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"
        Write-Host "[3] 重新自动搜索"
        Write-Host "[Q] 退出"
        Write-Host ""
        $choice = (Read-Host "请选择 1 / 2 / 3 / Q，然后按回车").Trim()
        if (-not $choice) {
            continue
        }
        switch ($choice.Substring(0, 1).ToUpperInvariant()) {
            "1" {
                $candidate = Prompt-ArcGISInstallDir
                if ($candidate) {
                    return Confirm-PythonCandidate $candidate
                }
            }
            "2" {
                $candidate = Prompt-ExactPython
                if ($candidate) {
                    return Confirm-PythonCandidate $candidate
                }
            }
            "3" {
                $candidate = Search-ArcPyPython
                if ($candidate) {
                    return Confirm-PythonCandidate $candidate
                }
            }
            "Q" { return $null }
            default { Write-Host "输入无效，请重新选择。" }
        }
    }
}

if ($ResetCache) {
    if (Test-Path -LiteralPath $CacheFile) {
        Remove-Item -LiteralPath $CacheFile -Force
    }
    Write-Host "已清除 ArcGIS Pro Python 环境缓存。"
    Write-Host ""
}

$candidate = Load-CachedPython
if (-not $candidate) {
    $candidate = Search-ArcPyPython
}

if ($PrintOnly) {
    if ($candidate) {
        Save-PythonCache $candidate
        Write-Output $candidate.Path
        exit 0
    }
    exit 1
}

$confirmed = if ($candidate) { Confirm-PythonCandidate $candidate } else { Manual-PythonLoop }
if (-not $confirmed) {
    exit 0
}

Save-PythonCache $confirmed
Write-Host "使用 ArcGIS Pro Python 环境："
Write-Host "  $($confirmed.Path)"
Write-Host ""

& $confirmed.Path (Join-Path $ProjectRoot "run_workflow_ui.py")
exit $LASTEXITCODE
