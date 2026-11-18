# Prospecção: Receita Federal → GestaoRevenda

Adaptação deste pipeline para gerar listas de prospecção a partir dos dados abertos de CNPJ e
enviá-las como leads ao GestaoRevenda. Dois caminhos usam a mesma base e o mesmo registro do que já
foi enviado:

- **Tela local** (ícone "Prospecção Receita" na área de trabalho): a pessoa escolhe os filtros, vê o
  resultado em segundos, baixa em Excel e envia quando quiser. Ver [Tela local](#tela-local).
- **Envio automático mensal**, configurado no GestaoRevenda (desligado por padrão). Ver abaixo.

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
 ├─ 2. grava a BASE RÁPIDA em Parquet (filtro.py, FiltroProspeccao.base)
 │      estabelecimentos → todos os da UF, de qualquer situação, CNAE e data
 │      empresas / Simples → só os CNPJs básicos desses estabelecimentos
 ├─ 3. seleciona na base com o filtro do envio automático (exportar_prospeccao.py, DuckDB)
 │      e marca CNPJ_INEDITO / FILIAL_NOVA / JA_ENVIADO (estado/prospeccao.sqlite)
 ├─ 4. envia só os novos ao GestaoRevenda (enviar_gestaorevenda.py), se o envio estiver ligado
 │      POST /api/leads/importar com origem RECEITA_FEDERAL, em lotes, com nova tentativa
 └─ 5. apaga ZIPs/CSVs temporários e grava o log com as contagens
servico_prospeccao.py → resultado e sinal de vida ao GestaoRevenda (aviso_gestaorevenda.py)
                      → tela local em http://127.0.0.1:8765 (tela_local.py + consulta_base.py + tela/)
```

| Arquivo / pasta | O que é |
|---|---|
| `.env` | Só `GR_API_URL`, `GR_CLIENT_ID` e `GR_CLIENT_SECRET` (fora do git) |
| `estado/prospeccao.sqlite` | O que já foi enviado (pelo envio automático e pela tela), por execução, o histórico de buscas da tela e a fila do que ainda não chegou ao GestaoRevenda. **Não apague**: sem ele tudo volta a ser "novo" e é reenviado (o GestaoRevenda deduplica) |
| `estado/config_remota.json` | A última configuração boa recebida do GestaoRevenda |
| `saida/prospeccao_AAAA-MM.csv` | Lista do mês (o que seria/foi enviado) |
| `saida/enviados_AAAA-MM_*.csv` | Backup de cada envio, com o resultado de cada linha |
| `logs/rodar_mensal_*.log` | Log de cada execução; a última linha "Resumo" traz as contagens. Apagados após 365 dias |
| `logs/servico.log` | Log do serviço (verificações diárias), rotação 10 MB × 10. `servico_manual.log` = `rodar_agora` |
| `logs/winsw/` | Log do WinSW (o "embrulho" do serviço) |
| `parquet/AAAA-MM/` | Base rápida do mês: todos os estabelecimentos da UF (uma pasta por mês; ficam os 2 mais recentes) |
| `tela/` | A página da tela local (HTML, JS, CSS e o ícone) |
| `logs/instalacao.log` | O que o `Instalar.cmd` fez (mande ao suporte se a instalação falhar) |
| `servico/` | WinSW e o XML do serviço, gerados pelo instalador (fora do git) |

## Configuração

### No GestaoRevenda: Configurações → Prospecção Receita

| Campo | Padrão | Observação |
|---|---|---|
| Envio ao GestaoRevenda | **desligado** | Desligado: o serviço baixa, filtra e conta tudo, mas não cria leads. Ligar pede confirmação e mostra quantas empresas a próxima execução enviaria |
| CNAEs principais | 4711-3/02, 4712-1/00, 4711-3/01, 4744-0/99, 4744-0/01, 4530-7/03, 4789-0/04 | Com ou sem máscara |
| UF | SC | Obrigatória com municípios. É também a UF da **base rápida**: mudar a UF faz baixar a base de novo |
| Municípios | (todos da UF) | Por nome, como a Receita grafa; acento e maiúsculas não importam. O que o pipeline não reconhecer fica de fora e aparece na tela e no e-mail. Se nenhum for reconhecido, a execução falha |
| Situação cadastral | Ativa | |
| Data de abertura mínima | últimos 12 meses | "Sem limite" = todas. A 1ª execução com envio traz todas as ativas da região, independente disto |
| Horário da verificação | 03:00 | Computador desligado nesse horário: verifica 10 min depois de ligar |
| Disco mínimo | 25 GB | Sem esse espaço livre (descontado o que já está em `temp/`): pula, registra e avisa |
| Conta do aviso / e-mails de destino | (nenhuma) | Só aparecem contas de saída, sem aquecimento e nunca usadas na prospecção. "Enviar e-mail de teste" confere |
| Responsável pela demanda | quem salvou por último | Recebe a Demanda Interna se o serviço ficar mais de 8 dias sem sinal |

Toda alteração vai para a Auditoria, com o antes e o depois de cada campo.

- **Quando a mudança vale:** o serviço busca a configuração a cada 15 min e no início de cada execução. O quadro do serviço, na própria tela, mostra qual versão ele já aplicou.
- **Filtro alterado** (CNAE, município, situação ou abertura): a seleção do envio automático é refeita sobre a base já carregada na próxima verificação programada, sem baixar nada. Só **mudar a UF** faz baixar e recarregar a base (cerca de 6,6 GB e 30 min), também na próxima verificação, nunca na hora de salvar.
- **GestaoRevenda fora do ar:** o serviço usa `estado/config_remota.json`. Se nunca conseguiu buscar a configuração, roda com os valores padrão acima e o **envio desligado**.

### No computador do serviço: `.env`

| Variável | Observação |
|---|---|
| `GR_API_URL` | Base da API do GestaoRevenda **com** `/api` |
| `GR_CLIENT_ID` / `GR_CLIENT_SECRET` | GestaoRevenda → Configurações → API Externa: um Client com os escopos **leads** (enviar), **prospeccao-config** (ler a configuração, só leitura) e **prospeccao-execucoes** (gravar execução e sinal) |

`GR_*` vazios: nada é enviado nem buscado. O serviço roda com os padrões e o envio desligado.

Fora do `.env`, com valor fixo no código, ficam:

- Parquet, base rápida da UF (2 meses guardados);
- tela local na porta 8765, só neste computador;
- confirmação com o número digitado acima de 500 empresas num envio pela tela;
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

## Espaço em disco e tempo

Medido na 1ª carga filtrada (base 2026-10), antes da base rápida:

| | |
|---|---|
| Download | 6,6 GB (sem sócios) — 14 arquivos. Igual com a base rápida: a Receita só publica o Brasil inteiro |
| Pico de disco | ~17 GB medidos (pasta `temp/` + cópia UTF-8 na pasta temporária do sistema). **Reserve 25 GB livres** |
| Tempo da carga | **27,5 min** (download ~2,2 MB/s por arquivo, 4 em paralelo). A base rápida lê os mesmos arquivos: deve ficar perto disso |
| Exportação | ~1 s |
| Envio | depende do GestaoRevenda; estimativa 7.174 linhas em 36 lotes de 200 |

**Base rápida (estimativa, a confirmar na 1ª carga):** SC tem perto de 1,3 milhão de estabelecimentos
ativos e uns 3 milhões contando baixados, suspensos e inaptos. Em Parquet, isso deve dar **400 a 600 MB
por mês** (estabelecimentos + empresas + Simples), ou até ~1,2 GB com os 2 meses guardados. O tamanho
real aparece no alto da tela local e no `rodar_agora.ps1 -Status`. Consultas na tela: 1 a 2 s. Durante
a carga, o conjunto de CNPJs básicos da UF fica em memória (algumas centenas de MB).

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
   - Ao mudar o filtro, a seleção do mês é refeita sobre a base (só a UF faz baixar de novo).
4. Se o mês está pendente, confere o disco e roda o `rodar_mensal.py --mes <mês>` num subprocesso.
5. Manda ao GestaoRevenda o resultado e um sinal de vida (último contato, próxima verificação, quantas empresas iriam no próximo envio). Ele grava o histórico e manda o e-mail com as contagens (lidas, filtradas, novas, enviadas, importadas, duplicadas, bloqueadas, recusadas) ou o erro com o fim do log.

Os dias sem mês novo mandam só o sinal, sem e-mail. Se o GestaoRevenda não responder, tudo fica na fila
(`estado/prospeccao.sqlite`) e vai no ciclo seguinte. A chave de cada execução impede histórico ou e-mail em dobro.

**Alerta:** se o GestaoRevenda ficar mais de 8 dias sem nenhum contato do serviço, cria uma Demanda Interna
(setor ADMIN, prioridade alta) para o responsável da tela. Verifica todo dia às 09:00 e não duplica enquanto a anterior estiver aberta.

**Uma execução por vez:** a trava `estado/.prospeccao.lock` cobre carga, exportação e envio. O serviço,
o `rodar_agora` e a tela local (envio e "Atualizar base agora") usam a mesma trava; quem chega depois
não faz nada (a tela avisa "o serviço está trabalhando na base agora").

**Desligado no meio:** a próxima execução retoma de onde parou.

| Onde parou | O que acontece |
|---|---|
| Download | Retoma o `.part` |
| Carga | Pula as tabelas Parquet já gravadas (a gravação é atômica). Se a UF mudou, refaz o mês |
| Exportação | Refeita (~1 s) |
| Envio | Os lotes aceitos já estão no estado e não são reenviados. O lote que estava no ar, se for de novo, volta `DUPLICADO` do GestaoRevenda |

A execução que ficou pela metade aparece como `INTERROMPIDA`. Os temporários só são apagados quando a execução termina bem.

**`--limite`** gera status `PARCIAL`. Esse status não conta como mês concluído nem como "primeira execução", então a janela continua em 0 meses e o restante vai na execução seguinte.

### Instalar

**Dois cliques em `Instalar.cmd`** (na pasta do projeto) e "Sim" quando o Windows pedir permissão. Ele:

1. instala o **Python 3.12 para todos os usuários**, se faltar: pelo winget (que confere o hash) ou,
   sem winget, pelo instalador oficial do python.org, só se a assinatura digital for válida e da
   Python Software Foundation;
2. cria o `.env` a partir do `.env.example`, se não existir (sem os `GR_*`, a tela funciona mas não envia);
3. recria o `.venv`, se preciso (feche antes o VS Code e os terminais que usam o `.venv`), e instala as dependências;
4. instala o serviço, cria o ícone **"Prospecção Receita"** na área de trabalho de todos os usuários e
   confere que a tela responde;
5. termina com uma janela "Instalação concluída" ou, se algo falhar, com uma mensagem simples do que
   fazer. O detalhe fica em `logs\instalacao.log`.

Pode rodar de novo para atualizar (depois de um `git pull`, por exemplo). Pela linha de comando, num
PowerShell **como Administrador**: `powershell -ExecutionPolicy Bypass -File scripts\instalar_servico.ps1`
(mesmas etapas, mensagens no terminal). Ele avisa se o `.env` ainda tiver variáveis antigas.

Remover: `scripts\desinstalar_servico.ps1` (como Administrador). Tira o serviço e o ícone e mantém
`estado\`, `logs\`, `saida\`, `parquet\` e o `.env`.

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

O `rodar_agora` usa a configuração da tela como o serviço. Se a UF mudou, ele também recarrega a base.

## Tela local

Dois cliques no ícone **"Prospecção Receita"** abrem a página `http://127.0.0.1:8765` no navegador. Ela
é servida pelo próprio serviço (sem terminal, sem outro programa) e só atende este computador.

| Parte | O que faz |
|---|---|
| Alto da página | Mês da base em uso, data da atualização e tamanho. **"Atualizar base agora"**: baixa o mês mais recente da Receita (cerca de 30 min, com o andamento em GB baixados); se a base já é a mais recente, pergunta se quer baixar de novo |
| Filtros | Atividades (CNAE) por nome ou código, com busca; cidades por nome (as da UF da base); abertura de/até ou todas; situação (ativa por padrão); matriz/filial; capital social mínimo e/ou máximo (liga/desliga); excluir MEI |
| Ver resultado | Encontradas, já enviadas antes e novas; entre as novas, quantas são MEI e quantas não têm telefone nem e-mail; prévia das 100 primeiras (novas primeiro) |
| Baixar em Excel | Todas as encontradas (até o limite do Excel), com a coluna "Envio" (Nova / Já enviada) |
| Enviar ao GestaoRevenda | Só as novas. Confirmação com a quantidade, MEI e sem contato; **acima de 500**, uma segunda confirmação com o número digitado (conferido também no servidor). Mostra o andamento e, no fim, importadas, já existiam, bloqueadas e recusadas |
| Histórico | Cada busca com data, base, filtros e contagens, e o resultado do envio feito a partir dela. **"Repetir"** refaz a busca com a base atual |

- **Envio:** o mesmo do envio automático (lotes de 200, sem duplicar, bloqueados recusados). Vai para o
  histórico de Execuções do GestaoRevenda com origem "tela", **sem e-mail**. Não conta como execução
  do envio automático (o mês continua pendente para ele), mas o que a tela enviou não volta como novo
  em nenhum dos dois.
- **Envio que para no meio:** o que já foi aceito fica registrado; "Ver resultado" de novo mostra só o
  que falta.
- **"Já enviadas"** são as que este computador já mandou (pela tela ou pelo envio automático). Uma
  empresa que entrou no GestaoRevenda por outro caminho (CSV, cadastro manual) aparece como nova e
  volta "já existia" no resultado do envio.
- **Segurança:** a página só escuta em 127.0.0.1, recusa outro nome de site no endereço (DNS rebinding)
  e recusa pedidos de envio vindos de outros sites abertos no navegador.
- Sem o serviço (desenvolvimento): `.venv\Scripts\python tela_local.py`.

## Roteiro de teste

Pré-requisitos:

- a branch `prospeccao-receita-federal` do GestaoRevenda já publicada;
- para A–E, as branches `prospeccao-receita-tela-local` (GestaoRevenda) e `prospeccao-tela-local`
  (pipeline), que incluem `prospeccao-receita-config` e `prospeccao-config-gestaorevenda`.

A parte F é o roteiro da tela local, em linguagem simples.

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
   - antes de salvar, o aviso "Isso vai baixar e recarregar a base na próxima madrugada (cerca de 6,6 GB e 30 minutos)", porque a UF mudou (mudar só municípios ou CNAEs não mostra o aviso);
   - em Auditoria, a ação `prospeccao-receita.config` com o antes/depois.
4. **Contas:** confira que a lista de contas não mostra nenhuma conta de prospecção nem com aquecimento. "Enviar e-mail de teste": o e-mail chega.
5. **Client:** em API Externa, crie um Client com os escopos **leads**, **prospeccao-config** e **prospeccao-execucoes**. Os escopos não se editam, por isso um Client novo; revogue o antigo, se houver.

### B. Pipeline (branch `prospeccao-config-gestaorevenda`), sem enviar leads

6. Preencha os três `GR_*` no `.env` e rode o `Instalar.cmd` (dois cliques). Depois, pare o serviço (`Stop-Service PonteProspeccaoReceita`) para os passos 7 a 12.
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

13. Na tela, tire o CIDADE TESTE (sem aviso de recarga: a UF não mudou), ponha o horário uns 20 min à frente e salve.
    - Rode `Start-Service PonteProspeccaoReceita`. Em até 15 min, o quadro mostra a configuração nova como aplicada.
    - Esperado no horário, em `logs\servico.log`: "Filtro alterado na tela … refazendo a seleção do mês" e a execução, **sem baixar de novo** (poucos segundos).
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
    - O ícone "Prospecção Receita" some da área de trabalho.
    - Na tela, depois de 8 dias sem contato, a demanda de alerta aparece. É esperado.

### F. Tela local (linguagem simples)

Antes: o serviço instalado (passo 6) e a internet funcionando.

24. **Abrir:** dê dois cliques no ícone "Prospecção Receita" da área de trabalho.
    - Esperado: abre uma página no navegador, sem nenhuma janela preta. No alto: "Ainda não há base carregada".
25. **Primeira base:** clique em "Atualizar base agora".
    - Esperado: uma faixa azul "Atualizando a base… GB baixados, … min". Pode fechar a página e voltar depois.
    - Em cerca de 30 minutos: "Base de … pronta", e no alto o mês, a data e o tamanho (anote o tamanho).
26. **Primeira busca:** em Atividades, digite "supermerc" e escolha "4711-3/02 Supermercados". Em
    Cidades, digite "blum" e escolha BLUMENAU. Em Data de abertura, escolha "Escolher período" e
    clique em "últimos 12 meses". Clique em "Ver resultado".
    - Esperado: em poucos segundos, as caixas "empresas encontradas", "já enviadas antes" e "novas", a
      linha "Entre as novas: … são MEI e … não têm telefone nem e-mail" e a prévia.
27. **Mais filtros:** marque "Filtrar por capital social", ponha mínimo 50.000, marque "Deixar de fora
    os MEI" e clique em "Ver resultado".
    - Esperado: o número de encontradas cai, e a prévia não tem MEI nem capital abaixo de R$ 50.000.
28. **Excel:** clique em "Baixar em Excel" e abra o arquivo.
    - Esperado: a mesma quantidade de linhas que "encontradas", com a coluna "Envio" em "Nova".
29. **Envio pequeno:** ajuste os filtros até ter menos de 500 novas (por exemplo, uma cidade e uma
    atividade) e clique em "Enviar ao GestaoRevenda".
    - Esperado: uma confirmação com a quantidade, MEI e sem contato. Clique em "Enviar".
    - No alto: "Enviando… X de Y". No fim, uma janela com importadas, já existiam, bloqueadas e recusadas.
    - No GestaoRevenda, em Leads: as empresas com a fonte "Receita Federal". Em Configurações →
      Prospecção Receita → Execuções: uma linha "Tela local", **sem e-mail**.
30. **Não manda duas vezes:** clique em "Ver resultado" de novo com os mesmos filtros.
    - Esperado: as que foram enviadas aparecem em "já enviadas antes", e "novas" fica em 0 (botão "Nada novo para enviar").
31. **Trava de quantidade:** tire as cidades e as atividades para passar de 500 novas e clique em
    "Enviar ao GestaoRevenda". Clique em "Continuar".
    - Esperado: a janela "Confirme a quantidade", com o botão "Enviar" apagado. Digite um número
      errado: continua apagado. **Clique em "Cancelar"** (não precisa enviar esse tanto no teste).
32. **Histórico:** clique na aba "Histórico".
    - Esperado: as buscas feitas, com data, filtros e contagens; a do passo 29 com o resultado do envio.
    - Clique em "Repetir" numa delas: volta para "Nova busca" com os mesmos filtros e o resultado.
33. **Base já em dia:** clique em "Atualizar base agora".
    - Esperado: a pergunta "A base já está em dia… Baixar de novo mesmo assim?". Clique em "Cancelar".
34. **Uma coisa por vez:** com o serviço rodando uma verificação (`scripts\rodar_agora.ps1 -Forcar` num
    terminal), tente enviar pela tela.
    - Esperado: a mensagem "O serviço está trabalhando na base agora…". Depois que terminar, o envio funciona.
35. **Só neste computador:** de outro computador da rede, tente abrir `http://<IP deste computador>:8765`.
    - Esperado: não abre.
36. **Sem configuração do GestaoRevenda:** apague os valores `GR_*` do `.env` (guarde-os antes) e
    reinicie o serviço.
    - Esperado: a faixa amarela "O envio ao GestaoRevenda ainda não está configurado…". Ver resultado e
      Excel funcionam; o botão de enviar fica apagado. Depois, devolva os valores e reinicie o serviço.
37. **Instalação do zero (opcional, num computador sem Python):** copie a pasta do projeto e dê dois
    cliques em `Instalar.cmd`.
    - Esperado: o Python é instalado sozinho e a janela "Instalação concluída" aparece. Se faltar
      internet, a mensagem diz o que fazer e onde está o arquivo de detalhes.

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
| Instalador: "Não consegui baixar o Python" / "os componentes do programa" | Sem internet (ou bloqueada): confira e rode o `Instalar.cmd` de novo |
| Instalador: "não tem a assinatura digital da Python Software Foundation" | O arquivo baixado não é o oficial: não instale e avise o suporte |
| O ícone abre uma página de erro ("não é possível acessar") | O serviço está parado ou iniciando: espere 1 minuto; se continuar, reinicie o computador. `logs\servico.log` |
| `Tela local não iniciou: porta 8765 ocupada` no `logs\servico.log` | Outro programa usa a porta: feche-o e reinicie o serviço |
| Tela: "O resultado mudou desde a busca" | A base ou o que já foi enviado mudou entre a busca e o envio: clique em "Ver resultado" de novo |
| Instalador: "Não consegui apagar o .venv" | Feche o VS Code e os terminais que usam o `.venv` e rode de novo |
| Serviço não inicia | `logs\winsw\*.log` e `logs\rodar_mensal_saida.txt`; `scripts\rodar_agora.ps1 -Status` |
