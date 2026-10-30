# Construye dist\infranettone-oracle_<versión>_windows.exe con el csc.exe que trae Windows (sin SDK ni NuGet) y pasa
# las pruebas. Uso: windows\build.ps1 [versión]
param([string]$Version = "0.0.0")
$ErrorActionPreference = "Stop"

$root = Split-Path $PSScriptRoot
$version = $Version -replace '^v', ''
$csc = Join-Path $env:windir "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
$obj = Join-Path $root "windows\obj"
$dist = Join-Path $root "dist"
New-Item -ItemType Directory -Force $obj, $dist | Out-Null

# AssemblyVersion solo admite números; la versión completa va en la informativa (Application.ProductVersion).
$numeric = if ($version -match '^(\d+\.\d+\.\d+)') { $Matches[1] } else { "0.0.0" }
$versionFile = Join-Path $obj "Version.cs"
@"
using System.Reflection;
[assembly: AssemblyTitle("Oracle Manager")]
[assembly: AssemblyProduct("Oracle Manager")]
[assembly: AssemblyVersion("$numeric.0")]
[assembly: AssemblyFileVersion("$numeric.0")]
[assembly: AssemblyInformationalVersion("$version")]
"@ | Set-Content -Encoding UTF8 $versionFile

$common = "/nologo", "/optimize+", "/codepage:65001", "/utf8output", "/warnaserror+",
    "/r:System.dll", "/r:System.Core.dll", "/r:System.Web.Extensions.dll",
    "/resource:$root\oracle_manager\remote\discover.sh,discover.sh",
    "/resource:$root\oracle_manager\remote\dump.sh,dump.sh"

$exe = Join-Path $dist "infranettone-oracle_${version}_windows.exe"
& $csc @common /target:winexe "/out:$exe" "/win32manifest:$root\windows\app.manifest" "/win32icon:$root\packaging\icon.ico" `
    /r:System.Windows.Forms.dll /r:System.Drawing.dll `
    "$root\windows\Core.cs" $versionFile "$root\windows\Program.cs" "$root\windows\Installer.cs" "$root\windows\Dialogs.cs" "$root\windows\MainForm.cs"
if ($LASTEXITCODE) { exit $LASTEXITCODE }

$tests = Join-Path $obj "Tests.exe"
& $csc @common /target:exe "/out:$tests" "$root\windows\Core.cs" "$root\windows\Tests.cs"
if ($LASTEXITCODE) { exit $LASTEXITCODE }
& $tests "$root\tests\fixtures"
if ($LASTEXITCODE) { exit $LASTEXITCODE }

"{0}: {1:N0} KB" -f (Split-Path -Leaf $exe), ((Get-Item $exe).Length / 1KB)
