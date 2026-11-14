# Prospecção: Receita Federal → GestaoRevenda

Adaptação deste pipeline para gerar listas de prospecção mensais a partir dos dados abertos de CNPJ
e enviá-las como leads ao GestaoRevenda.

## Como funciona

```
rodar_mensal.py
 ├─ 1. baixa o mês mais recente da Receita (sem os arquivos de sócios)
 ├─ 2. carrega no Postgres local já filtrado (filtro.py)
 │      estabelecimentos → situação, CNAE, UF, municípios, início de atividade
 │      empresas / Simples → só os CNPJs básicos que passaram
 ├─ 3. exporta uma linha por estabelecimento (exportar_prospeccao.py)
 │      e marca CNPJ_INEDITO / FILIAL_NOVA / JA_ENVIADO (estado/prospeccao.sqlite)
 ├─ 4. envia só os novos ao GestaoRevenda (enviar_gestaorevenda.py)
 │      POST /api/leads/importar com origem RECEITA_FEDERAL, em lotes, com nova tentativa
 └─ 5. apaga ZIPs/CSVs temporários e grava o log com as contagens
```

| Arquivo / pasta | O que é |
|---|---|
| `.env` | Configuração e credenciais (fora do git) |
| `estado/prospeccao.sqlite` | O que já foi enviado, por execução — **não apague** (sem ele tudo volta a ser "novo"; o GestaoRevenda deduplica, mas reenvia tudo) |
| `saida/prospeccao_AAAA-MM.csv` | Lista do mês (o que seria/foi enviado) |
| `saida/enviados_AAAA-MM_*.csv` | Backup de cada envio, com o resultado de cada linha |
| `logs/rodar_mensal_*.log` | Log de cada execução; a última linha "Resumo" traz as contagens |

## Configuração (`.env`)

| Variável | Valor atual | Observação |
|---|---|---|
| `PROSPECCAO_FILTRO` | `true` | `false` = pipeline original (carga completa) |
| `FILTRO_SITUACAO` | `02` | ativa |
| `FILTRO_CNAES` | 4711-3/02, 4712-1/00, 4711-3/01, 4744-0/99, 4744-0/01, 4530-7/03, 4789-0/04 | aceita com ou sem máscara |
| `FILTRO_UF` | `SC` | |
| `FILTRO_MUNICIPIOS` | 21 códigos da Receita | Brusque, Nova Trento, São João Batista, Canelinha, Gaspar, Guabiruba, Tijucas, Ilhota, Itajaí, Navegantes, Penha, Barra Velha, Itapema, Biguaçu, Blumenau, Indaial, Pomerode, Major Gercino, Angelina, Rancho Queimado, Botuverá. Aceita também o nome como a Receita grafa |
| `FILTRO_PRIMEIRA_EXECUCAO_MESES` | `0` | 1ª execução com envio: todas as ativas da região |
| `FILTRO_INICIO_ATIVIDADE_MESES` | `12` | execuções seguintes |
| `GR_API_URL` | *(preencher)* | base da API do GestaoRevenda **com** `/api` |
| `GR_CLIENT_ID` / `GR_CLIENT_SECRET` | *(preencher)* | GestaoRevenda → Configurações → API Externa, escopo **leads** |
| `GR_LOTE` | `200` | linhas por requisição (máx. 1000) |
| `GR_ENFILEIRAR_CNPJA` | `false` | `true` faz o GestaoRevenda consultar cada CNPJ na cnpja.com (4/min) |

## O que chega no GestaoRevenda

Cada estabelecimento vira um Lead com `fonte = RECEITA_FEDERAL`, `baseLegal = LEGITIMO_INTERESSE`,
os mesmos campos que o enriquecimento da cnpja.com preencheria (abertura, porte, Simples, MEI,
matriz/filial, CNAE principal e secundários, situação, endereço, telefones, e-mail, natureza
jurídica, capital social) e a data da base como data da consulta do CNPJ. Segmento pelo CNAE:

| CNAE | Segmento |
|---|---|
| 4711-3/01, 4711-3/02, 4712-1/00 | MERCADO |
| 4744-x | CONSTRUCAO |
| 4530-7/03 | AUTOPECAS |
| 4789-0/04 | PETSHOP |

Status: `NOVO`; `CLIENTE_ATIVO` (base legal `EXECUCAO_CONTRATO`) se já houver contrato ativo com o
CNPJ. Quem está na lista de bloqueio entra marcado como bloqueado (contagem "bloqueados"). CNPJ que
já é lead (com ou sem máscara) volta como "duplicado" e não é alterado. O score é recalculado no fim
de cada lote.

## Espaço em disco e tempo (medido na 1ª carga, base 2026-10)

| | |
|---|---|
| Download | 6,6 GB (sem sócios) — 14 arquivos |
| Pico de disco | ~17 GB medidos (pasta `temp/` + cópia UTF-8 na pasta temporária do sistema). **Reserve 25 GB livres** |
| Banco do pipeline ao final | 13 MB (7.174 estabelecimentos, 6.779 empresas) |
| Tempo da carga | **27,5 min** (download ~2,2 MB/s por arquivo, 4 em paralelo) |
| Exportação | ~1 s |
| Envio | depende do GestaoRevenda; estimativa 7.174 linhas em 36 lotes de 200 |

O volume da 1ª execução (janela 0 meses) foi 7.174 estabelecimentos; com a janela de 12 meses,
986 deles se qualificariam — é a ordem de grandeza esperada por mês daqui pra frente (só os novos
são enviados).

## Agendamento

A Receita publica uma vez por mês, sem data fixa. Rodar **toda segunda às 3h** é barato: se não
houver mês novo, o pipeline só lista o servidor, vê que o mês já foi carregado e não envia nada
novo.

**Windows (Agendador de Tarefas)** — em um PowerShell como o seu usuário (Docker Desktop precisa
estar rodando):

```powershell
schtasks /Create /TN "Prospeccao Receita GestaoRevenda" /SC WEEKLY /D MON /ST 03:00 /RL LIMITED `
  /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\GestaoPonte\beemore-pipeline\scripts\rodar_mensal.ps1"
```

Conferir/rodar na hora: `schtasks /Query /TN "Prospeccao Receita GestaoRevenda" /V` e
`schtasks /Run /TN "Prospeccao Receita GestaoRevenda"`. O computador precisa estar ligado no
horário (ou marque "Executar o mais cedo possível após uma inicialização agendada ter sido
perdida" nas propriedades da tarefa).

**Linux (cron)** — `crontab -e`:

```cron
0 3 * * 1  /caminho/beemore-pipeline/scripts/rodar_mensal.sh >> /caminho/beemore-pipeline/logs/cron.log 2>&1
```

## Roteiro de teste

Pré-requisitos: Docker rodando, `.venv` criado (`python -m venv .venv` e
`.venv\Scripts\python -m pip install -e .`), `.env` configurado.

### A. Pipeline sem envio (seguro — não toca no GestaoRevenda)

1. `docker compose up -d postgres`
2. `.venv\Scripts\python rodar_mensal.py --sem-envio`
   - Esperado: termina com `Resumo (SEM_ENVIO)`, `exportadas` ≈ 7.174 (base 2026-10), CSV em
     `saida\prospeccao_2026-10.csv`.
3. Abra o CSV no Excel e confira 5 a 10 CNPJs no cartão CNPJ da Receita: razão social, endereço,
   telefone, CNAE, situação.
4. Confira que `temp\` está vazia ao final.

### B. Backend do GestaoRevenda

5. Publique o backend com as alterações (a migration `20261115000000_leads_fonte_receita_federal`
   roda sozinha no start do container) e o frontend (rótulo "Receita Federal").
6. **Importação CSV antiga** (regressão): importe um CSV pela tela de Leads como sempre. Esperado:
   mesmo resultado de antes (fonte cnpj.chat, consulta CNPJ enfileirada).
7. Em Configurações → API Externa, crie um Client com o escopo **leads**; coloque
   `GR_API_URL`, `GR_CLIENT_ID` e `GR_CLIENT_SECRET` no `.env`.

### C. Primeiro envio real, pequeno

8. `.venv\Scripts\python rodar_mensal.py --limite 20`
   - Esperado no log: `Lote 1: 20 enviadas — N importadas (B bloqueadas), D duplicadas, 0 erros`
     e `Resumo (OK)`.
9. No GestaoRevenda, filtre Leads pela fonte "Receita Federal" e confira em 3 leads: segmento pelo
   CNAE, porte/Simples/MEI/matriz-filial preenchidos, observações vazias, score calculado, status
   NOVO, sem "Na fila" de consulta CNPJ.
10. Rode de novo `rodar_mensal.py --limite 20`: deve enviar **outros** 20 (os primeiros já são
    `JA_ENVIADO`).
11. Abra `saida\enviados_2026-10_*.csv`: a coluna `resultado` bate com o log.

### D. Envio completo

12. `.venv\Scripts\python rodar_mensal.py` — envia o restante (~7.134). Acompanhe pelo log.
13. Rode de novo sem nada novo: `selecionadas: 0`, `envio: {"enviadas": 0}`.

### E. Agendamento

14. Crie a tarefa (seção Agendamento), rode com `schtasks /Run` e confira o log novo em `logs\`.

## Problemas comuns

| Sintoma | Causa / o que fazer |
|---|---|
| `Credenciais recusadas` | Client ID/Secret errados ou revogados |
| `não tem o escopo 'leads'` | Recrie o Client com o escopo leads |
| `Resposta sem 'resultados'` / `Lote recusado (400)` com `origem should not exist` | Backend do GestaoRevenda ainda sem a importação com origem — publique o backend |
| `Município não encontrado na tabela da Receita` | Nome grafado diferente da Receita; use o código (ver log da carga) |
| `Another run is using TEMP_DIR` | Já há uma execução rodando |
| Falha no meio do envio | Rode de novo: o que foi aceito está no estado e não é reenviado como novo |
