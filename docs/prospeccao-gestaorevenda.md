# Prospecção: Receita Federal → GestaoRevenda

Adaptação deste pipeline para gerar listas de prospecção mensais a partir dos dados abertos de CNPJ
e enviá-las como leads ao GestaoRevenda.

## Como funciona

O serviço Windows **"Ponte - Prospecção Receita"** (`servico_prospeccao.py`, rodando pelo WinSW) segue a
configuração feita no GestaoRevenda, em **Configurações → Prospecção Receita** (só administrador). Todo dia,
no horário da tela (padrão 03:00), verifica se a Receita publicou mês novo. Se o mês mais recente ainda não
foi processado, roda o `rodar_mensal.py` para ele e manda o resultado ao GestaoRevenda. O GestaoRevenda guarda
o histórico e envia o e-mail. Se o mês já foi processado, não faz nada.

```
rodar_mensal.py
 ├─ 0. busca a configuração no GestaoRevenda (config_prospeccao.py)
 ├─ 1. baixa o mês mais recente da Receita (sem os arquivos de sócios)
 ├─ 2. grava em Parquet já filtrado (filtro.py)
 │      estabelecimentos → situação, CNAE, UF, municípios, início de atividade
 │      empresas / Simples → só os CNPJs básicos que passaram
 ├─ 3. exporta uma linha por estabelecimento (exportar_prospeccao.py)
 │      e marca CNPJ_INEDITO / FILIAL_NOVA / JA_ENVIADO (estado/prospeccao.sqlite)
 ├─ 4. envia só os novos ao GestaoRevenda (enviar_gestaorevenda.py), se o envio estiver ligado
 │      POST /api/leads/importar com origem RECEITA_FEDERAL, em lotes, com nova tentativa
 └─ 5. apaga ZIPs/CSVs temporários e grava o log com as contagens
servico_prospeccao.py → resultado e sinal de vida ao GestaoRevenda (aviso_gestaorevenda.py)
```

| Arquivo / pasta | O que é |
|---|---|
| `.env` | Só `GR_API_URL`, `GR_CLIENT_ID` e `GR_CLIENT_SECRET` (fora do git) |
| `estado/prospeccao.sqlite` | O que já foi enviado, por execução, e a fila do que ainda não chegou ao GestaoRevenda. **Não apague**: sem ele tudo volta a ser "novo" e é reenviado (o GestaoRevenda deduplica) |
| `estado/config_remota.json` | A última configuração boa recebida do GestaoRevenda |
| `saida/prospeccao_AAAA-MM.csv` | Lista do mês (o que seria/foi enviado) |
| `saida/enviados_AAAA-MM_*.csv` | Backup de cada envio, com o resultado de cada linha |
| `logs/rodar_mensal_*.log` | Log de cada execução; a última linha "Resumo" traz as contagens. Apagados após 365 dias |
| `logs/servico.log` | Log do serviço (verificações diárias), rotação 10 MB × 10. `servico_manual.log` = `rodar_agora` |
| `logs/winsw/` | Log do WinSW (o "embrulho" do serviço) |
| `parquet/AAAA-MM/` | Dados filtrados do mês (uma pasta por mês) |
| `servico/` | WinSW e o XML do serviço, gerados pelo instalador (fora do git) |

## Configuração

### No GestaoRevenda: Configurações → Prospecção Receita

| Campo | Padrão | Observação |
|---|---|---|
| Envio ao GestaoRevenda | **desligado** | Desligado: o serviço baixa, filtra e conta tudo, mas não cria leads. Ligar pede confirmação e mostra quantas empresas a próxima execução enviaria |
| CNAEs principais | 4711-3/02, 4712-1/00, 4711-3/01, 4744-0/99, 4744-0/01, 4530-7/03, 4789-0/04 | Com ou sem máscara |
| UF | (todas) | Obrigatória com municípios |
| Municípios | (todos da UF) | Por nome, como a Receita grafa; acento e maiúsculas não importam. O que o pipeline não reconhecer fica de fora e aparece na tela e no e-mail. Se nenhum for reconhecido, a execução falha |
| Situação cadastral | Ativa | |
| Data de abertura mínima | últimos 12 meses | "Sem limite" = todas. A 1ª execução com envio traz todas as ativas da região, independente disto |
| Horário da verificação | 03:00 | Computador desligado nesse horário: verifica 10 min depois de ligar |
| Disco mínimo | 25 GB | Sem esse espaço livre (descontado o que já está em `temp/`): pula, registra e avisa |
| Conta do aviso / e-mails de destino | (nenhuma) | Só aparecem contas de saída, sem aquecimento e nunca usadas na prospecção. "Enviar e-mail de teste" confere |
| Responsável pela demanda | quem salvou por último | Recebe a Demanda Interna se o serviço ficar mais de 8 dias sem sinal |

Toda alteração vai para a Auditoria, com o antes e o depois de cada campo.

- **Quando a mudança vale:** o serviço busca a configuração a cada 15 min e no início de cada execução. O quadro do serviço, na própria tela, mostra qual versão ele já aplicou.
- **Filtro alterado** (CNAE, UF, município, situação ou abertura): o mês é baixado e recarregado na próxima verificação programada (cerca de 6,6 GB e 30 min), nunca na hora de salvar.
- **GestaoRevenda fora do ar:** o serviço usa `estado/config_remota.json`. Se nunca conseguiu buscar a configuração, roda com os valores padrão acima e o **envio desligado**.

### No computador do serviço: `.env`

| Variável | Observação |
|---|---|
| `GR_API_URL` | Base da API do GestaoRevenda **com** `/api` |
| `GR_CLIENT_ID` / `GR_CLIENT_SECRET` | GestaoRevenda → Configurações → API Externa: um Client com os escopos **leads** (enviar), **prospeccao-config** (ler a configuração, só leitura) e **prospeccao-execucoes** (gravar execução e sinal) |

`GR_*` vazios: nada é enviado nem buscado. O serviço roda com os padrões e o envio desligado.

Fora do `.env`, com valor fixo no código, ficam:

- Parquet;
- filtro de prospecção sempre ligado;
- 3 tentativas por dia, 60 min entre elas;
- 10 min de atraso depois de ligar;
- logs mantidos por 365 dias;
- lote de 200.

`GR_LOTE`, `GR_TENTATIVAS`, `GR_TIMEOUT` e `GR_ENFILEIRAR_CNPJA` ainda podem ser definidos, mas não precisam.

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

1. A cada 15 min, busca a configuração e manda ao GestaoRevenda o que estiver na fila.
2. No horário da tela, ou 10 min depois de ligar se o computador estava desligado nesse horário, lista os meses da Receita.
3. Se já existe execução `OK` para o mês mais recente, feita com o filtro atual, não faz nada.
   - Enquanto o envio estiver desligado, `SEM_ENVIO` também conta.
   - Ao ligar o envio, o mês volta a ficar pendente.
   - Ao mudar o filtro, o mês é recarregado.
4. Se o mês está pendente, confere o disco e roda o `rodar_mensal.py --mes <mês>` num subprocesso.
5. Manda ao GestaoRevenda o resultado e um sinal de vida (último contato, próxima verificação, quantas empresas iriam no próximo envio). Ele grava o histórico e manda o e-mail com as contagens (lidas, filtradas, novas, enviadas, importadas, duplicadas, bloqueadas, recusadas) ou o erro com o fim do log.

Os dias sem mês novo mandam só o sinal, sem e-mail. Se o GestaoRevenda não responder, tudo fica na fila
(`estado/prospeccao.sqlite`) e vai no ciclo seguinte. A chave de cada execução impede histórico ou e-mail em dobro.

**Alerta:** se o GestaoRevenda ficar mais de 8 dias sem nenhum contato do serviço, cria uma Demanda Interna
(setor ADMIN, prioridade alta) para o responsável da tela. Verifica todo dia às 09:00 e não duplica enquanto a anterior estiver aberta.

**Uma execução por vez:** a trava `estado/.prospeccao.lock` cobre carga, exportação e envio. O serviço e
o `rodar_agora` usam a mesma trava; quem chega depois sai sem fazer nada.

**Desligado no meio:** a próxima execução retoma de onde parou.

| Onde parou | O que acontece |
|---|---|
| Download | Retoma o `.part` |
| Carga | Pula as tabelas Parquet já gravadas (a gravação é atômica). Se o filtro mudou, refaz o mês |
| Exportação | Refeita (~1 s) |
| Envio | Os lotes aceitos já estão no estado e não são reenviados. O lote que estava no ar, se for de novo, volta `DUPLICADO` do GestaoRevenda |

A execução que ficou pela metade aparece como `INTERROMPIDA`. Os temporários só são apagados quando a execução termina bem.

**`--limite`** gera status `PARCIAL`. Esse status não conta como mês concluído nem como "primeira execução", então a janela continua em 0 meses e o restante vai na execução seguinte.

### Instalar

1. Python **para todos os usuários** (o instalador exige e recria o `.venv` a partir dele):
   `winget install --id Python.Python.3.12 --scope machine`. Ou use o instalador de python.org: "Customize installation" → "Install Python for all users".
2. Preencha os três `GR_*` no `.env`.
3. Feche o VS Code e os terminais que usam o `.venv`, porque ele é recriado.
4. PowerShell **como Administrador**, na pasta do projeto:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\instalar_servico.ps1
   ```
   Pode rodar de novo para atualizar (depois de um `git pull`, por exemplo). Ele avisa se o `.env` ainda tiver variáveis antigas.
5. Remover: `scripts\desinstalar_servico.ps1` (como Administrador). Mantém `estado\`, `logs\`, `saida\`, `parquet\` e o `.env`.

O instalador deixa o `.env` legível só por SYSTEM, Administradores, o seu usuário e o serviço.

### Rodar na hora e acompanhar

Não precisa de administrador:

```powershell
scripts\rodar_agora.ps1                 # igual à verificação diária (respeita o envio da tela)
scripts\rodar_agora.ps1 -SemEnvio       # gera a lista sem enviar
scripts\rodar_agora.ps1 -Limite 20      # envia só 20
scripts\rodar_agora.ps1 -Forcar         # roda mesmo se o mês já foi processado
scripts\rodar_agora.ps1 -Recarregar     # baixa e carrega o mês do zero
scripts\rodar_agora.ps1 -Status         # configuração em uso, verificações, execuções, fila
scripts\rodar_agora.ps1 -TestarConexao  # busca a configuração e manda um sinal ao GestaoRevenda
```

O `rodar_agora` usa a configuração da tela como o serviço. Se o filtro mudou, ele também recarrega o mês.

## Roteiro de teste

Pré-requisitos:

- a branch `prospeccao-receita-federal` do GestaoRevenda já publicada;
- Python para todos os usuários instalado no computador do serviço.

### A. GestaoRevenda (branch `prospeccao-receita-config`)

1. Publique o backend e o frontend. A migration `20261116000000_prospeccao_receita_config` roda no start do container e só cria três tabelas novas.
2. **Acesso:** entre com um usuário que não é administrador.
   - Esperado: "Prospecção Receita" não aparece em Configurações, e `GET /api/prospeccao-receita/config` dá 403.
   - Como administrador: a aba aparece em Integrações, com o envio desligado e o quadro "O serviço ainda não entrou em contato".
3. **Configuração:** preencha os campos e salve.
   - UF `SC`.
   - Os 21 municípios, um por linha: ANGELINA, BARRA VELHA, BIGUACU, BLUMENAU, BOTUVERA, BRUSQUE, CANELINHA, GASPAR, GUABIRUBA, ILHOTA, INDAIAL, ITAJAI, ITAPEMA, MAJOR GERCINO, NAVEGANTES, NOVA TRENTO, PENHA, POMERODE, RANCHO QUEIMADO, SAO JOAO BATISTA, TIJUCAS. Acrescente **CIDADE TESTE**, que sai no passo 13.
   - Conta do aviso e o seu e-mail de destino.

   Esperado:
   - antes de salvar, o aviso "Isso vai baixar e recarregar a base na próxima madrugada (cerca de 6,6 GB e 30 minutos)";
   - em Auditoria, a ação `prospeccao-receita.config` com o antes/depois.
4. **Contas:** confira que a lista de contas não mostra nenhuma conta de prospecção nem com aquecimento. "Enviar e-mail de teste": o e-mail chega.
5. **Client:** em API Externa, crie um Client com os escopos **leads**, **prospeccao-config** e **prospeccao-execucoes**. Os escopos não se editam, por isso um Client novo; revogue o antigo, se houver.

### B. Pipeline (branch `prospeccao-config-gestaorevenda`), sem enviar leads

6. Preencha os três `GR_*` no `.env` e rode `scripts\instalar_servico.ps1 -SemIniciar` como Administrador.
7. `scripts\rodar_agora.ps1 -TestarConexao`.
   - Esperado: "configuração v… do GestaoRevenda" e "Sinal enviado".
   - Na tela: "Serviço em dia" e a configuração aplicada igual à atual.
8. `scripts\rodar_agora.ps1 -Status`: os 22 municípios, o envio "desligado na tela do GestaoRevenda" e a fila 0.
9. `scripts\rodar_agora.ps1 -Forcar` (~30 min).
   - Esperado: `SEM_ENVIO` com cerca de 7.174 na lista, mais o aviso "Municípios não reconhecidos: CIDADE TESTE".
   - Na tela, em Execuções: a linha "Sem envio" com as contagens e o município destacado.
   - E-mail "OK (envio desligado)" com o mesmo aviso.
10. **Retomada:** rode `scripts\rodar_agora.ps1 -Recarregar` e aperte Ctrl+C durante o download. Rode de novo `scripts\rodar_agora.ps1 -Forcar`.
    - Esperado: a anterior vira `INTERROMPIDA` e o download continua de onde parou.
    - Com essa execução rodando, abra outro terminal e rode `scripts\rodar_agora.ps1 -Forcar`: resultado `OCUPADO`.
11. **Disco:** na tela, mude o disco mínimo para 2000 GB e rode `scripts\rodar_agora.ps1 -Forcar`.
    - Esperado: `DISCO_INSUFICIENTE`, o e-mail "PULADA — espaço em disco insuficiente" e a linha "Pulada (disco)" em Execuções.
    - Depois, volte para 25.
12. **GestaoRevenda fora do ar:** troque o `GR_API_URL` por um endereço inválido e rode `scripts\rodar_agora.ps1 -Status`.
    - Esperado: "configuração v… guardada em … (GestaoRevenda não respondeu…)".
    - Depois, volte o endereço certo.

### C. Serviço

13. Na tela, tire o CIDADE TESTE (o aviso de recarga aparece de novo), ponha o horário uns 20 min à frente e salve.
    - Rode `Start-Service PonteProspeccaoReceita`. Em até 15 min, o quadro mostra a configuração nova como aplicada.
    - Esperado no horário, em `logs\servico.log`: "Filtro alterado na tela … será baixado e recarregado" e a execução.
    - Em Execuções aparece um `SEM_ENVIO` novo, sem município não reconhecido. Na verificação seguinte: `SEM_NOVIDADE` e só o sinal.
14. Reinicie o Windows **sem fazer login**, espere uns 15 min e entre.
    - Esperado: o serviço "Em execução" e, na tela, um contato do serviço depois do reinício.
15. Com uma execução do serviço em andamento, rode `Stop-Service PonteProspeccaoReceita` e depois inicie o serviço de novo.
    - Esperado: a anterior fica `INTERROMPIDA` e a nova retoma.
16. **Alerta de 8 dias:** pare o serviço. No banco do GestaoRevenda, rode:
    ```sql
    UPDATE prospeccao_receita_sinal SET ultimo_contato_em = now() - interval '9 days'
    ```
    - Esperado no dia seguinte, depois das 09:00: uma Demanda Interna "Prospecção Receita: serviço sem sinal há 9 dias" para o responsável, com notificação.
    - No outro dia, nenhuma demanda nova.
    - Inicie o serviço: o quadro volta a "em dia". Conclua a demanda.
17. Volte o horário para 03:00.

### D. Envio real

18. **Regressão:** importe um CSV pela tela de Leads como sempre. Esperado: o mesmo resultado de antes.
19. Ligue o envio na tela.
    - Esperado: a confirmação "A próxima execução enviaria cerca de 7.174 empresas (mês 2026-10, contagem de …)".
    - Confirme. Auditoria: `envioAtivo` de `false` para `true`.
20. `scripts\rodar_agora.ps1 -Limite 20`.
    - Esperado: `PARCIAL`, 20 enviadas e a linha "Parcial" em Execuções.
    - Confira 3 leads com a fonte "Receita Federal". Se o envio for desligado e ligado de novo, a confirmação mostra cerca de 7.154.
21. Rode `scripts\rodar_agora.ps1`, desligue o computador no meio dos lotes e rode de novo.
    - Esperado: envia só o que faltou, `OK` no fim, nenhum CNPJ em dobro.
    - Em Execuções: o `OK` com as contagens e o e-mail "OK 2026-10 — … novas, … enviadas".
22. Na verificação seguinte do serviço: `SEM_NOVIDADE`, sem e-mail.

### E. Remoção

23. `scripts\desinstalar_servico.ps1` (como Administrador).
    - Esperado: o serviço some, e `estado\`, `logs\`, `saida\` e `parquet\` continuam no lugar.
    - Na tela, depois de 8 dias sem contato, a demanda de alerta aparece. É esperado.

## Problemas comuns

| Sintoma | Causa / o que fazer |
|---|---|
| `Credenciais recusadas` | Client ID/Secret errados ou revogados |
| `não tem o escopo 'prospeccao-config'` (ou `leads`, `prospeccao-execucoes`) | Crie um Client novo com os três escopos (não dá para editar escopos) |
| `/prospeccao-receita/servico/... não existe (404)` | Backend do GestaoRevenda sem a branch `prospeccao-receita-config`. Execuções e sinal ficam na fila até o deploy |
| `Resposta sem 'resultados'` / `Lote recusado (400)` com `origem should not exist` | Backend do GestaoRevenda ainda sem a importação com origem — publique o backend |
| "Municípios não reconhecidos" na tela/e-mail | Nome grafado diferente da Receita (ex.: sem "D'"). Corrija na tela; acento não importa |
| `Nenhum dos municípios do filtro foi reconhecido` | Todos os nomes estão errados: a execução falha de propósito (senão viraria a UF inteira) |
| Tela: "usando a cópia guardada" | O serviço não alcançou o GestaoRevenda na última busca (rede, `GR_API_URL`) |
| Tela: "usando os valores padrão" | O serviço nunca conseguiu buscar a configuração: confira `GR_*` e `scripts\rodar_agora.ps1 -TestarConexao` |
| Execução com "e-mail: não enviado — a conta escolhida não pode mais ser usada" | A conta ficou inativa, ganhou aquecimento ou foi usada na prospecção: escolha outra na tela |
| `Another run is using TEMP_DIR` | Já há uma execução rodando |
| Falha no meio do envio | Rode de novo: o que foi aceito está no estado e não é reenviado como novo |
| `Já há uma execução da prospecção em andamento` | O serviço ou outro `rodar_agora` está rodando: `rodar_agora.ps1 -Status` |
| E-mail "PULADA — espaço em disco" | Libere espaço no disco do projeto (ou ajuste o mínimo na tela); a verificação do dia seguinte tenta de novo |
| Instalador: "Nenhum Python >= 3.11 instalado para todos os usuários" | `winget install --id Python.Python.3.12 --scope machine` |
| Instalador: "Não consegui apagar o .venv" | Feche o VS Code e os terminais que usam o `.venv` e rode de novo |
| Serviço não inicia | `logs\winsw\*.log` e `logs\rodar_mensal_saida.txt`; `scripts\rodar_agora.ps1 -Status` |
