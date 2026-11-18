'use strict';
/* Tela local da Prospecção Receita (servida por tela_local.py). Sem bibliotecas: tudo o que vem
 * do servidor entra na página por textContent, nunca como HTML. */

const $ = (id) => document.getElementById(id);
const numero = (n) => Number(n || 0).toLocaleString('pt-BR');
const reais = (n) => (n == null ? '—' : Number(n).toLocaleString('pt-BR', { style: 'currency', currency: 'BRL' }));
const dataBR = (iso) => (iso ? String(iso).slice(0, 10).split('-').reverse().join('/') : '—');
const dataHoraBR = (iso) => (iso ? new Date(iso).toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'short' }) : '—');
const formatarCnae = (c) => (c && c.length === 7 ? `${c.slice(0, 4)}-${c[4]}/${c.slice(5)}` : c);
const semAcento = (t) => String(t || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();

function el(tag, attrs = {}, ...filhos) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const f of filhos.flat()) {
    if (f == null || f === false) continue;
    e.append(f instanceof Node ? f : document.createTextNode(String(f)));
  }
  return e;
}

// ── Comunicação com o serviço ────────────────────────────────────────────────

async function api(caminho, corpo) {
  const opcoes = corpo === undefined ? {} : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Prospeccao': '1' },
    body: JSON.stringify(corpo),
  };
  let resp;
  try {
    resp = await fetch(caminho, opcoes);
  } catch {
    throw new Error('O serviço "Ponte - Prospecção Receita" não respondeu. Se continuar assim, reinicie o computador.');
  }
  const tipo = resp.headers.get('Content-Type') || '';
  if (!resp.ok) {
    const dados = tipo.includes('json') ? await resp.json() : {};
    throw new Error(dados.erro || `Erro ${resp.status}`);
  }
  if (tipo.includes('json')) return resp.json();
  return resp;
}

let temporizadorMensagem;
function mensagem(texto, ok = false) {
  const m = $('mensagem');
  m.textContent = texto;
  m.className = ok ? 'toast ok' : 'toast';
  clearTimeout(temporizadorMensagem);
  temporizadorMensagem = setTimeout(() => m.classList.add('oculto'), ok ? 5000 : 9000);
}

// ── Caixa de diálogo ─────────────────────────────────────────────────────────

function dialogo({ titulo, corpo, ok = 'OK', cancelar = 'Cancelar', okHabilitado = true, okClasse = 'principal' }) {
  return new Promise((resolver) => {
    const dlg = $('dlg');
    $('dlg-titulo').textContent = titulo;
    $('dlg-corpo').replaceChildren(...[corpo].flat().filter(Boolean));
    const botaoOk = $('dlg-ok');
    botaoOk.textContent = ok;
    botaoOk.className = okClasse;
    botaoOk.disabled = !okHabilitado;
    $('dlg-cancelar').textContent = cancelar;
    $('dlg-cancelar').classList.toggle('oculto', !cancelar);
    const fechar = (valor) => {
      botaoOk.onclick = null;
      $('dlg-cancelar').onclick = null;
      dlg.oncancel = null;
      if (dlg.open) dlg.close();
      resolver(valor);
    };
    botaoOk.onclick = () => fechar(true);
    $('dlg-cancelar').onclick = () => fechar(false);
    // Esc. (Não usar o evento "close": ele chega depois, e fecharia a caixa seguinte, como a da
    // confirmação com o número digitado.)
    dlg.oncancel = (ev) => { ev.preventDefault(); fechar(false); };
    dlg.showModal();
  });
}

// ── Estado geral (base, atualização, envio) ──────────────────────────────────

const app = {
  estado: null,
  opcoes: null,
  cnaes: new Map(), // código → descrição (escolhidos)
  cidades: new Map(), // código → nome (escolhidas)
  ultimoResultado: null,
  avisosVistos: new Set(),
};

async function atualizarEstado() {
  try {
    app.estado = await api('/api/estado');
  } catch (e) {
    $('base').textContent = e.message;
    return;
  }
  const { base, atualizacao, envio, gestaorevenda } = app.estado;
  if (base) {
    $('base').textContent =
      `Base da Receita de ${base.mes_texto} · ${base.uf || ''} · ${numero(base.estabelecimentos)} estabelecimentos` +
      ` · atualizada em ${dataHoraBR(base.carregada_em)} · ${base.tamanho_mb ? `${numero(base.tamanho_mb)} MB` : 'menos de 1 MB'}`;
  } else {
    $('base').textContent = 'Ainda não há base carregada. Clique em "Atualizar base agora" (leva cerca de 30 minutos).';
  }
  if (base && (!app.opcoes || app.opcoes.mes !== base.mes)) carregarOpcoes();

  // Atualização da base
  const faixaA = $('faixa-atualizacao');
  const rodando = atualizacao && !atualizacao.fim;
  $('btn-atualizar').disabled = Boolean(rodando);
  if (rodando) {
    faixaA.className = 'faixa';
    faixaA.textContent =
      `Atualizando a base de ${atualizacao.mes_texto}… ${String(atualizacao.baixado_gb).replace('.', ',')} GB baixados, ` +
      `${atualizacao.decorrido_min} min. Leva cerca de 30 minutos; pode fechar esta página, a atualização continua.`;
  } else if (atualizacao && atualizacao.fim && !app.avisosVistos.has('a' + atualizacao.fim)) {
    faixaA.className = atualizacao.ok ? 'faixa ok' : 'faixa erro';
    faixaA.replaceChildren(atualizacao.mensagem,
      el('button', { type: 'button', class: 'link', onclick: () => { app.avisosVistos.add('a' + atualizacao.fim); faixaA.classList.add('oculto'); } }, 'Fechar'));
  } else {
    faixaA.classList.add('oculto');
  }

  // Envio
  const faixaE = $('faixa-envio');
  const enviando = envio && !envio.fim;
  if (enviando) {
    faixaE.className = 'faixa';
    faixaE.textContent = `Enviando ao GestaoRevenda… ${numero(envio.enviadas)} de ${numero(envio.total)} empresas.`;
  } else if (envio && envio.fim && !app.avisosVistos.has('e' + envio.fim)) {
    faixaE.className = envio.ok ? 'faixa ok' : 'faixa erro';
    const r = envio.resultado;
    const texto = envio.ok
      ? `Envio concluído: ${resumoEnvio(r)}.`
      : `${envio.erro || 'O envio falhou.'}${r ? ` Até parar: ${resumoEnvio(r)}.` : ''}`;
    faixaE.replaceChildren(texto,
      el('button', { type: 'button', class: 'link', onclick: () => { app.avisosVistos.add('e' + envio.fim); faixaE.classList.add('oculto'); } }, 'Fechar'));
  } else {
    faixaE.classList.add('oculto');
  }
  document.querySelectorAll('[data-enviar]').forEach((b) => { b.disabled = Boolean(enviando) || !gestaorevenda.configurado; });

  const avisoGr = $('aviso-gr');
  avisoGr.textContent = gestaorevenda.aviso || '';
  avisoGr.classList.toggle('oculto', !gestaorevenda.aviso);

  clearTimeout(app.proximoEstado);
  app.proximoEstado = setTimeout(atualizarEstado, rodando || enviando ? 3000 : 30000);
}

function resumoEnvio(r) {
  if (!r) return '';
  return `${numero(r.importadas)} importadas, ${numero(r.ja_existiam)} já existiam no GestaoRevenda, ` +
    `${numero(r.bloqueadas)} bloqueadas, ${numero(r.recusadas)} recusadas`;
}

async function atualizarBase(forcar = false) {
  try {
    const r = await api('/api/atualizar-base', { forcar });
    if (r.ja_atualizada) {
      const sim = await dialogo({
        titulo: 'A base já está em dia',
        corpo: el('p', {}, `A base já é a mais recente da Receita (${r.mes_texto}). Baixar de novo mesmo assim? Leva cerca de 30 minutos.`),
        ok: 'Baixar de novo',
      });
      if (sim) await atualizarBase(true);
      return;
    }
    mensagem(`Atualização da base de ${r.mes_texto} começou. Leva cerca de 30 minutos.`, true);
  } catch (e) {
    mensagem(e.message);
  }
  atualizarEstado();
}

// ── Opções (atividades, cidades, situações) ──────────────────────────────────

async function carregarOpcoes() {
  try {
    app.opcoes = await api('/api/opcoes');
  } catch (e) {
    mensagem(e.message);
    return;
  }
  $('uf').textContent = app.opcoes.uf || '—';
  const sit = $('situacoes');
  if (!sit.children.length) {
    for (const s of app.opcoes.situacoes) {
      sit.append(el('label', { class: 'opcao' },
        el('input', { type: 'checkbox', name: 'situacao', value: s.codigo, checked: s.codigo === '02' }), s.nome));
    }
  }
  app.opcoes.cnaesBusca = app.opcoes.cnaes.map((c) => ({ ...c, chave: `${c.codigo} ${formatarCnae(c.codigo)} ${semAcento(c.descricao)}` }));
  app.opcoes.cidadesBusca = app.opcoes.municipios.map((m) => ({ ...m, chave: semAcento(m.nome) }));
}

function montarSeletor({ entrada, lista, chips, escolhidos, buscar, rotulo }) {
  let itens = [];
  let foco = -1;
  const dica = chips.parentElement.querySelector('small');
  const desenharChips = () => {
    if (dica) dica.classList.toggle('oculto', escolhidos.size > 0);
    chips.replaceChildren(...[...escolhidos].map(([codigo, texto]) =>
      el('span', { class: 'chip', title: texto },
        el('span', {}, texto),
        el('button', { type: 'button', 'aria-label': `Tirar ${texto}`, onclick: () => { escolhidos.delete(codigo); desenharChips(); } }, '×'))));
  };
  const fechar = () => { lista.classList.add('oculto'); foco = -1; };
  const escolher = (item) => {
    escolhidos.set(item.codigo, rotulo(item));
    entrada.value = '';
    fechar();
    desenharChips();
    entrada.focus();
  };
  const desenhar = () => {
    const termo = semAcento(entrada.value.trim());
    if (!termo || !app.opcoes) return fechar();
    itens = buscar(termo).filter((i) => !escolhidos.has(i.codigo)).slice(0, 40);
    foco = itens.length ? 0 : -1;
    lista.replaceChildren(...(itens.length
      ? itens.map((item, i) => el('li', { class: i === foco ? 'foco' : '', onmousedown: (ev) => { ev.preventDefault(); escolher(item); } }, ...rotuloSugestao(item)))
      : [el('li', { class: 'vazio' }, 'Nada encontrado')]));
    lista.classList.remove('oculto');
  };
  entrada.addEventListener('input', desenhar);
  entrada.addEventListener('blur', () => setTimeout(fechar, 100));
  entrada.addEventListener('keydown', (ev) => {
    if (lista.classList.contains('oculto') || !itens.length) return;
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault();
      foco = (foco + (ev.key === 'ArrowDown' ? 1 : -1) + itens.length) % itens.length;
      [...lista.children].forEach((li, i) => li.classList.toggle('foco', i === foco));
      lista.children[foco].scrollIntoView({ block: 'nearest' });
    } else if (ev.key === 'Enter') {
      ev.preventDefault();
      if (foco >= 0) escolher(itens[foco]);
    } else if (ev.key === 'Escape') {
      fechar();
    }
  });
  return desenharChips;
}

function rotuloSugestao(item) {
  return item.descricao !== undefined
    ? [el('span', { class: 'codigo' }, formatarCnae(item.codigo)), item.descricao]
    : [item.nome];
}

const rotuloCnae = (c) => `${formatarCnae(c.codigo)} ${c.descricao}`;
const desenharCnaes = montarSeletor({
  entrada: $('cnae-busca'), lista: $('cnae-sugestoes'), chips: $('cnae-chips'), escolhidos: app.cnaes,
  rotulo: rotuloCnae,
  buscar: (termo) => {
    const digitos = termo.replace(/\D/g, '');
    const palavras = termo.split(/\s+/);
    return app.opcoes.cnaesBusca.filter((c) =>
      (digitos.length >= 2 && c.codigo.startsWith(digitos)) || palavras.every((p) => c.chave.includes(p)));
  },
});
const desenharCidades = montarSeletor({
  entrada: $('cidade-busca'), lista: $('cidade-sugestoes'), chips: $('cidade-chips'), escolhidos: app.cidades,
  rotulo: (m) => m.nome,
  buscar: (termo) => app.opcoes.cidadesBusca.filter((m) => m.chave.includes(termo))
    .sort((a, b) => (b.chave.startsWith(termo) - a.chave.startsWith(termo)) || a.chave.localeCompare(b.chave)),
});

// ── Filtros ↔ formulário ─────────────────────────────────────────────────────

function valorMonetario(texto) {
  const t = String(texto || '').trim().replace(/[R$\s]/g, '');
  if (!t) return null;
  return t.includes(',') ? t.replace(/\./g, '').replace(',', '.') : t.replace(/\.(?=\d{3}(\D|$))/g, '');
}

function lerFiltros() {
  const periodo = document.querySelector('input[name=abertura]:checked').value === 'periodo';
  const capital = $('capital-ligado').checked;
  return {
    cnaes: [...app.cnaes.keys()],
    municipios: [...app.cidades.keys()],
    abertura_de: periodo ? $('abertura-de').value || null : null,
    abertura_ate: periodo ? $('abertura-ate').value || null : null,
    situacoes: [...document.querySelectorAll('input[name=situacao]:checked')].map((i) => i.value),
    matriz_filial: document.querySelector('input[name=matriz]:checked').value,
    capital_ligado: capital,
    capital_min: capital ? valorMonetario($('capital-min').value) : null,
    capital_max: capital ? valorMonetario($('capital-max').value) : null,
    excluir_mei: $('excluir-mei').checked,
  };
}

function aplicarFiltros(f) {
  app.cnaes.clear();
  const nomesCnae = new Map((app.opcoes?.cnaes || []).map((c) => [c.codigo, c]));
  for (const c of f.cnaes || []) app.cnaes.set(c, nomesCnae.has(c) ? rotuloCnae(nomesCnae.get(c)) : formatarCnae(c));
  app.cidades.clear();
  const nomesCidade = new Map((app.opcoes?.municipios || []).map((m) => [m.codigo, m.nome]));
  for (const m of f.municipios || []) app.cidades.set(m, nomesCidade.get(m) || m);
  desenharCnaes();
  desenharCidades();
  const periodo = Boolean(f.abertura_de || f.abertura_ate);
  document.querySelector(`input[name=abertura][value=${periodo ? 'periodo' : 'todas'}]`).checked = true;
  $('abertura-de').value = f.abertura_de || '';
  $('abertura-ate').value = f.abertura_ate || '';
  $('periodo').classList.toggle('oculto', !periodo);
  const sits = new Set(f.situacoes || []);
  document.querySelectorAll('input[name=situacao]').forEach((i) => { i.checked = sits.has(i.value); });
  document.querySelector(`input[name=matriz][value=${f.matriz_filial || 'todas'}]`).checked = true;
  $('capital-ligado').checked = Boolean(f.capital_ligado);
  $('capital').classList.toggle('oculto', !f.capital_ligado);
  $('capital-min').value = f.capital_min != null ? Number(f.capital_min).toLocaleString('pt-BR') : '';
  $('capital-max').value = f.capital_max != null ? Number(f.capital_max).toLocaleString('pt-BR') : '';
  $('excluir-mei').checked = Boolean(f.excluir_mei);
}

// ── Ver resultado ────────────────────────────────────────────────────────────

async function verResultado() {
  const botao = $('btn-ver');
  botao.disabled = true;
  botao.textContent = 'Buscando…';
  try {
    app.ultimoResultado = await api('/api/consultar', { filtros: lerFiltros() });
    desenharResultado(app.ultimoResultado);
  } catch (e) {
    mensagem(e.message);
  } finally {
    botao.disabled = false;
    botao.textContent = 'Ver resultado';
  }
}

function desenharResultado(r) {
  const c = r.contagens;
  const caixa = $('resultado');
  const configurado = app.estado?.gestaorevenda?.configurado;
  const enviando = app.estado?.envio && !app.estado.envio.fim;
  const tabela = el('table', {},
    el('thead', {}, el('tr', {}, ['', 'CNPJ', 'Nome', 'Atividade', 'Cidade', 'Abertura', 'Capital', 'MEI', 'Telefone', 'E-mail'].map((t) => el('th', {}, t)))),
    el('tbody', {}, r.previa.map((l) => el('tr', { class: l.ja_enviada ? 'enviada' : '' },
      el('td', {}, el('span', { class: l.ja_enviada ? 'etiqueta' : 'etiqueta nova' }, l.ja_enviada ? 'já enviada' : 'nova')),
      el('td', {}, l.cnpj),
      el('td', {}, l.nome, l.fantasia && l.fantasia !== l.nome ? el('div', { class: 'resumo-filtros' }, l.fantasia) : null),
      el('td', {}, l.atividade),
      el('td', {}, l.cidade || '—'),
      el('td', {}, dataBR(l.abertura)),
      el('td', { class: 'num' }, reais(l.capital)),
      el('td', {}, l.mei ? 'Sim' : 'Não'),
      el('td', {}, l.telefone || '—'),
      el('td', {}, l.email || '—')))));
  caixa.replaceChildren(
    el('h2', {}, `Resultado na base de ${r.mes_texto}`),
    el('p', { class: 'resumo-filtros' }, r.resumo),
    el('div', { class: 'numeros' },
      el('div', { class: 'numero' }, el('strong', {}, numero(c.encontradas)), 'empresas encontradas'),
      el('div', { class: 'numero' }, el('strong', {}, numero(c.ja_enviadas)), 'já enviadas antes'),
      el('div', { class: 'numero destaque' }, el('strong', {}, numero(c.novas)), 'novas')),
    c.novas ? el('p', { class: 'detalhe-novas' },
      `Entre as novas: ${numero(c.novas_mei)} são MEI e ${numero(c.novas_sem_contato)} não têm telefone nem e-mail.`) : null,
    el('div', { class: 'botoes' },
      el('button', { type: 'button', class: 'principal verde', 'data-enviar': true, disabled: !c.novas || !configurado || enviando, onclick: confirmarEnvio },
        c.novas ? `Enviar ao GestaoRevenda (${numero(c.novas)} novas)` : 'Nada novo para enviar'),
      el('button', { type: 'button', class: 'secundario', disabled: !c.encontradas, onclick: baixarExcel }, 'Baixar em Excel')),
    c.encontradas
      ? el('div', {},
        el('p', { class: 'dica' }, c.encontradas > r.previa.length
          ? `Prévia das ${numero(r.previa.length)} primeiras (as novas aparecem primeiro). O Excel traz todas.`
          : 'Todas as empresas encontradas:'),
        el('div', { class: 'rolagem' }, tabela))
      : el('p', {}, 'Nenhuma empresa com esses filtros.'),
  );
  caixa.classList.remove('oculto');
  caixa.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function baixarExcel(ev) {
  const botao = ev.currentTarget;
  botao.disabled = true;
  botao.textContent = 'Gerando o Excel…';
  try {
    const resp = await api('/api/excel', { filtros: app.ultimoResultado.filtros });
    const blob = await resp.blob();
    const nome = /filename="([^"]+)"/.exec(resp.headers.get('Content-Disposition') || '')?.[1] || 'prospeccao.xlsx';
    const url = URL.createObjectURL(blob);
    const a = el('a', { href: url, download: nome });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  } catch (e) {
    mensagem(e.message);
  } finally {
    botao.disabled = false;
    botao.textContent = 'Baixar em Excel';
  }
}

// ── Enviar ───────────────────────────────────────────────────────────────────

async function confirmarEnvio() {
  const r = app.ultimoResultado;
  const c = r.contagens;
  const limite = app.estado?.limite_confirmacao ?? 500;
  const detalhes = el('ul', {},
    el('li', {}, `${numero(c.novas_mei)} são MEI`),
    el('li', {}, `${numero(c.novas_sem_contato)} não têm telefone nem e-mail`),
    c.ja_enviadas ? el('li', {}, `${numero(c.ja_enviadas)} já enviadas antes ficam de fora`) : null);
  const primeira = await dialogo({
    titulo: `Enviar ${numero(c.novas)} empresas novas ao GestaoRevenda?`,
    corpo: [el('p', {}, r.resumo), detalhes, el('p', {}, 'Elas entram em Leads com origem "Receita Federal". Quem já existir lá não é duplicado.')],
    ok: c.novas > limite ? 'Continuar' : 'Enviar',
    okClasse: 'principal verde',
  });
  if (!primeira) return;
  let confirmacao = null;
  if (c.novas > limite) {
    const campo = el('input', { type: 'text', inputmode: 'numeric', 'aria-label': 'Quantidade' });
    const promessa = dialogo({
      titulo: 'Confirme a quantidade',
      corpo: [
        el('p', { class: 'atencao' }, `São ${numero(c.novas)} empresas, mais de ${numero(limite)}.`),
        el('p', {}, `Para confirmar, digite o número ${c.novas} (sem pontos):`),
        campo,
      ],
      ok: 'Enviar',
      okHabilitado: false,
      okClasse: 'principal verde',
    });
    campo.addEventListener('input', () => { $('dlg-ok').disabled = campo.value.trim() !== String(c.novas); });
    setTimeout(() => campo.focus(), 50);
    if (!(await promessa)) return;
    confirmacao = campo.value.trim();
  }
  try {
    await api('/api/enviar', {
      filtros: r.filtros, quantidade: c.novas, mes_base: r.mes_base, varredura_id: r.varredura_id, confirmacao,
    });
    mensagem(`Envio de ${numero(c.novas)} empresas começou. O andamento aparece no alto da página.`, true);
    document.querySelectorAll('[data-enviar]').forEach((b) => { b.disabled = true; });
    esperarFimDoEnvio();
  } catch (e) {
    mensagem(e.message);
  }
  atualizarEstado();
}

async function esperarFimDoEnvio() {
  await new Promise((ok) => setTimeout(ok, 3000));
  await atualizarEstado();
  const envio = app.estado?.envio;
  if (envio && !envio.fim) return esperarFimDoEnvio();
  if (envio) {
    await dialogo({
      titulo: envio.ok ? 'Envio concluído' : 'O envio parou',
      corpo: [
        envio.ok ? null : el('p', { class: 'atencao' }, envio.erro),
        envio.resultado ? el('ul', {},
          el('li', {}, `${numero(envio.resultado.importadas)} importadas`),
          el('li', {}, `${numero(envio.resultado.ja_existiam)} já existiam no GestaoRevenda`),
          el('li', {}, `${numero(envio.resultado.bloqueadas)} bloqueadas`),
          el('li', {}, `${numero(envio.resultado.recusadas)} recusadas`)) : null,
      ],
      cancelar: null,
    });
    app.avisosVistos.add('e' + envio.fim);
    atualizarEstado();
    if (app.ultimoResultado) verResultado(); // as enviadas passam a contar como "já enviadas"
  }
}

// ── Histórico ────────────────────────────────────────────────────────────────

async function carregarHistorico() {
  const tabela = $('tabela-historico');
  let itens;
  try {
    itens = await api('/api/historico');
  } catch (e) {
    mensagem(e.message);
    return;
  }
  if (!itens.length) {
    tabela.replaceChildren(el('tbody', {}, el('tr', {}, el('td', {}, 'Nenhuma busca ainda.'))));
    return;
  }
  tabela.replaceChildren(
    el('thead', {}, el('tr', {}, ['Data', 'Base', 'Filtros', 'Encontradas', 'Novas', 'Envio', ''].map((t) => el('th', {}, t)))),
    el('tbody', {}, itens.map((v) => el('tr', {},
      el('td', {}, dataHoraBR(v.em)),
      el('td', {}, v.mes_base || '—'),
      el('td', { class: 'resumo-filtros' }, v.resumo),
      el('td', { class: 'num' }, numero(v.encontradas)),
      el('td', { class: 'num' }, numero(v.novas)),
      el('td', {}, v.envio ? `${v.envio.ok ? '' : 'Parou no meio. '}${resumoEnvio(v.envio)}` : '—'),
      el('td', {}, el('button', { type: 'button', class: 'secundario', onclick: () => repetir(v.filtros) }, 'Repetir'))))));
}

async function repetir(filtros) {
  trocarAba('busca');
  aplicarFiltros(filtros);
  await verResultado();
}

// ── Abas e eventos ───────────────────────────────────────────────────────────

function trocarAba(aba) {
  document.querySelectorAll('.abas button').forEach((b) => b.classList.toggle('ativa', b.dataset.aba === aba));
  $('aba-busca').classList.toggle('oculto', aba !== 'busca');
  $('aba-historico').classList.toggle('oculto', aba !== 'historico');
  if (aba === 'historico') carregarHistorico();
}

document.querySelectorAll('.abas button').forEach((b) => b.addEventListener('click', () => trocarAba(b.dataset.aba)));
$('btn-atualizar').addEventListener('click', () => atualizarBase(false));
$('form-filtros').addEventListener('submit', (ev) => { ev.preventDefault(); verResultado(); });
$('btn-limpar').addEventListener('click', () => aplicarFiltros({ situacoes: ['02'] }));
document.querySelectorAll('input[name=abertura]').forEach((i) => i.addEventListener('change', () => {
  $('periodo').classList.toggle('oculto', document.querySelector('input[name=abertura]:checked').value !== 'periodo');
}));
$('capital-ligado').addEventListener('change', () => $('capital').classList.toggle('oculto', !$('capital-ligado').checked));
document.querySelectorAll('[data-meses]').forEach((b) => b.addEventListener('click', () => {
  const hoje = new Date();
  const de = new Date(hoje);
  de.setMonth(de.getMonth() - Number(b.dataset.meses));
  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  $('abertura-de').value = iso(de);
  $('abertura-ate').value = iso(hoje);
}));

atualizarEstado();
