# Activate this machine's existing ESP-IDF without changing global settings.
# Usage: . .\tools\export-idf.ps1
param(
    [string]$ProfilePath = 'C:/Espressif/tools/Microsoft.v6.1.PowerShell_profile.ps1'
)
. $ProfilePath

# EIM may rebuild PATH during selection; these are the actual executable locations.
$normalizedPath = @(
    'C:/Espressif/tools/ccache/4.12.1/ccache-4.12.1-windows-x86_64',
    'C:/Espressif/tools/ninja/1.12.1',
    'C:/Espressif/tools/git/usr/bin',
    $env:PATH,
    $env:Path
) -join ';'
[Environment]::SetEnvironmentVariable('PATH', $normalizedPath, 'Process')
[Environment]::SetEnvironmentVariable('Path', $normalizedPath, 'Process')
