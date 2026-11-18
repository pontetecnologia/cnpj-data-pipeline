#Requires -Version 5.1
<#
.SYNOPSIS
    Instala (ou atualiza) o serviço Windows "Ponte - Prospecção Receita" e o ícone da tela.

.DESCRIPTION
    O jeito simples: dois cliques em Instalar.cmd (na pasta do projeto). Ele pede a permissão de
    administrador e roda este script com -Simples (mensagens em janelas, detalhes em
    logs\instalacao.log). Pela linha de comando, num PowerShell COMO ADMINISTRADOR:
        powershell -ExecutionPolicy Bypass -File scripts\instalar_servico.ps1

    1. Procura o Python instalado para todos os usuários (C:\Program Files\Python3xx, >= 3.11). Se
       não houver, instala o Python 3.12 para todos os usuários: pelo winget (que confere o hash
       do instalador) ou, sem winget, pelo instalador oficial do python.org, só se a assinatura
       digital for válida e da Python Software Foundation.
    2. Confere o .env (só GR_API_URL, GR_CLIENT_ID e GR_CLIENT_SECRET; o resto vem da tela do
       GestaoRevenda, Configurações → Prospecção Receita). Sem .env, cria um a partir do
       .env.example: tudo funciona, menos o envio ao GestaoRevenda, até alguém preencher.
    3. Recria o .venv a partir desse Python, se preciso, e instala as dependências.
    4. Baixa o WinSW 2.12.0 (hash SHA-256 fixo) e gera servico\PonteProspeccaoReceita.xml.
    5. Instala o serviço com a conta virtual NT SERVICE\PonteProspeccaoReceita (sem senha, sem
       administrador), com leitura na pasta do projeto e escrita só em logs, estado, saida, temp e parquet.
    6. Restringe o .env (credenciais) ao SYSTEM, Administradores, ao seu usuário e ao serviço.
    7. Cria o ícone "Prospecção Receita" na área de trabalho de todos os usuários (abre a tela em
       http://127.0.0.1:8765, servida pelo próprio serviço).
    8. Inicia o serviço e confere que a tela responde.

    Pode rodar de novo para atualizar: para o serviço, reinstala e inicia.

.PARAMETER Usuario
    Conta que também roda o rodar_agora.ps1 (recebe escrita nas mesmas pastas). Padrão: quem executa.

.PARAMETER RecriarVenv
    Recria o .venv mesmo se ele já vier do Python de C:\Program Files.

.PARAMETER SemIniciar
    Instala sem iniciar o serviço.

.PARAMETER Simples
    Mensagens em janelas, em linguagem simples (usado pelo Instalar.cmd).
#>
param(
    [string]$Usuario = [Security.Principal.WindowsIdentity]::GetCurrent().Name,
    [switch]$RecriarVenv,
    [switch]$SemIniciar,
    [switch]$Simples
)

$ErrorActionPreference = 'Stop'
$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz

$id = 'PonteProspeccaoReceita'
$nome = 'Ponte - Prospecção Receita'
$descricao = 'Verifica todo dia se a Receita Federal publicou mês novo de CNPJ, mantém a base rápida e atende a tela local da prospecção (cnpj-pipeline).'
$conta = "NT SERVICE\$id"
$porta = 8765
$urlTela = "http://127.0.0.1:$porta/"
$winswVersao = 'v2.12.0'
$winswUrl = "https://github.com/winsw/winsw/releases/download/$winswVersao/WinSW-x64.exe"
# WinSW não publica hash nem assina o binário; este é o SHA-256 do WinSW-x64.exe v2.12.0 baixado
# do release oficial em 2026-11-15. Se o download não bater, o instalador para.
$winswSha256 = '05B82D46AD331CC16BDC00DE5C6332C1EF818DF8CEEFCD49C726553209B3A0DA'
# Python instalado quando falta (último 3.12 com instalador oficial para Windows)
$pythonVersao = '3.12.10'
$pythonWinget = 'Python.Python.3.12'
$pythonUrl = "https://www.python.org/ftp/python/$pythonVersao/python-$pythonVersao-amd64.exe"
$logInstalacao = Join-Path $raiz 'logs\instalacao.log'

New-Item -ItemType Directory -Force -Path (Join-Path $raiz 'logs') | Out-Null
if ($Simples) { Start-Transcript -Path $logInstalacao -Append | Out-Null }

function Passo([string]$texto) { Write-Host "==> $texto" -ForegroundColor Cyan }
function Aviso([string]$texto) { Write-Host "    Aviso: $texto" -ForegroundColor Yellow }

function Janela([string]$texto, [string]$icone = 'Information') {
    if (-not $Simples) { return }
    Add-Type -AssemblyName PresentationFramework
    [void][System.Windows.MessageBox]::Show($texto, 'Prospecção Receita', 'OK', $icone)
}

function Falha([string]$texto, [string]$oQueFazer = '') {
    Write-Host "ERRO: $texto" -ForegroundColor Red
    if ($oQueFazer) { Write-Host "O que fazer: $oQueFazer" -ForegroundColor Red }
    if ($Simples) {
        $corpo = "Não deu para terminar a instalação.`n`n$texto"
        if ($oQueFazer) { $corpo += "`n`nO que fazer: $oQueFazer" }
        $corpo += "`n`nSe precisar de ajuda, mande ao suporte o arquivo:`n$logInstalacao"
        try { Stop-Transcript | Out-Null } catch { }
        Janela $corpo 'Error'
    }
    exit 1
}

trap {
    Falha "Erro inesperado: $($_.Exception.Message)" 'Dê dois cliques em Instalar.cmd de novo. Se repetir, avise o suporte.'
}

$deNovo = 'Dê dois cliques em Instalar.cmd de novo.'
$deNovoMin = 'dê dois cliques em Instalar.cmd de novo.'
$semInternet = "Confira se o computador está com internet e $deNovoMin"

# ── 0. Administrador ─────────────────────────────────────────────────────────
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Falha 'A instalação precisa de permissão de administrador.' 'Dê dois cliques em Instalar.cmd e clique em "Sim" quando o Windows perguntar.'
}
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# ── 1. Python para todos os usuários (instala se faltar) ─────────────────────
function Procurar-Python {
    $achado = $null
    $melhor = [version]'0.0'
    foreach ($base in (@($env:ProgramFiles, ${env:ProgramW6432}) | Where-Object { $_ } | Select-Object -Unique)) {
        $candidatos = Get-ChildItem -Path $base -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
            ForEach-Object { Join-Path $_.FullName 'python.exe' } | Where-Object { Test-Path $_ }
        foreach ($p in $candidatos) {
            $v = & $p -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and [version]$v -ge [version]'3.11' -and [version]$v -gt $melhor) {
                $achado = $p; $melhor = [version]$v
            }
        }
    }
    return $achado
}

function Instalar-Python {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($winget) {
        Passo "Instalando o Python $pythonVersao para todos os usuários (winget)"
        & $winget.Source install --id $pythonWinget --exact --scope machine --silent `
            --accept-package-agreements --accept-source-agreements --disable-interactivity | Out-Host
        if (Procurar-Python) { return }
        Aviso 'o winget não instalou o Python; tentando pelo instalador oficial do python.org'
    }
    Passo "Baixando o instalador oficial do Python $pythonVersao (python.org)"
    $instalador = Join-Path $env:TEMP "python-$pythonVersao-amd64.exe"
    try { Invoke-WebRequest -UseBasicParsing -Uri $pythonUrl -OutFile $instalador }
    catch { Falha "Não consegui baixar o Python ($pythonUrl)." $semInternet }
    $assinatura = Get-AuthenticodeSignature $instalador
    if ($assinatura.Status -ne 'Valid' -or $assinatura.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
        Remove-Item $instalador -Force -ErrorAction SilentlyContinue
        Falha "O instalador do Python baixado não tem a assinatura digital da Python Software Foundation (status: $($assinatura.Status))." 'Não instale nada; avise o suporte.'
    }
    Passo "Instalando o Python $pythonVersao para todos os usuários (leva alguns minutos)"
    $proc = Start-Process -FilePath $instalador -Wait -PassThru -ArgumentList @(
        '/quiet', 'InstallAllUsers=1', 'PrependPath=0', 'Include_test=0', 'Include_doc=0',
        'Include_launcher=0', 'Include_tcltk=0', 'Shortcuts=0'
    )
    Remove-Item $instalador -Force -ErrorAction SilentlyContinue
    if ($proc.ExitCode -notin 0, 3010) {
        Falha "A instalação do Python terminou com erro (código $($proc.ExitCode))." "Reinicie o computador e $deNovoMin"
    }
}

Passo 'Procurando Python instalado para todos os usuários (C:\Program Files)'
$python = Procurar-Python
if (-not $python) {
    Instalar-Python
    $python = Procurar-Python
    if (-not $python) {
        Falha 'O Python foi instalado, mas não apareceu em C:\Program Files.' "Reinicie o computador e $deNovoMin"
    }
}
$melhor = & $python -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
Write-Host "    $python ($melhor)"

# ── 2. .env ──────────────────────────────────────────────────────────────────
Passo 'Conferindo o .env'
$envArq = Join-Path $raiz '.env'
if (-not (Test-Path $envArq)) {
    Copy-Item (Join-Path $raiz '.env.example') $envArq
    Aviso '.env não existia: criado a partir do .env.example (GR_* vazios).'
}
$envVars = @{}
foreach ($linha in Get-Content $envArq -Encoding UTF8) {
    if ($linha -match '^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$') { $envVars[$Matches[1]] = $Matches[2].Trim('"', "'") }
}
if ($envVars['OUTPUT_FORMAT'] -and $envVars['OUTPUT_FORMAT'] -ne 'parquet') {
    Falha 'O arquivo .env tem OUTPUT_FORMAT diferente de parquet: o serviço roda sem login, sem o Postgres do Docker Desktop.' 'Peça ao suporte para tirar OUTPUT_FORMAT do .env.'
}
$faltando = @('GR_API_URL', 'GR_CLIENT_ID', 'GR_CLIENT_SECRET') | Where-Object { -not $envVars[$_] }
if ($faltando) {
    $envio = "desligado ($($faltando -join ', ') vazio no .env)"
    Aviso "$($faltando -join ', ') vazio - a tela funciona (ver resultado, Excel), mas sem envio ao GestaoRevenda,"
    Aviso 'e o serviço roda com os valores padrão, sem buscar a configuração nem mandar o resultado.'
} else {
    $envio = 'conforme a tela do GestaoRevenda (Configurações → Prospecção Receita)'
}
$antigas = @($envVars.Keys | Where-Object { $_ -match '^(SMTP_|EMAIL_|FILTRO_|SERVICO_)' })
if ($antigas) {
    Aviso "o .env ainda tem $($antigas -join ', ') - não são mais usadas (agora estão na tela)."
}
Write-Host "    Envio ao GestaoRevenda: $envio"

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
$pipFalhou = 'Não consegui instalar os componentes do programa (pip).'
if ($RecriarVenv -or -not (Test-Path (Join-Path $venv 'Scripts\python.exe')) -or ($homeAtual -ne $homeNovo)) {
    Passo "Recriando o .venv a partir de $homeNovo (antes: $(if ($homeAtual) { $homeAtual } else { 'nenhum' }))"
    if (Test-Path $venv) {
        try { Remove-Item -Recurse -Force $venv }
        catch { Falha "Não consegui apagar a pasta .venv: $($_.Exception.Message)" "Feche o VS Code e outros programas que usam o projeto e $deNovoMin" }
    }
    & $python -m venv $venv
    if ($LASTEXITCODE -ne 0) { Falha 'Não consegui criar o ambiente do Python (.venv).' $deNovo }
    & "$venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip
    & "$venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -e $raiz
    if ($LASTEXITCODE -ne 0) { Falha $pipFalhou $semInternet }
}
else {
    Passo ".venv já vem de $homeNovo - mantido (use -RecriarVenv para recriar)"
    & "$venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -e $raiz
    if ($LASTEXITCODE -ne 0) { Falha $pipFalhou $semInternet }
}
$pyVenv = Join-Path $venv 'Scripts\python.exe'
& $pyVenv -c "import polars, duckdb, pyarrow, dotenv, requests, xlsxwriter" 2>$null
if ($LASTEXITCODE -ne 0) { Falha 'O ambiente do Python ficou sem algum componente (polars, duckdb, pyarrow, xlsxwriter...).' "Use a opção -RecriarVenv ou avise o suporte." }

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
    $tmp = "$exe.download"
    try { Invoke-WebRequest -UseBasicParsing -Uri $winswUrl -OutFile $tmp }
    catch { Falha "Não consegui baixar o WinSW ($winswUrl)." $semInternet }
    if (-not (Confere-Hash $tmp)) { Remove-Item $tmp -Force; Falha "O arquivo do WinSW baixado não confere ($winswUrl)." 'Não instale nada; avise o suporte.' }
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
        Falha 'O serviço antigo ficou "marcado para exclusão" no Windows.' "Feche a janela Serviços (services.msc), se estiver aberta, ou reinicie o computador, e $deNovoMin"
    }
}
Passo "Instalando o serviço '$nome'"
& $exe install | Out-Host
if ($LASTEXITCODE -ne 0) { Falha 'O Windows não aceitou instalar o serviço (WinSW install).' $deNovo }

# Conta virtual: o Windows cria NT SERVICE\<id> sozinho, sem senha e sem privilégio de administrador
Passo "Configurando a conta $conta"
& sc.exe config $id obj= $conta | Out-Host
if ($LASTEXITCODE -ne 0) { Falha 'Não consegui configurar a conta do serviço (sc config).' $deNovo }
& sc.exe failureflag $id 1 | Out-Null  # reinicia também se o processo sair com erro (não só se travar)

# ── 8. Permissões ────────────────────────────────────────────────────────────
Passo 'Ajustando permissões (leitura no projeto, escrita só nas pastas de dados)'
function Icacls([string[]]$argumentos) {
    & icacls.exe @argumentos | Out-Null
    if ($LASTEXITCODE -ne 0) { Falha "Não consegui ajustar as permissões (icacls $($argumentos -join ' '))." $deNovo }
}
Icacls @($raiz, '/grant', "${conta}:(OI)(CI)RX")
foreach ($p in $pastasEscrita) {
    Icacls @($p, '/grant', "${conta}:(OI)(CI)M", "${Usuario}:(OI)(CI)M", '/T', '/C', '/Q')
}
# .env: só SYSTEM, Administradores, o usuário e o serviço (leitura)
Icacls @($envArq, '/inheritance:r', '/grant:r', '*S-1-5-18:F', '*S-1-5-32-544:F', "${Usuario}:F", "${conta}:R")

# ── 9. Ícone na área de trabalho (todos os usuários) ─────────────────────────
Passo 'Criando o ícone "Prospecção Receita" na área de trabalho'
$areaDeTrabalho = [Environment]::GetFolderPath('CommonDesktopDirectory')
$atalho = Join-Path $areaDeTrabalho 'Prospecção Receita.url'
$icone = Join-Path $raiz 'tela\icone.ico'
$conteudoAtalho = "[InternetShortcut]`r`nURL=$urlTela`r`nIconFile=$icone`r`nIconIndex=0`r`n"
# Atalho de internet (.url) é lido no código de página do Windows (ANSI)
[IO.File]::WriteAllText($atalho, $conteudoAtalho, [Text.Encoding]::Default)

# ── 10. Inicia e confere a tela ──────────────────────────────────────────────
$telaOk = $false
if (-not $SemIniciar) {
    $ocupada = Get-NetTCPConnection -LocalPort $porta -State Listen -ErrorAction SilentlyContinue
    if ($ocupada) {
        $dono = (Get-Process -Id $ocupada[0].OwningProcess -ErrorAction SilentlyContinue).ProcessName
        Aviso "a porta $porta já está em uso por '$dono' - a tela não vai abrir enquanto esse programa usar a porta."
    }
    Passo 'Iniciando'
    Start-Service -Name $id
    (Get-Service $id).WaitForStatus('Running', '00:00:30')
    Passo "Conferindo a tela ($urlTela)"
    for ($i = 0; $i -lt 45 -and -not $telaOk; $i++) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -Uri "$($urlTela)api/estado" -TimeoutSec 5
            $telaOk = $r.StatusCode -eq 200
        } catch { Start-Sleep -Seconds 2 }
    }
    if (-not $telaOk) { Aviso "a tela ainda não respondeu em $urlTela (ver logs\servico.log)." }
}

$svc = Get-CimInstance Win32_Service -Filter "Name='$id'"
Write-Host ''
Write-Host "Serviço:  $($svc.DisplayName) [$id]"
Write-Host "Estado:   $($svc.State)   Início: $($svc.StartMode)$(if ($svc.DelayedAutoStart) { ' (atraso)' })"
Write-Host "Conta:    $($svc.StartName)"
Write-Host "Envio:    $envio"
Write-Host "Tela:     $urlTela (ícone '$atalho')$(if (-not $SemIniciar) { if ($telaOk) { ' - respondendo' } else { ' - SEM RESPOSTA' } })"
Write-Host "Logs:     $raiz\logs\servico.log"
Write-Host ''
Write-Host 'Status:   scripts\rodar_agora.ps1 -Status'
Write-Host 'Na hora:  scripts\rodar_agora.ps1'

if ($Simples) {
    try { Stop-Transcript | Out-Null } catch { }
    $texto = "Instalação concluída.`n`nPara usar: dois cliques no ícone ""Prospecção Receita"" na área de trabalho."
    if (-not $telaOk) {
        $texto = "Instalação concluída, mas a tela ainda não respondeu.`n`nEspere um minuto e abra o ícone ""Prospecção Receita"" na área de trabalho. Se não abrir, reinicie o computador."
    }
    if ($faltando) {
        $texto += "`n`nO envio ao GestaoRevenda ainda não está configurado neste computador: dá para ver o resultado e baixar em Excel, mas não enviar. Peça ao suporte para preencher o arquivo .env."
    }
    $texto += "`n`nNa primeira vez, clique em ""Atualizar base agora"" na tela (leva cerca de 30 minutos)."
    Janela $texto ($(if ($telaOk) { 'Information' } else { 'Warning' }))
}
