# Stage 4.5 - test the unified model API through the gateway.
# Run:  .\request.ps1            (defaults to gpt-4o)
#       .\request.ps1 luna
#       .\request.ps1 deepseek

param([string]$Model = "gpt-4o")

$rg      = "multi-llm-chatbot-rg"
$apim    = "apim-multillm-contoso-university"
$sub     = az account show --query id -o tsv
$armBase = "https://management.azure.com/subscriptions/$sub/resourceGroups/$rg/providers/Microsoft.ApiManagement/service/$apim"

# Keys are only returned by POST listSecrets, never by a plain GET.
$key = az rest --method post `
  --url "$armBase/subscriptions/master/listSecrets?api-version=2024-05-01" `
  --query primaryKey -o tsv

$gw = "https://$apim.azure-api.net/llm/v1"

$body = @{
    model      = $Model
    messages   = @(@{ role = "user"; content = "Reply with just: OK" })
    max_tokens = 10
} | ConvertTo-Json -Depth 5

# Content-Type is required: without it the model receives untyped bytes
# and returns "Input should be a valid dictionary".
$headers = @{
    "Ocp-Apim-Subscription-Key" = $key
    "Content-Type"              = "application/json"
}

Write-Host "POST $gw/chat/completions  (model=$Model)" -ForegroundColor Cyan
$resp = Invoke-RestMethod -Method Post -Uri "$gw/chat/completions" -Body $body -Headers $headers

Write-Host "reply  : $($resp.choices[0].message.content)"
Write-Host "backend: $($resp.model)"
Write-Host "tokens : $($resp.usage.total_tokens)"
