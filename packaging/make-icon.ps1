# Genera packaging/icon.ico (se versiona; el .exe lo incrusta) con el mismo dibujo que icon.svg.
Add-Type -AssemblyName System.Drawing
$ErrorActionPreference = "Stop"

function Draw([int]$size) {
    $bmp = New-Object Drawing.Bitmap $size, $size
    $g = [Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = "AntiAlias"
    $g.ScaleTransform($size / 256, $size / 256)

    $bg = New-Object Drawing.Drawing2D.GraphicsPath
    $r = 96
    $bg.AddArc(8, 8, $r, $r, 180, 90); $bg.AddArc(248 - $r, 8, $r, $r, 270, 90)
    $bg.AddArc(248 - $r, 248 - $r, $r, $r, 0, 90); $bg.AddArc(8, 248 - $r, $r, $r, 90, 90)
    $bg.CloseFigure()
    $g.FillPath((New-Object Drawing.Drawing2D.LinearGradientBrush((New-Object Drawing.Point 0, 8), (New-Object Drawing.Point 0, 248),
        [Drawing.Color]::FromArgb(31, 58, 95), [Drawing.Color]::FromArgb(14, 28, 48))), $bg)

    # Tres discos de abajo arriba: el contorno oscuro los separa también a 16 px.
    $disk = New-Object Drawing.SolidBrush ([Drawing.Color]::FromArgb(236, 242, 248))
    $edge = New-Object Drawing.Pen ([Drawing.Color]::FromArgb(20, 38, 64)), 11
    foreach ($y in 148, 104, 60) {
        $p = New-Object Drawing.Drawing2D.GraphicsPath
        $p.AddArc(52, $y, 152, 48, 180, 180)
        $p.AddLine(204, ($y + 24), 204, ($y + 54))
        $p.AddArc(52, ($y + 30), 152, 48, 0, 180)
        $p.CloseFigure()
        $g.FillPath($disk, $p)
        $g.DrawPath($edge, $p)
    }
    $g.FillEllipse((New-Object Drawing.SolidBrush ([Drawing.Color]::FromArgb(143, 170, 200))), 62, 66, 132, 36)

    # Punto de estado: la instancia está viva.
    $g.FillEllipse((New-Object Drawing.SolidBrush ([Drawing.Color]::FromArgb(14, 28, 48))), 154, 154, 84, 84)
    $g.FillEllipse((New-Object Drawing.SolidBrush ([Drawing.Color]::FromArgb(46, 194, 126))), 166, 166, 60, 60)
    $g.Dispose()
    $ms = New-Object IO.MemoryStream
    $bmp.Save($ms, [Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    return , $ms.ToArray()
}

$sizes = 16, 20, 24, 32, 40, 48, 64, 128, 256
$images = @(); foreach ($s in $sizes) { $images += , (Draw $s) }
$out = New-Object IO.MemoryStream
$w = New-Object IO.BinaryWriter $out
$w.Write([uint16]0); $w.Write([uint16]1); $w.Write([uint16]$sizes.Count)
$offset = 6 + 16 * $sizes.Count
for ($i = 0; $i -lt $sizes.Count; $i++) {
    $b = if ($sizes[$i] -eq 256) { 0 } else { $sizes[$i] }
    $w.Write([byte]$b); $w.Write([byte]$b); $w.Write([byte]0); $w.Write([byte]0)
    $w.Write([uint16]1); $w.Write([uint16]32); $w.Write([uint32]$images[$i].Length); $w.Write([uint32]$offset)
    $offset += $images[$i].Length
}
foreach ($img in $images) { $w.Write([byte[]]$img) }
[IO.File]::WriteAllBytes((Join-Path $PSScriptRoot "icon.ico"), $out.ToArray())
[IO.File]::WriteAllBytes((Join-Path $PSScriptRoot "icon-preview.png"), $images[-1])
