#Requires -Version 5.1
<#
.SYNOPSIS
    Roda a prospecção agora, em primeiro plano, com as mesmas regras do serviço.

.DESCRIPTION
    Não precisa de administrador. Verifica o mês mais recente da Receita; se ainda não foi
    processado, confere o disco, roda o fluxo do rodar_mensal e manda o resultado ao GestaoRevenda
    (que envia o e-mail). Usa a mesma trava do serviço: se ele estiver rodando, este comando sai sem
    fazer nada (e vice-versa). Configuração e envio ligado/desligado: tela do GestaoRevenda.

    scripts\rodar_agora.ps1                  # igual à verificação diária
    scripts\rodar_agora.ps1 -SemEnvio        # gera a lista sem enviar
    scripts\rodar_agora.ps1 -Limite 20       # envia só 20 (primeiro teste real)
    scripts\rodar_agora.ps1 -Forcar          # roda mesmo se o mês já foi processado
    scripts\rodar_agora.ps1 -Recarregar      # baixa e carrega o mês do zero (~30 min)
    scripts\rodar_agora.ps1 -Status          # configuração, verificações, execuções e estado do serviço
    scripts\rodar_agora.ps1 -TestarConexao   # busca a configuração e manda um sinal ao GestaoRevenda
#>
param(
    [switch]$SemEnvio,
    [int]$Limite = 0,
    [switch]$Forcar,
    [switch]$Recarregar,
    [switch]$Status,
    [switch]$TestarConexao
)

$raiz = Split-Path -Parent $PSScriptRoot
$py = Join-Path $raiz '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { Write-Host "ERRO: não achei $py (rode scripts\instalar_servico.ps1)." -ForegroundColor Red; exit 1 }
$script = Join-Path $raiz 'servico_prospeccao.py'

if ($Status) {
    $svc = Get-CimInstance Win32_Service -Filter "Name='PonteProspeccaoReceita'" -ErrorAction SilentlyContinue
    if ($svc) { Write-Host "Serviço Windows: $($svc.State) ($($svc.StartMode), conta $($svc.StartName))`n" }
    else { Write-Host "Serviço Windows: não instalado`n" }
    & $py $script --status
    exit $LASTEXITCODE
}
if ($TestarConexao) { & $py $script --testar-conexao; exit $LASTEXITCODE }

$argumentos = @($script, '--agora')
if ($SemEnvio) { $argumentos += '--sem-envio' }
if ($Limite -gt 0) { $argumentos += @('--limite', "$Limite") }
if ($Forcar) { $argumentos += '--forcar' }
if ($Recarregar) { $argumentos += '--recarregar' }
& $py @argumentos
exit $LASTEXITCODE
