#Requires -Version 5.1
<#
.SYNOPSIS
    Remove o serviço Windows "Ponte - Prospecção Receita".

.DESCRIPTION
    Rodar num PowerShell COMO ADMINISTRADOR:
        powershell -ExecutionPolicy Bypass -File scripts\desinstalar_servico.ps1

    Para o serviço (uma execução em andamento é interrompida e retomada na próxima vez que rodar),
    remove o serviço, as permissões da conta virtual e o ícone da área de trabalho. Mantém estado\, logs\, saida\, parquet\ e o
    .env: reinstalar continua de onde parou, sem reenviar nada.
#>
$ErrorActionPreference = 'Stop'
$raiz = Split-Path -Parent $PSScriptRoot
$id = 'PonteProspeccaoReceita'
$conta = "NT SERVICE\$id"
$exe = Join-Path $raiz "servico\$id.exe"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'ERRO: rode este script num PowerShell aberto como Administrador.' -ForegroundColor Red
    exit 1
}

$svc = Get-Service -Name $id -ErrorAction SilentlyContinue
if (-not $svc) {
    Write-Host "Serviço $id não está instalado."
}
else {
    if ($svc.Status -ne 'Stopped') {
        Write-Host '==> Parando o serviço'
        Stop-Service -Name $id -Force
        $svc.WaitForStatus('Stopped', '00:02:00')
    }
    Write-Host '==> Removendo o serviço'
    if (Test-Path $exe) { & $exe uninstall | Out-Host } else { & sc.exe delete $id | Out-Host }
    for ($i = 0; $i -lt 30 -and (Get-Service -Name $id -ErrorAction SilentlyContinue); $i++) { Start-Sleep -Seconds 1 }
    if (Get-Service -Name $id -ErrorAction SilentlyContinue) {
        Write-Host 'Aviso: o serviço ficou "marcado para exclusão"; some ao fechar o services.msc ou reiniciar.' -ForegroundColor Yellow
    }
}

# Permissões da conta virtual (a conta deixa de existir com o serviço; tira as entradas órfãs)
Write-Host '==> Removendo as permissões da conta virtual'
$alvos = @($raiz, (Join-Path $raiz '.env')) + (@('logs', 'estado', 'saida', 'temp', 'parquet') | ForEach-Object { Join-Path $raiz $_ })
foreach ($alvo in $alvos) {
    if (Test-Path $alvo) { & icacls.exe $alvo /remove:g $conta /C /Q 2>$null | Out-Null }
}

# Ícone da tela na área de trabalho de todos os usuários
$atalho = Join-Path ([Environment]::GetFolderPath('CommonDesktopDirectory')) 'Prospecção Receita.url'
if (Test-Path $atalho) {
    Write-Host '==> Removendo o ícone "Prospecção Receita" da área de trabalho'
    Remove-Item $atalho -Force
}

Write-Host ''
Write-Host 'Serviço removido. Mantidos: estado\, logs\, saida\, parquet\, .env e servico\ (WinSW).'
