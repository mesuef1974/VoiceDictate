# خادم نطق دائم: يقرأ من stdin سطرًا «مسار_wav<TAB>نص base64» ويكتب wav بصوت ويندوز العربي، ثم يطبع المسار.
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.Encoding]::UTF8
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[void][Windows.Media.SpeechSynthesis.SpeechSynthesizer, Windows.Media.SpeechSynthesis, ContentType = WindowsRuntime]
[void][Windows.Storage.Streams.DataReader, Windows.Storage.Streams, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) { $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(); $t.Result }
$synth = New-Object Windows.Media.SpeechSynthesis.SpeechSynthesizer
$voice = [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices | Where-Object { $_.Language -like 'ar*' } | Select-Object -First 1
if ($voice) { $synth.Voice = $voice }
$synth.Options.SpeakingRate = 1.1
Write-Output ("ready " + $synth.Voice.DisplayName)
while ($true) {
  $line = [Console]::In.ReadLine()
  if ($null -eq $line) { break }
  $parts = $line.Split("`t", 3)
  if ($parts.Count -lt 2) { continue }
  try {
    if ($parts.Count -ge 3) { $synth.Options.SpeakingRate = [double]::Parse($parts[2], [Globalization.CultureInfo]::InvariantCulture) }
    $text = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($parts[1]))
    $stream = Await ($synth.SynthesizeTextToStreamAsync($text)) ([Windows.Media.SpeechSynthesis.SpeechSynthesisStream])
    $reader = New-Object Windows.Storage.Streams.DataReader($stream.GetInputStreamAt(0))
    [void](Await ($reader.LoadAsync([uint32]$stream.Size)) ([uint32]))
    $bytes = New-Object byte[] $stream.Size
    $reader.ReadBytes($bytes)
    [IO.File]::WriteAllBytes($parts[0], $bytes)
    Write-Output ("ok " + $parts[0])
  } catch {
    Write-Output ("error " + $_.Exception.Message)
  }
}
