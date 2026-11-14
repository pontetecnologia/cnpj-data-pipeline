# Execução agendada (Agendador de Tarefas do Windows) da prospecção Receita -> GestaoRevenda.
# Sobe o Postgres do pipeline (docker compose) e roda rodar_mensal.py com o Python do .venv.
# Código de saída = o do rodar_mensal (0 = OK), para o Agendador marcar falha.
$ErrorActionPreference = 'Stop'
$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz

docker compose up -d postgres | Out-Null
$pronto = $false
for ($i = 0; $i -lt 30; $i++) {
    docker exec cnpj-pipeline-postgres pg_isready -U postgres | Out-Null
    if ($LASTEXITCODE -eq 0) { $pronto = $true; break }
    Start-Sleep -Seconds 2
}
if (-not $pronto) { Write-Error 'PostgreSQL do pipeline não respondeu'; exit 1 }

& "$raiz\.venv\Scripts\python.exe" "$raiz\rodar_mensal.py" @args
exit $LASTEXITCODE
