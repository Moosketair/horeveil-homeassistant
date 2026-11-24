param(
    [Parameter(Mandatory = $true)][string] $Java,
    [string] $AndroidRepo = (Join-Path $PSScriptRoot '../../horeveil'),
    [string] $GradleCache = (Join-Path $env:USERPROFILE '.gradle/caches/modules-2/files-2.1')
)

$ErrorActionPreference = 'Stop'
$target = Join-Path $PSScriptRoot '../.venv/android-qr-probe'
New-Item -ItemType Directory -Path $target -Force | Out-Null
$target = (Resolve-Path -LiteralPath $target).Path
$source = Join-Path $target 'PairingProtocol.kt'
$path = 'app/src/main/java/fr/moosketair/horeveil/platform/homeassistant/PairingProtocol.kt'
$androidGit = Join-Path ([System.IO.Path]::GetFullPath($AndroidRepo)) '.git'
$kotlin = git --git-dir $androidGit show "0bd42b0:$path"
if ($LASTEXITCODE -ne 0) { throw 'Impossible de lire le parseur Android au commit 0bd42b0.' }
Set-Content -LiteralPath $source -Value $kotlin -Encoding utf8

function Find-Jar($coordinate) {
    $jar = Get-ChildItem -LiteralPath (Join-Path $GradleCache $coordinate) -Recurse -Filter '*.jar' |
        Where-Object { $_.Name -notmatch 'sources|javadoc' } | Select-Object -First 1
    if (-not $jar) { throw "JAR Gradle absent pour $coordinate" }
    $jar.FullName
}

$stdlib = Find-Jar 'org.jetbrains.kotlin/kotlin-stdlib/2.2.10'
$annotations = Find-Jar 'org.jetbrains/annotations/13.0'
$runtime = @(
    $stdlib
    $annotations
    (Find-Jar 'org.jetbrains.kotlinx/kotlinx-serialization-json-jvm/1.9.0')
    (Find-Jar 'org.jetbrains.kotlinx/kotlinx-serialization-core-jvm/1.9.0')
    (Find-Jar 'com.google.zxing/core/3.4.1')
)
$compiler = @(
    $stdlib
    $annotations
    (Find-Jar 'org.jetbrains.kotlin/kotlin-compiler-embeddable/2.2.10')
    (Find-Jar 'org.jetbrains.kotlin/kotlin-script-runtime/2.2.10')
    (Find-Jar 'org.jetbrains.kotlin/kotlin-reflect/2.2.10')
    (Find-Jar 'org.jetbrains.kotlin/kotlin-daemon-embeddable/2.2.10')
    (Find-Jar 'org.jetbrains.kotlinx/kotlinx-coroutines-core-jvm/1.8.0')
)
$plugin = Find-Jar 'org.jetbrains.kotlin/kotlin-serialization-compiler-plugin-embeddable/2.2.10'
$separator = [System.IO.Path]::PathSeparator
$classes = Join-Path $target 'probe.jar'
$compileArgs = @(
    '-no-stdlib', '-no-reflect', '-classpath', ($runtime -join $separator), "-Xplugin=$plugin",
    '-d', $classes, $source, (Join-Path $PSScriptRoot 'AndroidPairingQrProbe.kt')
)
& $Java -classpath ($compiler -join $separator) org.jetbrains.kotlin.cli.jvm.K2JVMCompiler @compileArgs
if ($LASTEXITCODE -ne 0) { throw 'Échec de la compilation de la sonde Android.' }
$env:HOREVEIL_ANDROID_QR_JAVA = $Java
$env:HOREVEIL_ANDROID_QR_CLASSPATH = (@($classes) + $runtime) -join $separator
