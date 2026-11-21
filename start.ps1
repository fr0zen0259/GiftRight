$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:GIFT_RIGHT_PREVIEW_LIMIT = "40"
$env:GIFT_RIGHT_DISPLAY_LIMIT = "20"
$env:DIFY_CANDIDATE_LIMIT = "24"
$env:GIFT_RIGHT_USE_FAMILY_REPRESENTATIVES = "0"
python app.py
