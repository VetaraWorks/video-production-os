param(
    [string]$SkillRoot = "$PSScriptRoot\..",
    [Alias("WorkRoot")][string]$DataRoot = "",
    [Alias("ChromePath")][string]$BrowserPath = "",
    [string]$NodePath = "",
    [string]$PythonPath = "",
    [string]$NodeModules = "",
    [string]$FFmpegPath = "",
    [string]$FFprobePath = "",
    [int]$RemoteDebuggingPort = 0
)

$ErrorActionPreference = "Stop"
$resolvedSkillRoot = [System.IO.Path]::GetFullPath($SkillRoot)
$videoOsScript = Join-Path $resolvedSkillRoot "scripts\video_os.py"

function Resolve-PythonExecutable {
    param([string]$ExplicitPath)
    foreach ($candidate in @($ExplicitPath, $env:VIDEO_OS_PYTHON, (Join-Path $resolvedSkillRoot "runtime\python\python.exe"))) {
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return [System.IO.Path]::GetFullPath($candidate)
        }
    }
    foreach ($name in @("python.exe", "python", "python3")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command -and $command.Source -and (Test-Path -LiteralPath $command.Source -PathType Leaf)) {
            return $command.Source
        }
    }
    throw "Python runtime unavailable [runtime.python.unavailable]. Pass -PythonPath or set VIDEO_OS_PYTHON."
}

if (-not (Test-Path -LiteralPath $videoOsScript -PathType Leaf)) {
    throw "Video OS CLI not found: $videoOsScript"
}
$resolvedPython = Resolve-PythonExecutable $PythonPath
$arguments = @($videoOsScript, "worker", "login")
foreach ($item in @(
    @{ Name = "data-root"; Value = $DataRoot },
    @{ Name = "browser"; Value = $BrowserPath },
    @{ Name = "node"; Value = $NodePath },
    @{ Name = "python"; Value = $resolvedPython },
    @{ Name = "node-modules"; Value = $NodeModules },
    @{ Name = "ffmpeg"; Value = $FFmpegPath },
    @{ Name = "ffprobe"; Value = $FFprobePath }
)) {
    if (-not [string]::IsNullOrWhiteSpace($item.Value)) {
        $arguments += @("--$($item.Name)", $item.Value)
    }
}
if ($RemoteDebuggingPort -gt 0) {
    $arguments += @("--cdp-port", [string]$RemoteDebuggingPort)
}

$resultText = (& $resolvedPython @arguments | Out-String)
if ($LASTEXITCODE -ne 0) {
    throw "Gemini Browser Worker initialization failed. Run video_os.py worker status for details."
}
$result = $resultText | ConvertFrom-Json
$configPath = $result.config_path
$config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
$resolvedDataRoot = $result.data_root

$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell

$loginShortcut = $shell.CreateShortcut((Join-Path $desktop "Gemini Worker Login.lnk"))
$loginShortcut.TargetPath = $resolvedPython
$loginShortcut.Arguments = "`"$videoOsScript`" worker login --data-root `"$resolvedDataRoot`""
$loginShortcut.WorkingDirectory = $resolvedDataRoot
$loginShortcut.IconLocation = "$($config.browserPath),0"
$loginShortcut.Save()

$runShortcut = $shell.CreateShortcut((Join-Path $desktop "Gemini Worker - Run Once.lnk"))
$runShortcut.TargetPath = $config.nodePath
$runShortcut.Arguments = "`"$(Join-Path $resolvedSkillRoot 'scripts\gemini_worker.mjs')`" once --config `"$configPath`""
$runShortcut.WorkingDirectory = (Split-Path -Parent $configPath)
$runShortcut.IconLocation = "$($config.browserPath),0"
$runShortcut.Save()

$startShortcut = $shell.CreateShortcut((Join-Path $desktop "Gemini Worker - Start.lnk"))
$startShortcut.TargetPath = $resolvedPython
$startShortcut.Arguments = "`"$videoOsScript`" worker start --data-root `"$resolvedDataRoot`""
$startShortcut.WorkingDirectory = $resolvedDataRoot
$startShortcut.IconLocation = "$($config.browserPath),0"
$startShortcut.Save()

[ordered]@{
    ok = $true
    status = $result.status
    login_state = $result.login_state
    config = $configPath
    data_root = $resolvedDataRoot
    profile = $config.userDataDir
    browser = $config.browserPath
    browser_type = $config.browserType
    cdp_port = $config.remoteDebuggingPort
    ffmpeg = $config.ffmpegPath
    ffprobe = $config.ffprobePath
    login_shortcut = (Join-Path $desktop "Gemini Worker Login.lnk")
    start_shortcut = (Join-Path $desktop "Gemini Worker - Start.lnk")
    run_once_shortcut = (Join-Path $desktop "Gemini Worker - Run Once.lnk")
} | ConvertTo-Json
