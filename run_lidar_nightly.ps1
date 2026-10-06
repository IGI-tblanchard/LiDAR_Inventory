$ErrorActionPreference = 'Stop'

$qgisPython = 'C:\Program Files\QGIS 3.42.3\bin\python-qgis.bat'
$propy = 'C:\Program Files\ArcGIS\Pro\bin\Python\Scripts\propy.bat'
$scriptRoot = $PSScriptRoot
$pipeline = @(
    [pscustomobject]@{
        Name = '1_reproject_LiDAR_nightly.py'
        Launcher = $qgisPython
        Description = 'LiDAR reprojection (QGIS GDAL/AIG)'
    },
    [pscustomobject]@{
        Name = '2_DEM_to_mosaic_nightly.py'
        Launcher = $propy
        Description = 'DEM mosaic update (ArcGIS Pro/ArcPy)'
    }
)
$reportRoot = '\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Staging\LiDAR_Reports'
$dataRoot = '\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive'
$runStamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$transcriptPath = Join-Path $reportRoot "lidar_nightly_scheduler_$runStamp.log"

$transcriptStarted = $false
$failureCount = 0

try {
    New-Item -ItemType Directory -Path $reportRoot -Force | Out-Null
    if (-not (Test-Path -LiteralPath $reportRoot -PathType Container)) {
        throw "Reports folder is unavailable: $reportRoot"
    }

    Start-Transcript -LiteralPath $transcriptPath -Force | Out-Null
    $transcriptStarted = $true
    $env:UAV_REPORT_DIR = $reportRoot
    Write-Output "Started LiDAR nightly workflow: $(Get-Date -Format o)"
    Write-Output "Working folder: $scriptRoot"
    Write-Output "UNC data share check: $(Test-Path -LiteralPath $dataRoot)"

    if (-not (Test-Path -LiteralPath $dataRoot -PathType Container)) {
        throw "UNC data share is unavailable to this account: $dataRoot"
    }

    Set-Location -LiteralPath $scriptRoot
    foreach ($step in $pipeline) {
        $pythonScript = Join-Path $scriptRoot $step.Name
        if (-not (Test-Path -LiteralPath $step.Launcher -PathType Leaf)) {
            Write-Output "ERROR: Python launcher for $($step.Name) was not found: $($step.Launcher)"
            $failureCount++
            continue
        }
        if (-not (Test-Path -LiteralPath $pythonScript -PathType Leaf)) {
            Write-Output "ERROR: Nightly script was not found: $pythonScript"
            $failureCount++
            continue
        }

        Write-Output "`n===== START $($step.Description): $(Get-Date -Format o) ====="
        Write-Output "Python launcher: $($step.Launcher)"
        $LASTEXITCODE = 0
        & $step.Launcher $pythonScript
        $scriptExitCode = $LASTEXITCODE
        Write-Output "===== END $($step.Name): exit=$scriptExitCode : $(Get-Date -Format o) ====="
        if ($scriptExitCode -ne 0) {
            $failureCount++
        }
    }

    Write-Output "Finished LiDAR nightly workflow: $(Get-Date -Format o)"
    Write-Output "Scripts with nonzero exit codes or missing files: $failureCount"
}
catch {
    Write-Error (($_ | Out-String).Trim())
    $failureCount++
}
finally {
    Remove-Item Env:\UAV_REPORT_DIR -ErrorAction SilentlyContinue
    if ($transcriptStarted) {
        Stop-Transcript | Out-Null
    }
}

if ($failureCount -gt 0) {
    exit 1
}
exit 0
