@echo off
rem Instalador da Prospeccao Receita: de dois cliques neste arquivo.
rem Pede permissao de administrador e roda scripts\instalar_servico.ps1 -Simples
rem (instala o Python se faltar, o servico e o icone na area de trabalho).
setlocal
title Prospeccao Receita - instalacao
rem Caminho deste arquivo numa variavel: o PowerShell le sem problemas de aspas no caminho
set "INSTALAR_CMD=%~f0"

net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "try { Start-Process -FilePath $env:ComSpec -ArgumentList '/c', ('\"' + $env:INSTALAR_CMD + '\"') -Verb RunAs -ErrorAction Stop } catch { Add-Type -AssemblyName PresentationFramework; [void][System.Windows.MessageBox]::Show('A instalacao precisa de permissao de administrador. De dois cliques em Instalar.cmd de novo e clique em Sim quando o Windows perguntar.', 'Prospeccao Receita', 'OK', 'Warning') }"
    exit /b
)

echo Instalando a Prospeccao Receita. Pode levar alguns minutos; nao feche esta janela.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\instalar_servico.ps1" -Simples
exit /b %errorlevel%
