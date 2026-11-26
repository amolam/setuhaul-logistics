# Renders slides to PNG with the installed PowerPoint (no LibreOffice on this workstation).
#   render.ps1 -Deck <pptx> -OutDir <dir>                  -> slide-01.png ...
#   render.ps1 -Deck <pptx> -OutDir <dir> -Names <txt>     -> <dir>\<name>.png per slide (diagrams_source.pptx)
param([Parameter(Mandatory)]$Deck, [Parameter(Mandatory)]$OutDir, $Names = $null, [int]$Width = 1920, [int]$Height = 1080)
$ErrorActionPreference = 'Stop'
$pp = New-Object -ComObject PowerPoint.Application
try {
    $pres = $pp.Presentations.Open((Resolve-Path $Deck).Path, $true, $false, $false)
    $list = if ($Names) { Get-Content $Names } else { $null }
    $i = 1
    foreach ($s in $pres.Slides) {
        $file = if ($list) { Join-Path $OutDir ($list[$i - 1] + '.png') } else { Join-Path $OutDir ('slide-{0:D2}.png' -f $i) }
        New-Item -ItemType Directory -Force (Split-Path $file) | Out-Null
        $s.Export($file, 'PNG', $Width, $Height)
        $i++
    }
    $pres.Close()
} finally { $pp.Quit() }
