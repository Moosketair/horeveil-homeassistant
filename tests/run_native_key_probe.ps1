param(
    [Parameter(Mandatory = $true)][string] $AndroidSdk,
    [Parameter(Mandatory = $true)][string] $JavaHome,
    [Parameter(Mandatory = $true)][ValidatePattern('^emulator-\d+$')][string] $Emulator,
    [string] $BuildTools = '36.0.0',
    [string] $Platform = 'android-37.0'
)

$ErrorActionPreference = 'Stop'
$env:JAVA_HOME = $JavaHome
$target = New-Item -ItemType Directory -Force (Join-Path $PSScriptRoot '../.venv/qualification/android')
$tools = Join-Path $AndroidSdk "build-tools/$BuildTools"
$androidJar = Join-Path $AndroidSdk "platforms/$Platform/android.jar"
$adb = Join-Path $AndroidSdk 'platform-tools/adb.exe'
$app = 'fr.moosketair.horeveil.qualification'
$maven = 'https://repo.maven.apache.org/maven2/'
$libraries = foreach ($artifact in @(
    'com/authlete/http-message-signatures/1.8/http-message-signatures-1.8.jar',
    'com/nimbusds/nimbus-jose-jwt/9.41.1/nimbus-jose-jwt-9.41.1.jar',
    'org/greenbytes/http/structured-fields/0.4/structured-fields-0.4.jar'
)) {
    $jar = Join-Path $target (Split-Path $artifact -Leaf)
    Invoke-WebRequest -Uri ($maven + $artifact) -OutFile $jar
    $jar
}

function Require-Success($operation) {
    if ($LASTEXITCODE -ne 0) { throw "$operation a échoué ($LASTEXITCODE)." }
}

function Invoke-ProbeAdb([string[]] $Command, [int] $TimeoutSeconds = 20, [switch] $AllowFailure) {
    $start = [System.Diagnostics.ProcessStartInfo]::new($adb)
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    foreach ($arg in @('-s', $Emulator) + $Command) { $start.ArgumentList.Add($arg) }
    $process = [System.Diagnostics.Process]::Start($start)
    try {
        $output = $process.StandardOutput.ReadToEndAsync()
        $errors = $process.StandardError.ReadToEndAsync()
        if (!$process.WaitForExit($TimeoutSeconds * 1000)) {
            $process.Kill()
            throw "Délai ADB dépassé pour $Emulator ($($Command[0]))."
        }
        if ($process.ExitCode -ne 0 -and !$AllowFailure) { throw ($errors.GetAwaiter().GetResult()) }
        $output.GetAwaiter().GetResult() -split '\r?\n' | Where-Object { $_ }
    } finally { $process.Dispose() }
}

if ((Invoke-ProbeAdb @('shell', '-n', '-T', 'getprop', 'ro.kernel.qemu')) -ne '1') { throw 'La cible doit être un émulateur.' }
if ((Invoke-ProbeAdb @('shell', '-n', '-T', 'pm', 'path', $app) -AllowFailure) -match '^package:') { throw 'Le paquet de recette existe déjà.' }

$manifest = Join-Path $target 'AndroidManifest.xml'
@'
<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="fr.moosketair.horeveil.qualification">
  <uses-sdk android:minSdkVersion="26" android:targetSdkVersion="35"/>
  <application android:label="Qualification Horéveil"/>
  <instrumentation android:name=".NativeTransportKeyProbe" android:targetPackage="fr.moosketair.horeveil.qualification"/>
</manifest>
'@ | Set-Content -LiteralPath $manifest -Encoding utf8

& "$JavaHome/bin/javac.exe" -source 8 -target 8 -cp ((@($androidJar) + $libraries) -join ';') -d $target.FullName `
    (Join-Path $PSScriptRoot 'NativeTransportKeyProbe.java') (Join-Path $PSScriptRoot 'NativeTransportSignatureProbe.java')
Require-Success 'javac'
$classFile = Join-Path $target 'fr/moosketair/horeveil/qualification/NativeTransportKeyProbe.class'
$signatureClass = Join-Path $target 'fr/moosketair/horeveil/qualification/NativeTransportSignatureProbe.class'
& "$tools/d8.bat" --min-api 26 --lib $androidJar --output $target.FullName $classFile $signatureClass @libraries
Require-Success 'D8'
$unsigned = Join-Path $target 'probe-unsigned.apk'
$apk = Join-Path $target 'probe.apk'
& "$tools/aapt.exe" package -f -M $manifest -I $androidJar -F $unsigned
Require-Success 'aapt package'
Push-Location $target.FullName
try {
    & "$tools/aapt.exe" add $unsigned classes.dex
    Require-Success 'aapt add'
} finally { Pop-Location }

$signingKey = Join-Path $target ('signing-' + [guid]::NewGuid() + '.p12')
$password = [guid]::NewGuid().ToString('N')
try {
    & "$JavaHome/bin/keytool.exe" -genkeypair -keystore $signingKey -storepass $password -alias probe `
        -keyalg RSA -keysize 2048 -validity 2 -dname 'CN=Horeveil Qualification'
    Require-Success 'keytool'
    & "$tools/apksigner.bat" sign --ks $signingKey --ks-pass "pass:$password" --out $apk $unsigned
    Require-Success 'apksigner'
} finally {
    if (Test-Path -LiteralPath $signingKey) { Remove-Item -LiteralPath $signingKey }
}

Invoke-ProbeAdb @('install', $apk) -TimeoutSeconds 90
try {
    $publicKey = $null
    foreach ($attempt in 1..2) {
        $result = Invoke-ProbeAdb @('shell', '-n', '-T', 'am', 'instrument', '-w', '-r', "$app/.NativeTransportKeyProbe")
        if ($result -notcontains 'INSTRUMENTATION_CODE: -1' -or !$result.Where({ $_ -match '^INSTRUMENTATION_RESULT: probe_signature=' })) {
            throw ($result -join "`n")
        }
        $spki = $result | Select-String '^INSTRUMENTATION_RESULT: probe_spki='
        if ($attempt -eq 1) { $publicKey = $spki.Line }
        elseif ($publicKey -ne $spki.Line) { throw 'La clé a changé entre les deux processus Android.' }
    }
    $result | Set-Content -LiteralPath (Join-Path $target 'result.txt') -Encoding utf8
    'Signature Keystore émise sur émulateur.'
} finally { Invoke-ProbeAdb @('uninstall', $app) }
