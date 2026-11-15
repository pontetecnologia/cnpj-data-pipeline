# Prospecção: Receita Federal → GestaoRevenda

Adaptação deste pipeline para gerar listas de prospecção mensais a partir dos dados abertos de CNPJ
e enviá-las como leads ao GestaoRevenda.

## Como funciona

O serviço Windows **"Ponte - Prospecção Receita"** (`servico_prospeccao.py`, rodando pelo WinSW) verifica
todo dia às 03:00 se a Receita publicou mês novo. Se o mês mais recente ainda não foi processado, roda o
`rodar_mensal.py` para ele e manda um e-mail com as contagens; se já foi, não faz nada.

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
| `logs/rodar_mensal_*.log` | Log de cada execução; a última linha "Resumo" traz as contagens. Apagados após `SERVICO_LOGS_MANTER_DIAS` |
| `logs/servico.log` | Log do serviço (verificações diárias), rotação 10 MB × 10. `servico_manual.log` = `rodar_agora` |
| `logs/winsw/` | Log do WinSW (o "embrulho" do serviço) |
| `parquet/AAAA-MM/` | Dados filtrados do mês (substitui o Postgres; uma pasta por mês) |
| `servico/` | WinSW e o XML do serviço, gerados pelo instalador (fora do git) |

## Configuração (`.env`)

| Variável | Valor atual | Observação |
|---|---|---|
| `PROSPECCAO_FILTRO` | `true` | `false` = pipeline original (carga completa) |
| `OUTPUT_FORMAT` | `parquet` | O serviço roda sem login, sem o Postgres do Docker Desktop. Obrigatório para o instalador |
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
| `SERVICO_ENVIO_ATIVO` | `false` | **Só envia com `true`** (depois do deploy do backend). `false` ou `GR_*` vazios: roda tudo como `--sem-envio` e o e-mail avisa "envio desligado". Vale também para o `rodar_mensal.py` direto |
| `SERVICO_HORARIO` | `03:00` | Verificação diária. Desligado nesse horário: roda `SERVICO_ATRASO_INICIAL_MIN` (10) depois de ligar |
| `SERVICO_DISCO_MIN_GB` | `25` | Sem esse espaço livre (descontado o que já está em `temp/`): pula, registra e avisa |
| `SERVICO_TENTATIVAS_DIA` / `SERVICO_INTERVALO_TENTATIVA_MIN` | `3` / `60` | Receita fora do ar ou outra execução rodando |
| `SMTP_HOST`, `SMTP_PORTA`, `SMTP_SEGURANCA`, `SMTP_USUARIO`, `SMTP_SENHA`, `EMAIL_DE`, `EMAIL_PARA` | *(preencher)* | Aviso por e-mail. Vazio = só log. Ver "E-mail" abaixo |

O serviço relê o `.env` a cada ciclo: mudar `SERVICO_ENVIO_ATIVO`, o horário ou o e-mail não exige
reiniciar o serviço.

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

## Serviço Windows

| | |
|---|---|
| Nome | **Ponte - Prospecção Receita** (`PonteProspeccaoReceita`) |
| Wrapper | [WinSW](https://github.com/winsw/winsw) 2.12.0, baixado pelo instalador com SHA-256 fixo |
| Conta | Virtual `NT SERVICE\PonteProspeccaoReceita`: sem senha, sem administrador. Lê a pasta do projeto e escreve só em `logs`, `estado`, `saida`, `temp` e `parquet` |
| Início | Automático (atrasado), sem login e sem janela. Reinicia se cair (1, 5 e 15 min) |
| Python | O do `.venv`, criado a partir do Python **para todos os usuários** em `C:\Program Files` |

**O que ele faz todo dia:**

1. A partir das 03:00, ou 10 min depois de ligar se o computador estava desligado, lista os meses da Receita.
2. Se já existe execução `OK` para o mês mais recente, não faz nada. Enquanto o envio estiver desligado, `SEM_ENVIO` também conta. Ao ligar o envio, o mês volta a ficar pendente e é enviado na verificação seguinte.
3. Se o mês está pendente, confere o disco e roda o `rodar_mensal.py --mes <mês>` num subprocesso.
4. Manda o e-mail: contagens (lidas, filtradas, novas, enviadas, importadas, duplicadas, bloqueadas, recusadas) ou o erro com o fim do log.

Os dias sem mês novo não geram e-mail.

**Uma execução por vez:** a trava `estado/.prospeccao.lock` cobre carga, exportação e envio. O serviço e
o `rodar_agora` usam a mesma trava; quem chega depois sai sem fazer nada.

**Desligado no meio:** a próxima execução retoma de onde parou.

| Onde parou | O que acontece |
|---|---|
| Download | Retoma o `.part` |
| Carga | Pula as tabelas Parquet já gravadas (a gravação é atômica) |
| Exportação | Refeita (~1 s) |
| Envio | Os lotes aceitos já estão no estado e não são reenviados. O lote que estava no ar, se for de novo, volta `DUPLICADO` do GestaoRevenda |

A execução que ficou pela metade aparece como `INTERROMPIDA`. Os temporários só são apagados quando a execução termina bem.

**`--limite`** gera status `PARCIAL`. Esse status não conta como mês concluído nem como "primeira execução", então a janela continua em 0 meses e o restante vai na execução seguinte.

### Instalar

1. Python **para todos os usuários** (o instalador exige e recria o `.venv` a partir dele):
   `winget install --id Python.Python.3.12 --scope machine`. Ou use o instalador de python.org: "Customize installation" → "Install Python for all users".
2. Feche o VS Code e os terminais que usam o `.venv`, porque ele é recriado.
3. PowerShell **como Administrador**, na pasta do projeto:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\instalar_servico.ps1
   ```
   Pode rodar de novo para atualizar (depois de um `git pull`, por exemplo).
4. Remover: `scripts\desinstalar_servico.ps1` (como Administrador). Mantém `estado\`, `logs\`, `saida\`, `parquet\` e o `.env`.

O instalador deixa o `.env` legível só por SYSTEM, Administradores, o seu usuário e o serviço.

### Rodar na hora e acompanhar

Não precisa de administrador:

```powershell
scripts\rodar_agora.ps1                 # igual à verificação diária (respeita SERVICO_ENVIO_ATIVO)
scripts\rodar_agora.ps1 -SemEnvio       # gera a lista sem enviar
scripts\rodar_agora.ps1 -Limite 20      # envia só 20
scripts\rodar_agora.ps1 -Forcar         # roda mesmo se o mês já foi processado
scripts\rodar_agora.ps1 -Recarregar     # baixa e carrega o mês do zero
scripts\rodar_agora.ps1 -Status         # serviço, últimas verificações e execuções, próxima verificação
scripts\rodar_agora.ps1 -TestarEmail
```

### E-mail

| Provedor | `SMTP_HOST` | Porta / segurança | Senha |
|---|---|---|---|
| Gmail | `smtp.gmail.com` | 587 / `starttls` | Senha de app (exige verificação em 2 etapas) |
| Microsoft 365 | `smtp.office365.com` | 587 / `starttls` | Exige "SMTP autenticado" liberado na caixa. A Microsoft vem desligando a autenticação básica no SMTP do Exchange Online; se o login falhar com 535, use outra conta/relay |

`EMAIL_PARA` aceita vários destinatários separados por vírgula. Uma falha de e-mail vai para o log e
nunca derruba a execução.

## Roteiro de teste

Pré-requisitos: Python para todos os usuários instalado, `.env` com `OUTPUT_FORMAT=parquet`,
`SERVICO_ENVIO_ATIVO=false` e o e-mail preenchido.

### A. Sem tocar no GestaoRevenda

1. `scripts\instalar_servico.ps1 -SemIniciar` (como Administrador). Ele recria o `.venv` e instala o
   serviço parado. Confira a saída: conta `NT SERVICE\PonteProspeccaoReceita`, início automático (atraso).
2. `scripts\rodar_agora.ps1 -TestarEmail`. Esperado: chega o e-mail de teste.
3. `scripts\rodar_agora.ps1 -Forcar` (o mês 2026-10 já tem uma execução sem envio, por isso o `-Forcar`).
   - Esperado: baixa ~6,6 GB e carrega em Parquet (~30 min), com `Resumo (SEM_ENVIO)` e `exportadas` ≈ 7.174.
   - Chega o e-mail "OK (envio desligado)" com as contagens.
   - `parquet\2026-10\` foi criado e `temp\` termina vazia.
4. Retomada: rode `scripts\rodar_agora.ps1 -Recarregar` e aperte Ctrl+C durante o download. Rode de novo
   `scripts\rodar_agora.ps1 -Forcar`.
   - Esperado: o log diz que a anterior foi marcada `INTERROMPIDA` e o download retoma de onde parou, sem recomeçar do zero.
5. Uma por vez: com o passo 4 ainda rodando, abra outro terminal e rode `scripts\rodar_agora.ps1 -Forcar`.
   - Esperado: "Já há uma execução da prospecção em andamento", e o resultado é `OCUPADO`.
6. Disco: no `.env`, `SERVICO_DISCO_MIN_GB=9999`, e rode `scripts\rodar_agora.ps1 -Forcar`.
   - Esperado: `DISCO_INSUFICIENTE` e o e-mail "PULADA". Volte para 25.

### B. Serviço

7. Para ver a verificação sem esperar até as 03:00, coloque no `.env` `SERVICO_HORARIO` alguns minutos
   antes de agora e `SERVICO_ATRASO_INICIAL_MIN=1`. Inicie o serviço: `Start-Service PonteProspeccaoReceita`.
   - Esperado em `logs\servico.log`: "iniciado" e, depois de ~1 min, "2026-10 — já processado, nada a fazer" (ou a execução, se a Receita publicou 2026-11).
   - `scripts\rodar_agora.ps1 -Status` mostra a verificação e a próxima para amanhã.
8. Reinicie o Windows **sem fazer login**. Espere ~15 min e entre.
   - Esperado: `services.msc` mostra o serviço "Em execução" e `servico.log` registra o início antes do login.
9. Com uma execução rodando pelo serviço (use `-Recarregar` no passo 7, ou espere um mês novo), pare o
   serviço: `Stop-Service PonteProspeccaoReceita`. Inicie de novo.
   - Esperado: a anterior fica `INTERROMPIDA` e a nova retoma.
10. Volte `SERVICO_HORARIO=03:00` e `SERVICO_ATRASO_INICIAL_MIN=10`.

### C. Backend do GestaoRevenda e envio real

11. Publique o backend e o frontend da branch `prospeccao-receita-federal` do GestaoRevenda seguindo
    `docs/integracoes/normalizacao-cnpj.md` daquele repositório: relatório prévio (só leitura), backup do
    banco, deploy (as migrations `20261115000000_leads_fonte_receita_federal` e
    `20261115010000_normalizar_cnpj` rodam no start do container), conferência e, se preciso, volta atrás.
12. **Importação CSV antiga** (regressão): importe um CSV pela tela de Leads como sempre. Esperado: mesmo
    resultado de antes (fonte cnpj.chat, consulta CNPJ enfileirada).
13. Em Configurações → API Externa, crie um Client com o escopo **leads**. Preencha `GR_API_URL`,
    `GR_CLIENT_ID` e `GR_CLIENT_SECRET` e mude `SERVICO_ENVIO_ATIVO=true` no `.env`.
14. `scripts\rodar_agora.ps1 -Limite 20`.
    - Esperado: `Lote 1: 20 enviadas — N importadas …`, status `PARCIAL` e o e-mail "OK PARCIAL".
    - No GestaoRevenda, filtre Leads pela fonte "Receita Federal" e confira 3 leads: segmento pelo CNAE,
      porte/Simples/MEI/matriz-filial, score, status NOVO, sem "Na fila" de consulta CNPJ.
15. Desligar no meio do envio: rode `scripts\rodar_agora.ps1` (envia o restante, ~7.154 em ~36 lotes) e
    feche o terminal (ou desligue o computador) depois de alguns lotes. Rode de novo `scripts\rodar_agora.ps1`.
    - Esperado: a primeira vira `INTERROMPIDA` e a segunda envia só o que faltou.
    - No fim, `Status: OK`, a janela ainda em 0 meses e, no GestaoRevenda, nenhum CNPJ em dobro.
    - Nos `saida\enviados_2026-10_*.csv`, a soma das linhas bate com o total.
16. Na verificação seguinte do serviço (ou `scripts\rodar_agora.ps1`): `SEM_NOVIDADE`, sem e-mail.

### D. Remoção

17. `scripts\desinstalar_servico.ps1` (como Administrador). Esperado: o serviço some do `services.msc`, e
    `estado\`, `logs\`, `saida\` e `parquet\` continuam no lugar. Reinstalar continua de onde parou.

## Problemas comuns

| Sintoma | Causa / o que fazer |
|---|---|
| `Credenciais recusadas` | Client ID/Secret errados ou revogados |
| `não tem o escopo 'leads'` | Recrie o Client com o escopo leads |
| `Resposta sem 'resultados'` / `Lote recusado (400)` com `origem should not exist` | Backend do GestaoRevenda ainda sem a importação com origem — publique o backend |
| `Município não encontrado na tabela da Receita` | Nome grafado diferente da Receita; use o código (ver log da carga) |
| `Another run is using TEMP_DIR` | Já há uma execução rodando |
| Falha no meio do envio | Rode de novo: o que foi aceito está no estado e não é reenviado como novo |
| `Já há uma execução da prospecção em andamento` | O serviço ou outro `rodar_agora` está rodando: `rodar_agora.ps1 -Status` |
| E-mail diz "envio desligado" | `SERVICO_ENVIO_ATIVO=false` ou `GR_*` vazio no `.env` (intencional até o deploy do backend) |
| E-mail "PULADA — espaço em disco" | Libere espaço no disco do projeto (25 GB); a verificação do dia seguinte tenta de novo |
| `E-mail não enviado … 535` no log | Usuário/senha SMTP recusados: Gmail = senha de app; 365 = SMTP AUTH liberado |
| Instalador: "Nenhum Python >= 3.11 instalado para todos os usuários" | `winget install --id Python.Python.3.12 --scope machine` |
| Instalador: "Não consegui apagar o .venv" | Feche o VS Code e os terminais que usam o `.venv` e rode de novo |
| Serviço não inicia | `logs\winsw\*.log` e `logs\rodar_mensal_saida.txt`; `scripts\rodar_agora.ps1 -Status` |
