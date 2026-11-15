#Requires -Version 5.1
<#
.SYNOPSIS
    Instala (ou atualiza) o serviço Windows "Ponte - Prospecção Receita".

.DESCRIPTION
    Rodar num PowerShell COMO ADMINISTRADOR, na pasta do projeto:
        powershell -ExecutionPolicy Bypass -File scripts\instalar_servico.ps1

    1. Exige o Python instalado para todos os usuários (C:\Program Files\Python3xx, >= 3.11)
       e recria o .venv a partir dele, se o atual vier de outro lugar.
    2. Confere o .env (PROSPECCAO_FILTRO=true, OUTPUT_FORMAT=parquet).
    3. Baixa o WinSW 2.12.0 (hash SHA-256 fixo) e gera servico\PonteProspeccaoReceita.xml.
    4. Instala o serviço com a conta virtual NT SERVICE\PonteProspeccaoReceita (sem senha, sem
       administrador), com leitura na pasta do projeto e escrita só em logs, estado, saida, temp e parquet.
    5. Restringe o .env (credenciais) ao SYSTEM, Administradores, ao seu usuário e ao serviço.
    6. Inicia o serviço.

    Pode rodar de novo para atualizar: para o serviço, reinstala e inicia.

.PARAMETER Usuario
    Conta que também roda o rodar_agora.ps1 (recebe escrita nas mesmas pastas). Padrão: quem executa.

.PARAMETER RecriarVenv
    Recria o .venv mesmo se ele já vier do Python de C:\Program Files.

.PARAMETER SemIniciar
    Instala sem iniciar o serviço.
#>
param(
    [string]$Usuario = [Security.Principal.WindowsIdentity]::GetCurrent().Name,
    [switch]$RecriarVenv,
    [switch]$SemIniciar
)

$ErrorActionPreference = 'Stop'
$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz

$id = 'PonteProspeccaoReceita'
$nome = 'Ponte - Prospecção Receita'
$descricao = 'Verifica todo dia se a Receita Federal publicou mês novo de CNPJ e gera a prospecção para o GestaoRevenda (cnpj-pipeline).'
$conta = "NT SERVICE\$id"
$winswVersao = 'v2.12.0'
$winswUrl = "https://github.com/winsw/winsw/releases/download/$winswVersao/WinSW-x64.exe"
# WinSW não publica hash nem assina o binário; este é o SHA-256 do WinSW-x64.exe v2.12.0 baixado
# do release oficial em 2026-11-15. Se o download não bater, o instalador para.
$winswSha256 = '05B82D46AD331CC16BDC00DE5C6332C1EF818DF8CEEFCD49C726553209B3A0DA'

function Passo([string]$texto) { Write-Host "==> $texto" -ForegroundColor Cyan }
function Falha([string]$texto) { Write-Host "ERRO: $texto" -ForegroundColor Red; exit 1 }

# ── 0. Administrador ─────────────────────────────────────────────────────────
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Falha 'Rode este script num PowerShell aberto como Administrador.'
}

# ── 1. Python para todos os usuários ─────────────────────────────────────────
Passo 'Procurando Python instalado para todos os usuários (C:\Program Files)'
$pythons = @()
foreach ($base in (@($env:ProgramFiles, ${env:ProgramW6432}) | Where-Object { $_ } | Select-Object -Unique)) {
    $pythons += Get-ChildItem -Path $base -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName 'python.exe' } | Where-Object { Test-Path $_ }
}
$python = $null
$melhor = [version]'0.0'
foreach ($p in $pythons) {
    $v = & $p -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -eq 0 -and [version]$v -ge [version]'3.11' -and [version]$v -gt $melhor) {
        $python = $p; $melhor = [version]$v
    }
}
if (-not $python) {
    Falha (@(
        'Nenhum Python >= 3.11 instalado para todos os usuários em C:\Program Files.',
        'Instale e rode este script de novo. Opções:',
        '  winget install --id Python.Python.3.12 --scope machine',
        '  ou o instalador de python.org > "Customize installation" > marque "Install Python for all users".'
    ) -join "`n")
}
Write-Host "    $python ($melhor)"

# ── 2. .env ──────────────────────────────────────────────────────────────────
Passo 'Conferindo o .env'
$envArq = Join-Path $raiz '.env'
if (-not (Test-Path $envArq)) { Falha "Não achei $envArq (copie de .env.example e preencha)." }
$envVars = @{}
foreach ($linha in Get-Content $envArq -Encoding UTF8) {
    if ($linha -match '^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$') { $envVars[$Matches[1]] = $Matches[2].Trim('"', "'") }
}
if ($envVars['PROSPECCAO_FILTRO'] -ne 'true') { Falha 'O .env precisa de PROSPECCAO_FILTRO=true.' }
if ($envVars['OUTPUT_FORMAT'] -ne 'parquet') {
    Falha 'O .env precisa de OUTPUT_FORMAT=parquet (o serviço roda sem login, sem o Postgres do Docker Desktop).'
}
$envio = if ($envVars['SERVICO_ENVIO_ATIVO'] -eq 'true') { 'LIGADO' } else { 'desligado (SERVICO_ENVIO_ATIVO=false)' }
Write-Host "    Envio ao GestaoRevenda: $envio"
if (-not $envVars['SMTP_HOST'] -or -not $envVars['EMAIL_PARA']) {
    Write-Host '    Aviso: SMTP_HOST/EMAIL_PARA vazios - o serviço roda, mas sem e-mail (só log).' -ForegroundColor Yellow
}

# ── 3. Para o serviço, se já existir (o .venv pode estar em uso) ──────────────
$exe = Join-Path $raiz "servico\$id.exe"
$existente = Get-Service -Name $id -ErrorAction SilentlyContinue
if ($existente) {
    Passo 'Serviço já instalado: parando para atualizar'
    if ($existente.Status -ne 'Stopped') { Stop-Service -Name $id -Force; (Get-Service $id).WaitForStatus('Stopped', '00:02:00') }
}

# ── 4. .venv a partir do Python de C:\Program Files ──────────────────────────
$venv = Join-Path $raiz '.venv'
$cfgVenv = Join-Path $venv 'pyvenv.cfg'
$homeAtual = ''
if (Test-Path $cfgVenv) {
    $l = Get-Content $cfgVenv | Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1
    if ($l) { $homeAtual = ($l -split '=', 2)[1].Trim() }
}
$homeNovo = Split-Path -Parent $python
if ($RecriarVenv -or -not (Test-Path (Join-Path $venv 'Scripts\python.exe')) -or ($homeAtual -ne $homeNovo)) {
    Passo "Recriando o .venv a partir de $homeNovo (antes: $(if ($homeAtual) { $homeAtual } else { 'nenhum' }))"
    if (Test-Path $venv) {
        try { Remove-Item -Recurse -Force $venv }
        catch { Falha "Não consegui apagar o .venv (feche o VS Code/terminais que usam o Python dele): $_" }
    }
    & $python -m venv $venv
    if ($LASTEXITCODE -ne 0) { Falha 'python -m venv falhou' }
    & "$venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip
    & "$venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -e $raiz
    if ($LASTEXITCODE -ne 0) { Falha 'pip install falhou' }
}
else {
    Passo ".venv já vem de $homeNovo - mantido (use -RecriarVenv para recriar)"
    & "$venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -e $raiz
    if ($LASTEXITCODE -ne 0) { Falha 'pip install falhou' }
}
$pyVenv = Join-Path $venv 'Scripts\python.exe'
& $pyVenv -c "import polars, duckdb, pyarrow, dotenv, requests" 2>$null
if ($LASTEXITCODE -ne 0) { Falha 'O .venv não importa as dependências (polars, duckdb, pyarrow...).' }

# ── 5. Pastas ────────────────────────────────────────────────────────────────
$pastasEscrita = @('logs', 'estado', 'saida', 'temp', 'parquet') | ForEach-Object { Join-Path $raiz $_ }
$pastasCriar = $pastasEscrita + @((Join-Path $raiz 'servico'), (Join-Path $raiz 'temp\sys'), (Join-Path $raiz 'logs\winsw'))
foreach ($p in $pastasCriar) {
    New-Item -ItemType Directory -Force -Path $p | Out-Null
}

# ── 6. WinSW ─────────────────────────────────────────────────────────────────
function Confere-Hash([string]$arq) { (Get-FileHash $arq -Algorithm SHA256).Hash -eq $winswSha256 }
if (-not ((Test-Path $exe) -and (Confere-Hash $exe))) {
    Passo "Baixando WinSW $winswVersao"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $tmp = "$exe.download"
    Invoke-WebRequest -UseBasicParsing -Uri $winswUrl -OutFile $tmp
    if (-not (Confere-Hash $tmp)) { Remove-Item $tmp -Force; Falha "Hash do WinSW não confere ($winswUrl)." }
    Move-Item -Force $tmp $exe
}

function Esc([string]$s) { [Security.SecurityElement]::Escape($s) }
$xml = @"
<?xml version="1.0" encoding="utf-8"?>
<!-- Gerado por scripts\instalar_servico.ps1 - edite o script, não este arquivo -->
<service>
  <id>$id</id>
  <name>$(Esc $nome)</name>
  <description>$(Esc $descricao)</description>
  <executable>$(Esc $pyVenv)</executable>
  <arguments>-u "$(Esc (Join-Path $raiz 'servico_prospeccao.py'))"</arguments>
  <workingdirectory>$(Esc $raiz)</workingdirectory>
  <startmode>Automatic</startmode>
  <delayedAutoStart/>
  <onfailure action="restart" delay="1 min"/>
  <onfailure action="restart" delay="5 min"/>
  <onfailure action="restart" delay="15 min"/>
  <resetfailure>1 day</resetfailure>
  <stoptimeout>90 sec</stoptimeout>
  <env name="TMP" value="$(Esc (Join-Path $raiz 'temp\sys'))"/>
  <env name="TEMP" value="$(Esc (Join-Path $raiz 'temp\sys'))"/>
  <env name="PYTHONUTF8" value="1"/>
  <env name="PYTHONIOENCODING" value="utf-8"/>
  <logpath>$(Esc (Join-Path $raiz 'logs\winsw'))</logpath>
  <log mode="roll-by-size">
    <sizeThreshold>10240</sizeThreshold>
    <keepFiles>5</keepFiles>
  </log>
</service>
"@
[IO.File]::WriteAllText((Join-Path $raiz "servico\$id.xml"), $xml, (New-Object Text.UTF8Encoding($true)))

# ── 7. Instala (ou reinstala) ────────────────────────────────────────────────
if ($existente) {
    Passo 'Removendo a instalação anterior'
    & $exe uninstall | Out-Host
    for ($i = 0; $i -lt 30 -and (Get-Service -Name $id -ErrorAction SilentlyContinue); $i++) { Start-Sleep -Seconds 1 }
    if (Get-Service -Name $id -ErrorAction SilentlyContinue) {
        Falha 'O serviço antigo ficou "marcado para exclusão" - feche o services.msc e rode o instalador de novo.'
    }
}
Passo "Instalando o serviço '$nome'"
& $exe install | Out-Host
if ($LASTEXITCODE -ne 0) { Falha 'WinSW install falhou' }

# Conta virtual: o Windows cria NT SERVICE\<id> sozinho, sem senha e sem privilégio de administrador
Passo "Configurando a conta $conta"
& sc.exe config $id obj= $conta | Out-Host
if ($LASTEXITCODE -ne 0) { Falha 'sc config (conta virtual) falhou' }
& sc.exe failureflag $id 1 | Out-Null  # reinicia também se o processo sair com erro (não só se travar)

# ── 8. Permissões ────────────────────────────────────────────────────────────
Passo 'Ajustando permissões (leitura no projeto, escrita só nas pastas de dados)'
function Icacls([string[]]$argumentos) {
    & icacls.exe @argumentos | Out-Null
    if ($LASTEXITCODE -ne 0) { Falha "icacls $($argumentos -join ' ') falhou" }
}
Icacls @($raiz, '/grant', "${conta}:(OI)(CI)RX")
foreach ($p in $pastasEscrita) {
    Icacls @($p, '/grant', "${conta}:(OI)(CI)M", "${Usuario}:(OI)(CI)M", '/T', '/C', '/Q')
}
# .env: só SYSTEM, Administradores, o usuário e o serviço (leitura)
Icacls @($envArq, '/inheritance:r', '/grant:r', '*S-1-5-18:F', '*S-1-5-32-544:F', "${Usuario}:F", "${conta}:R")

# ── 9. Inicia ────────────────────────────────────────────────────────────────
if (-not $SemIniciar) {
    Passo 'Iniciando'
    Start-Service -Name $id
    (Get-Service $id).WaitForStatus('Running', '00:00:30')
}

$svc = Get-CimInstance Win32_Service -Filter "Name='$id'"
Write-Host ''
Write-Host "Serviço:  $($svc.DisplayName) [$id]"
Write-Host "Estado:   $($svc.State)   Início: $($svc.StartMode)$(if ($svc.DelayedAutoStart) { ' (atraso)' })"
Write-Host "Conta:    $($svc.StartName)"
Write-Host "Envio:    $envio"
Write-Host "Logs:     $raiz\logs\servico.log"
Write-Host ''
Write-Host 'Status:   scripts\rodar_agora.ps1 -Status'
Write-Host 'Na hora:  scripts\rodar_agora.ps1'
