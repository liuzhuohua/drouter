# OCR the user's layout screenshots with bounding boxes (Windows built-in OCR).
# Output: TSV  text<TAB>x,y,w,h  -> lets us spot "tall & narrow" text boxes (= vertical text).
param([string[]]$Paths)

Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]

function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}

[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) {
    Write-Output "NO_OCR_ENGINE"
    exit 2
}
Write-Output ("ENGINE_LANG=" + $engine.RecognizerLanguage.LanguageTag)

foreach ($p in $Paths) {
    if (-not (Test-Path $p)) { Write-Output ("MISSING " + $p); continue }
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($p)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $res = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    Write-Output ("=== " + (Split-Path $p -Leaf) + " " + $decoder.PixelWidth + "x" + $decoder.PixelHeight)
    foreach ($line in $res.Lines) {
        foreach ($w in $line.Words) {
            $r = $w.BoundingRect
            Write-Output ("{0}`t{1},{2},{3},{4}" -f $w.Text, [int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height)
        }
    }
    $stream.Dispose()
}
Write-Output "OCR_DONE"
