param(
	[string]$PackagePath = (Join-Path $PSScriptRoot "dist\nvdaHttpBridge-1.3.0.nvda-addon")
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$sourceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "nvda-addon")).Path.TrimEnd("\")
$outputFullPath = [System.IO.Path]::GetFullPath($PackagePath)
$outputDirectory = Split-Path -Parent $outputFullPath
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null

if (Test-Path -LiteralPath $outputFullPath) {
	Remove-Item -LiteralPath $outputFullPath -Force
}

$archive = [System.IO.Compression.ZipFile]::Open(
	$outputFullPath,
	[System.IO.Compression.ZipArchiveMode]::Create
)
try {
	Get-ChildItem -LiteralPath $sourceRoot -Recurse -File |
		Where-Object {
			$_.Extension -ne ".pyc" -and
			$_.FullName -notmatch "[\\/]__pycache__[\\/]"
		} |
		ForEach-Object {
			$relativePath = $_.FullName.Substring($sourceRoot.Length + 1).Replace("\", "/")
			[System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
				$archive,
				$_.FullName,
				$relativePath,
				[System.IO.Compression.CompressionLevel]::Optimal
			) | Out-Null
		}
}
finally {
	$archive.Dispose()
}

$artifact = Get-Item -LiteralPath $outputFullPath
$hash = Get-FileHash -LiteralPath $outputFullPath -Algorithm SHA256
[pscustomobject]@{
	Path = $artifact.FullName
	Bytes = $artifact.Length
	SHA256 = $hash.Hash
}
