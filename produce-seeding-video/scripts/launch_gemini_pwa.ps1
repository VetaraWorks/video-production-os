param(
    [string]$SkillRoot = "$PSScriptRoot\..",
    [Alias("WorkRoot")][string]$DataRoot = "",
    [string]$PythonPath = "",
    [Alias("ChromePath")][string]$BrowserPath = "",
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

$resolvedPython = Resolve-PythonExecutable $PythonPath
$arguments = @($videoOsScript, "worker", "login")
if (-not [string]::IsNullOrWhiteSpace($DataRoot)) { $arguments += @("--data-root", $DataRoot) }
if (-not [string]::IsNullOrWhiteSpace($BrowserPath)) { $arguments += @("--browser", $BrowserPath) }
if ($RemoteDebuggingPort -gt 0) { $arguments += @("--cdp-port", [string]$RemoteDebuggingPort) }

& $resolvedPython @arguments
exit $LASTEXITCODE
