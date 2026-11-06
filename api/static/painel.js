"use strict";
/* Painel de transmissão do rePET. Sem dependências.
 * Regra de segurança: todo texto vindo da API entra por textContent/createTextNode, nunca por innerHTML.
 * Rotas usadas: /status (a cada 2 s), /broadcast/*, /live/*, e o destino da transmissão:
 *   GET /quadras/{id}/youtube          resumo (key mascarada, URL efetiva, hosts permitidos)
 *   GET/PUT /quadras/{id}/youtube/key  a key inteira / troca da key
 *   PUT /quadras/{id}/youtube/url      troca da URL RTMP (null volta à global)
 *   GET/PUT /broadcast/{id}/audio      corte de agudos e redução de ruído (valem ao vivo, sem relançar)
 */

const POLL_MS = 2000;
const REVEAL_MS = 30000;                      // a key mostrada some sozinha depois disto
const VOLUME_MAX = 100;                       // teto do volume no painel: 100% é o volume original da câmera
const VOLUME_STEP = 10;                       // passo dos botões − e +
const KEY_RE = /^[A-Za-z0-9_-]{8,128}$/;      // o mesmo formato que a API aceita
const URL_RE = /^rtmp:\/\/\S+$/i;
const $ = (sel, root = document) => root.querySelector(sel);
const cards = new Map();                      // quadra_id -> cartão
let pollTimer = null;
let overview = null;
let lastOk = 0;
let falhando = false;

// --- ícones (SVG montado pelo DOM: a CSP não deixa estilo/imagem inline) ------------

const SVG_NS = "http://www.w3.org/2000/svg";
const ICONS = {
  clock: [["circle", { cx: 12, cy: 12, r: 9 }], ["path", { d: "M12 7v5l3 2" }]],
  volume: [["path", { d: "M4 9v6h4l5 4V5L8 9H4z" }], ["path", { d: "M16.5 9a4 4 0 0 1 0 6" }]],
  mute: [["path", { d: "M4 9v6h4l5 4V5L8 9H4z" }], ["path", { d: "M17 9l4 6M21 9l-4 6" }]],
  key: [["circle", { cx: 8, cy: 15, r: 4 }], ["path", { d: "M11 12l9-9M16 7l3 3M14 9l2 2" }]],
  link: [["path", { d: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1" }], ["path", { d: "M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1" }]],
  eye: [["path", { d: "M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z" }], ["circle", { cx: 12, cy: 12, r: 3 }]],
  eyeoff: [["path", { d: "M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z" }], ["circle", { cx: 12, cy: 12, r: 3 }], ["path", { d: "M3 3l18 18" }]],
  lock: [["rect", { x: 5, y: 11, width: 14, height: 9, rx: 2 }], ["path", { d: "M8 11V8a4 4 0 0 1 8 0v3" }]],
  alert: [["path", { d: "M12 3l10 18H2L12 3z" }], ["path", { d: "M12 10v5M12 18v.5" }]],
  check: [["path", { d: "M5 12l5 5 9-10" }]],
  signal: [["path", { d: "M5 12a7 7 0 0 1 14 0" }], ["path", { d: "M8.5 12a3.5 3.5 0 0 1 7 0" }], ["circle", { cx: 12, cy: 12, r: 1 }]],
  grid: [["rect", { x: 4, y: 4, width: 7, height: 7, rx: 1 }], ["rect", { x: 13, y: 4, width: 7, height: 7, rx: 1 }], ["rect", { x: 4, y: 13, width: 7, height: 7, rx: 1 }], ["rect", { x: 13, y: 13, width: 7, height: 7, rx: 1 }]],
  sun: [["circle", { cx: 12, cy: 12, r: 4 }], ["path", { d: "M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M5 19l1.5-1.5M17.5 6.5L19 5" }]],
  moon: [["path", { d: "M21 13A9 9 0 1 1 11 3a7 7 0 0 0 10 10z" }]],
};

function icon(name) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "ico");
  svg.setAttribute("aria-hidden", "true");
  for (const [tag, attrs] of ICONS[name]) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    svg.append(node);
  }
  return svg;
}

// --- utilidades -------------------------------------------------------------------

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function fmtDuration(total) {
  if (total == null) return "–";
  const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
  return h > 0 ? `${h}h${String(m).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

let toastTimer = null;
function toast(message, ok = false) {
  const box = $("#toast");
  box.replaceChildren(icon(ok ? "check" : "alert"), el("span", "", message));
  box.className = ok ? "toast ok" : "toast";
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, ok ? 2200 : 6000);
}

function errorText(data, status) {
  const d = data && data.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d) && d.length) return "Dados inválidos: " + d.map((x) => x.msg).join("; ");
  return `Erro ${status}`;
}

async function api(path, method = "GET", body) {
  const opts = { method, credentials: "same-origin", headers: { "X-Painel": "1" } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opts);
  let data = null;
  try { data = await resp.json(); } catch { /* corpo vazio */ }
  if (resp.status === 401) { showLogin(); throw new Error("Sessão expirada."); }
  if (!resp.ok) throw new Error(errorText(data, resp.status));
  return data;
}

/* Janela de confirmação própria (o confirm() do navegador some em alguns celulares e não dá para estilizar). */
function confirmar({ titulo, texto, ok = "Confirmar", perigo = false }) {
  const dlg = $("#dlg");
  if (typeof dlg.showModal !== "function") return Promise.resolve(window.confirm(`${titulo}\n\n${texto}`));
  $("#dlg-titulo").textContent = titulo;
  $("#dlg-texto").textContent = texto;
  $("#dlg-ok").textContent = ok;
  dlg.classList.toggle("perigo", perigo);
  return new Promise((resolve) => {
    const onClose = () => { dlg.removeEventListener("close", onClose); resolve(dlg.returnValue === "ok"); };
    dlg.addEventListener("close", onClose);
    dlg.returnValue = "";
    dlg.showModal();
  });
}

/* Botão que se desabilita enquanto a chamada roda. `setDisabled` é a trava vinda das regras do cartão:
 * terminar uma chamada não pode reabilitar um botão que o cartão manteve travado. */
function button(label, cls, onclick, ico) {
  const b = el("button", cls);
  b.type = "button";
  setLabel(b, label, ico);
  b.addEventListener("click", async () => {
    b.dataset.ocupado = "1";
    b.disabled = true;
    try { await onclick(); } catch (e) { toast(e.message); } finally {
      b.dataset.ocupado = "";
      b.disabled = b.dataset.travado === "1";
    }
  });
  return b;
}
function setLabel(b, label, ico) {
  b.replaceChildren(...(ico ? [icon(ico)] : []), ...(label ? [document.createTextNode(label)] : []));
}
function setDisabled(b, value) {
  b.dataset.travado = value ? "1" : "";
  b.disabled = Boolean(value) || b.dataset.ocupado === "1";
}

// --- tema -------------------------------------------------------------------------

function temaAtual() {
  return document.documentElement.dataset.tema || (matchMedia("(prefers-color-scheme: light)").matches ? "claro" : "escuro");
}
function pintarBotaoTema() {
  $("#tema").replaceChildren(icon(temaAtual() === "claro" ? "moon" : "sun"));
}
$("#tema").addEventListener("click", () => {
  const novo = temaAtual() === "claro" ? "escuro" : "claro";
  document.documentElement.dataset.tema = novo;
  try { localStorage.setItem("repet-tema", novo); } catch { /* modo privado */ }
  pintarBotaoTema();
});
try {
  const salvo = localStorage.getItem("repet-tema");
  if (salvo === "claro" || salvo === "escuro") document.documentElement.dataset.tema = salvo;
} catch { /* sem localStorage */ }
pintarBotaoTema();

// --- login ------------------------------------------------------------------------

function showLogin() {
  clearInterval(pollTimer);
  for (const card of cards.values()) clearTimeout(card.dest.revealTimer);
  cards.clear();
  $("#quadras").replaceChildren();   // nada de key ou placar fica na tela de quem saiu
  overview = null;
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#usuario").focus();
}

async function showApp(user) {
  $("#login").hidden = true;
  $("#app").hidden = false;
  $("#usuario-logado").textContent = user;
  await refresh();
  startPolling();
}

function startPolling() {
  clearInterval(pollTimer);
  pollTimer = setInterval(refresh, POLL_MS);
}

$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const erro = $("#login-erro");
  erro.hidden = true;
  try {
    const resp = await fetch("/painel/login", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ usuario: $("#usuario").value, senha: $("#senha").value }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(errorText(data, resp.status));
    $("#senha").value = "";
    await showApp(data.usuario);
  } catch (e) {
    erro.textContent = e.message;
    erro.hidden = false;
  }
});

$("#sair").addEventListener("click", async () => {
  try { await api("/painel/logout", "POST"); } catch { /* já saiu */ }
  showLogin();
});

// Aba em segundo plano não precisa consultar a API a cada 2 s.
document.addEventListener("visibilitychange", () => {
  if ($("#app").hidden) return;
  if (document.hidden) clearInterval(pollTimer);
  else { refresh(); startPolling(); }
});

// --- cartão: aba "Transmissão" ----------------------------------------------------

function buildScoreboard(card) {
  const sb = el("div", "sb");
  const cab = el("div", "sb-cab");
  cab.append(el("span"), el("span", "", "SETS"), el("span", "", "PONTOS"));
  sb.append(cab);
  for (const side of ["a", "b"]) {
    const lin = el("div", "sb-lin");
    card[`nome${side}`] = el("span", "nome");
    card[`sets${side}`] = el("span", "sets", "0");
    card[`pontos${side}`] = el("span", "pontos", "0");
    lin.append(card[`nome${side}`], card[`sets${side}`], card[`pontos${side}`]);
    sb.append(lin);
  }
  sb.append(el("p", "sb-rodape", "Assim aparece na transmissão"));
  return sb;
}

function buildControls(card, side) {
  const q = card.id;
  const box = el("div", "ctrl");
  card[`tit${side}`] = el("h3");
  const call = (path) => async () => { applyPlacar(card, await api(`/broadcast/${q}/${path}`, "POST")); };
  const par = el("div", "par");
  const menosPonto = button("− ponto", "secondary", call(`ponto/${side}?delta=-1`));
  menosPonto.title = "Desfaz um ponto";
  const menosSet = button("− set", "secondary", call(`set/${side}?delta=-1`));
  menosSet.title = "Tira um set (não mexe nos pontos)";
  par.append(menosPonto, menosSet);
  const fechar = button("Fechar set", "set", async () => {
    const nome = card[`nome${side}`].textContent;
    const sim = await confirmar({
      titulo: `Fechar o set para ${nome}?`,
      texto: "Soma 1 set e zera os pontos dos dois lados.",
      ok: "Fechar set",
    });
    if (sim) applyPlacar(card, await api(`/broadcast/${q}/set/${side}`, "POST"));
  });
  box.append(card[`tit${side}`], button("+1 ponto", "ok mais", call(`ponto/${side}`)), par, fechar);
  return box;
}

function buildVolume(card) {
  const q = card.id;
  const wrap = el("div", "stack");
  const vol = el("div", "vol");
  card.btnVolMenos = button("−", "", async () => {
    await enviar(Math.min(VOLUME_MAX, atual() - VOLUME_STEP));   // acima do teto (valor posto pela API), o − já volta ao teto
  });
  card.btnVolMais = button("+", "", async () => {
    if (atual() >= VOLUME_MAX) return;                           // no teto: não chama a API
    await enviar(Math.min(VOLUME_MAX, atual() + VOLUME_STEP));
  });
  card.btnVolMenos.setAttribute("aria-label", "Diminuir volume");
  card.btnVolMais.setAttribute("aria-label", "Aumentar volume");
  card.volIcon = el("span");
  card.volIcon.append(icon("volume"));
  card.volRange = el("input");
  Object.assign(card.volRange, { type: "range", min: 0, max: VOLUME_MAX, step: 5, value: 100 });
  card.volRange.setAttribute("aria-label", "Volume da câmera, em porcentagem");
  card.volOut = el("output", "", "100%");
  const enviar = async (nivel) => { applyPlacar(card, await api(`/broadcast/${q}/volume`, "PUT", { nivel: Math.max(0, Math.min(VOLUME_MAX, nivel)) })); };
  // Os botões calculam o valor novo aqui (e usam PUT) em vez de chamar volume/up|down, que somam um passo fixo
  // no servidor e passariam do teto (95% + 10 = 105%).
  const atual = () => (card.ultimoPlacar ? card.ultimoPlacar.volume : Number(card.volRange.value));
  card.volRange.addEventListener("input", () => {
    card.volEditando = true;
    card.volOut.textContent = `${card.volRange.value}%`;
    setDisabled(card.btnVolMais, Number(card.volRange.value) >= VOLUME_MAX);
  });
  card.volRange.addEventListener("change", async () => {
    try { await enviar(Number(card.volRange.value)); } catch (e) { toast(e.message); } finally { card.volEditando = false; }
  });
  vol.append(
    card.volIcon, card.volRange, card.volOut,
    card.btnVolMenos,
    card.btnVolMais,
  );
  wrap.append(vol, el("p", "hint", "100% é o volume original da câmera e o máximo permitido no painel."));
  return wrap;
}

function numField(label) {
  const wrap = el("label", "", label);
  const input = el("input");
  Object.assign(input, { type: "number", min: 0, step: 1, inputMode: "numeric" });
  wrap.append(input);
  return { wrap, input };
}

function buildCorrecoes(card) {
  const q = card.id;
  const det = el("details");
  det.append(el("summary", "", "Nomes e correções"));
  const box = el("div", "stack");

  const nomes = el("div", "nomes");
  card.edA = el("input"); card.edA.maxLength = 14; card.edA.setAttribute("aria-label", "Nome da dupla A");
  card.edB = el("input"); card.edB.maxLength = 14; card.edB.setAttribute("aria-label", "Nome da dupla B");
  nomes.append(card.edA, card.edB);
  const salvarNomes = button("Salvar nomes", "secondary", async () => {
    applyPlacar(card, await api(`/broadcast/${q}/nomes`, "PUT", { nome_a: card.edA.value, nome_b: card.edB.value }));
    toast("Nomes salvos.", true);
  });

  const nums = el("div", "nomes");
  const f = { pa: numField("Pontos A"), pb: numField("Pontos B"), sa: numField("Sets A"), sb: numField("Sets B") };
  card.fields = f;
  nums.append(f.pa.wrap, f.pb.wrap, f.sa.wrap, f.sb.wrap);
  const aplicar = button("Aplicar correção", "secondary", async () => {
    const lido = {};
    for (const [chave, campo] of [["pontos_a", f.pa], ["pontos_b", f.pb], ["sets_a", f.sa], ["sets_b", f.sb]]) {
      const n = Number(campo.input.value);
      if (campo.input.value === "" || !Number.isInteger(n) || n < 0) throw new Error("Use números inteiros, 0 ou mais.");
      lido[chave] = n;
    }
    applyPlacar(card, await api(`/broadcast/${q}/placar`, "PUT", lido));
    toast("Placar corrigido.", true);
  });
  const zerar = button("Zerar placar", "danger", async () => {
    const sim = await confirmar({ titulo: "Zerar o placar?", texto: "Pontos e sets dos dois lados voltam a 0.", ok: "Zerar", perigo: true });
    if (sim) { applyPlacar(card, await api(`/broadcast/${q}/placar/zerar`, "POST")); fillFields(card); }
  });
  det.addEventListener("toggle", () => { if (det.open) fillFields(card); });
  box.append(nomes, salvarNomes, nums, aplicar, zerar);
  det.append(box);
  return det;
}

function fillFields(card) {
  const p = card.ultimoPlacar;
  if (!p) return;
  card.fields.pa.input.value = p.placar.a.pontos; card.fields.pb.input.value = p.placar.b.pontos;
  card.fields.sa.input.value = p.placar.a.sets; card.fields.sb.input.value = p.placar.b.sets;
}

function buildTransmissao(card) {
  const q = card.id;
  const pan = el("div", "aba");

  // fora do ar
  card.secOff = el("div", "stack");
  const nomes = el("div", "nomes");
  card.inA = el("input"); card.inA.placeholder = "Dupla A"; card.inA.maxLength = 14; card.inA.setAttribute("aria-label", "Nome da dupla A");
  card.inB = el("input"); card.inB.placeholder = "Dupla B"; card.inB.maxLength = 14; card.inB.setAttribute("aria-label", "Nome da dupla B");
  nomes.append(card.inA, card.inB);
  card.btnPlacar = button("Iniciar transmissão com placar", "primary grande", async () => {
    await api(`/broadcast/${q}/start`, "POST", { nome_a: card.inA.value || null, nome_b: card.inB.value || null });
    toast("Transmissão iniciada.", true);
    await refresh();
  }, "signal");
  card.btnDireta = button("Iniciar live direta (sem placar)", "secondary", async () => {
    await api(`/live/${q}`, "POST");
    toast("Live direta iniciada.", true);
    await refresh();
  });
  card.secOff.append(nomes, card.btnPlacar, card.btnDireta);

  // com placar
  card.secPlacar = el("div", "stack");
  const ctrls = el("div", "ctrls");
  ctrls.append(buildControls(card, "a"), buildControls(card, "b"));
  card.btnStopPlacar = button("Encerrar transmissão", "danger", async () => {
    const sim = await confirmar({
      titulo: `Encerrar a transmissão de ${card.titulo.textContent}?`,
      texto: "O vídeo para de ir ao YouTube. O placar e os nomes ficam guardados.",
      ok: "Encerrar", perigo: true,
    });
    if (!sim) return;
    await api(`/broadcast/${q}/stop`, "POST");
    toast("Transmissão encerrada.", true);
    await refresh();
  });
  card.secPlacar.append(buildScoreboard(card), ctrls, buildVolume(card), buildCorrecoes(card), card.btnStopPlacar);

  // direta
  card.secDireta = el("div", "stack");
  card.btnStopDireta = button("Encerrar live direta", "danger", async () => {
    const sim = await confirmar({
      titulo: `Encerrar a live direta de ${card.titulo.textContent}?`,
      texto: "A câmera para de enviar vídeo ao YouTube.", ok: "Encerrar", perigo: true,
    });
    if (!sim) return;
    await api(`/live/${q}/stop`, "POST");
    toast("Live direta encerrada.", true);
    await refresh();
  });
  card.secDireta.append(card.btnStopDireta);

  pan.append(card.secOff, card.secPlacar, card.secDireta);
  return pan;
}

// --- cartão: aba "Destino" (stream key e URL RTMP) ---------------------------------

function blocoTitulo(ico, texto) {
  const tit = el("div", "bloco-tit");
  const h = el("h3");
  h.append(icon(ico), document.createTextNode(texto));
  const tag = el("span", "tag", "carregando…");
  tit.append(h, tag);
  return { tit, tag };
}

function buildDestino(card) {
  const q = card.id;
  const d = card.dest = { data: null, dirtyKey: false, dirtyUrl: false, revealed: false, revealTimer: null, lock: null };
  const pan = el("div", "aba");

  d.lock = el("p", "trava");
  d.lock.append(icon("lock"), el("span", "", "Esta quadra está transmitindo. Encerre a transmissão para trocar a key ou a URL."));
  d.lock.hidden = true;

  // ---- stream key
  const bk = el("section", "bloco");
  const tk = blocoTitulo("key", "Stream key do YouTube");
  d.keyTag = tk.tag;
  d.keyShared = el("p", "hint"); d.keyShared.hidden = true;
  d.keyInput = el("input", "mono");
  Object.assign(d.keyInput, { type: "password", placeholder: "Nova stream key", maxLength: 128, autocomplete: "new-password", spellcheck: false });
  d.keyInput.setAttribute("autocapitalize", "off");
  d.keyInput.setAttribute("aria-label", `Stream key de ${q}`);
  d.keyInput.addEventListener("input", () => { d.dirtyKey = true; hideErr(d.keyErr); });
  d.btnShow = button("Ver atual", "secondary", () => toggleReveal(card), "eye");
  const campo = el("div", "campo-senha");
  campo.append(d.keyInput, d.btnShow);
  d.keyErr = el("p", "erro-inline"); d.keyErr.hidden = true;
  d.btnSaveKey = button("Salvar key", "primary", () => saveKey(card), "check");
  bk.append(tk.tit, d.keyShared, campo,
    el("p", "hint", "8 a 128 caracteres: letras, números, - e _ (sem espaços). A key vale para a live direta e para a transmissão com placar."),
    d.keyErr, d.btnSaveKey);

  // ---- URL RTMP
  const bu = el("section", "bloco");
  const tu = blocoTitulo("link", "Servidor RTMP");
  d.urlTag = tu.tag;
  d.urlInput = el("input", "mono");
  Object.assign(d.urlInput, { type: "text", placeholder: "rtmp://a.rtmp.youtube.com/live2", maxLength: 200, spellcheck: false });
  d.urlInput.setAttribute("inputmode", "url");
  d.urlInput.setAttribute("autocapitalize", "off");
  d.urlInput.setAttribute("aria-label", `URL RTMP de ${q}`);
  d.urlInput.addEventListener("input", () => { d.dirtyUrl = true; hideErr(d.urlErr); });
  d.hosts = el("p", "hint");
  d.urlErr = el("p", "erro-inline"); d.urlErr.hidden = true;
  d.btnSaveUrl = button("Salvar URL", "primary", () => saveUrl(card, d.urlInput.value.trim()), "check");
  d.btnUrlPadrao = button("Usar a padrão", "secondary", () => resetUrl(card));
  const linha = el("div", "linha");
  linha.append(d.btnSaveUrl, d.btnUrlPadrao);
  bu.append(tu.tit, d.urlInput, d.hosts, d.urlErr, linha);

  pan.append(d.lock, bk, bu);
  return pan;
}

function hideErr(p) { p.hidden = true; p.textContent = ""; }
function showErr(p, message) { p.textContent = message; p.hidden = false; }

function hideKey(card) {
  const d = card.dest;
  clearTimeout(d.revealTimer);
  d.keyInput.type = "password";
  if (!d.dirtyKey) d.keyInput.value = "";   // só some o que foi lido; o que a pessoa digitou fica
  d.revealed = false;
  setLabel(d.btnShow, "Ver atual", "eye");
}

async function toggleReveal(card) {
  const d = card.dest;
  hideErr(d.keyErr);
  if (d.revealed) { hideKey(card); return; }
  const r = await api(`/quadras/${card.id}/youtube/key`);   // devolve a key inteira; a API registra a leitura no log
  if (!r.tem_stream_key) { showErr(d.keyErr, "Esta quadra ainda não tem key cadastrada."); return; }
  d.keyInput.value = r.stream_key;
  d.keyInput.type = "text";
  d.dirtyKey = false;
  d.revealed = true;
  setLabel(d.btnShow, "Ocultar", "eyeoff");
  clearTimeout(d.revealTimer);
  d.revealTimer = setTimeout(() => hideKey(card), REVEAL_MS);
}

async function saveKey(card) {
  const d = card.dest;
  hideErr(d.keyErr);
  const valor = d.keyInput.value.trim();
  if (!KEY_RE.test(valor)) {
    showErr(d.keyErr, "Key inválida: use de 8 a 128 caracteres entre letras, números, - e _ (sem espaços).");
    return;
  }
  const sim = await confirmar({
    titulo: `Trocar a stream key de ${card.titulo.textContent}?`,
    texto: "As próximas transmissões desta quadra vão para a key nova. Isso muda para onde o vídeo é enviado.",
    ok: "Trocar key", perigo: true,
  });
  if (!sim) return;
  try {
    const r = await api(`/quadras/${card.id}/youtube/key`, "PUT", { stream_key: valor });
    d.keyInput.value = ""; d.dirtyKey = false;
    hideKey(card);
    toast(r.status === "sem_mudanca" ? "Essa já era a key cadastrada." : "Stream key atualizada.", true);
    await loadDestino(card);
  } catch (e) {
    showErr(d.keyErr, e.message);
    throw e;
  }
}

async function saveUrl(card, valor) {
  const d = card.dest;
  hideErr(d.urlErr);
  if (!URL_RE.test(valor)) { showErr(d.urlErr, "Use o formato rtmp://servidor/aplicação (sem espaços)."); return; }
  try {
    const r = await api(`/quadras/${card.id}/youtube/url`, "PUT", { rtmp_url: valor });
    d.dirtyUrl = false;
    toast(r.status === "sem_mudanca" ? "Essa já era a URL em uso." : "URL RTMP atualizada.", true);
    await loadDestino(card);
  } catch (e) {
    showErr(d.urlErr, e.message);
    throw e;
  }
}

async function resetUrl(card) {
  const d = card.dest;
  hideErr(d.urlErr);
  const sim = await confirmar({
    titulo: "Voltar à URL padrão do servidor?",
    texto: "A quadra deixa de ter uma URL própria e passa a usar a do servidor.", ok: "Usar a padrão",
  });
  if (!sim) return;
  try {
    await api(`/quadras/${card.id}/youtube/url`, "PUT", { rtmp_url: null });
    d.dirtyUrl = false;
    toast("Voltou à URL padrão.", true);
    await loadDestino(card);
  } catch (e) {
    showErr(d.urlErr, e.message);
    throw e;
  }
}

async function loadDestino(card) {
  const d = card.dest;
  try {
    d.data = await api(`/quadras/${card.id}/youtube`);
  } catch (e) {
    d.keyTag.textContent = "erro ao carregar";
    d.keyTag.className = "tag warn";
    showErr(d.keyErr, e.message);
    return;
  }
  renderDestino(card);
}

function nomeDaQuadra(id) {
  const q = overview && overview.quadras.find((x) => x.quadra_id === id);
  return q ? q.nome : id;
}

function renderDestino(card) {
  const d = card.dest, data = d.data;
  if (!data) return;
  d.keyTag.textContent = data.tem_stream_key ? (data.stream_key_final ? `Cadastrada · termina em ${data.stream_key_final}` : "Cadastrada") : "Não cadastrada";
  d.keyTag.className = `tag ${data.tem_stream_key ? "ok" : "warn"}`;
  const outras = data.chave_compartilhada_com;
  d.keyShared.hidden = outras.length === 0;
  d.keyShared.textContent = outras.length ? `Esta key também é usada por ${outras.map(nomeDaQuadra).join(", ")}: só uma quadra por vez pode transmitir com ela.` : "";
  if (!d.dirtyUrl && document.activeElement !== d.urlInput) d.urlInput.value = data.rtmp_url;
  d.urlTag.textContent = data.rtmp_url_padrao ? "Padrão do servidor" : "Própria desta quadra";
  d.urlTag.className = `tag ${data.rtmp_url_padrao ? "" : "ok"}`.trim();
  d.hosts.textContent = `Só rtmp://. Hosts aceitos: ${data.rtmp_hosts_permitidos.join(", ")}.`;
  setDisabled(d.btnUrlPadrao, data.rtmp_url_padrao || card.locked);
  applyLock(card);
}

function applyLock(card) {
  const d = card.dest, locked = Boolean(card.locked);
  d.lock.hidden = !locked;
  d.keyInput.readOnly = locked;
  d.urlInput.readOnly = locked;
  setDisabled(d.btnSaveKey, locked);
  setDisabled(d.btnSaveUrl, locked);
  setDisabled(d.btnUrlPadrao, locked || Boolean(d.data && d.data.rtmp_url_padrao));
}

// --- cartão: aba "Áudio" (corte de agudos e redução de ruído) ---------------------------------------

const CORTE_OPCOES = [
  { v: "off", rot: "Off", desc: "Sem corte de agudos: o chiado agudo passa como a câmera entrega." },
  { v: "suave", rot: "Suave", desc: "Corte em 5 kHz: reduz ~3 dB do agudo. A voz continua intacta." },
  { v: "medio", rot: "Médio", desc: "Corte em 4 kHz: reduz ~6,5 dB do agudo e mantém a voz intacta. Ponto de partida recomendado." },
  { v: "forte", rot: "Forte", desc: "Corte íngreme em 4 kHz: reduz ~11 dB do agudo. O som fica mais abafado (perde o brilho de consoantes e batidas)." },
  { v: "maximo", rot: "Máximo", desc: "Corte íngreme em 3,5 kHz: reduz ~16 dB do agudo. Bem abafado: use só se o chiado for o pior problema." },
];
const RUIDO_OPCOES = [
  { v: "off", rot: "Off", desc: "Sem redução de ruído de fundo e sem corte do grave." },
  { v: "leve", rot: "Leve", desc: "Corta o zumbido grave (< 100 Hz) e rebaixa pouco o ruído de fundo nos intervalos." },
  { v: "medio", rot: "Média", desc: "Corta o zumbido grave e rebaixa o ruído de fundo nos intervalos. É discreto de propósito: não tira chiado que está misturado com o som." },
  { v: "forte", rot: "Forte", desc: "Rebaixa mais, mas pode deixar um som metálico. Para chiado agudo, prefira o corte de agudos." },
];

/* Controle de escolha única (radiogroup). Devolve {root, set(valor), lock(bool)}. */
function buildSegmented(rotulo, opcoes, onPick) {
  const root = el("div", "seg");
  root.setAttribute("role", "radiogroup");
  root.setAttribute("aria-label", rotulo);
  const botoes = new Map();
  for (const o of opcoes) {
    const b = el("button", "", o.rot);
    b.type = "button";
    b.setAttribute("role", "radio");
    b.setAttribute("aria-checked", "false");
    b.addEventListener("click", () => onPick(o.v));
    botoes.set(o.v, b);
    root.append(b);
  }
  return {
    root,
    set(valor) { for (const [v, b] of botoes) { b.setAttribute("aria-checked", String(v === valor)); b.tabIndex = v === valor ? 0 : -1; } },
    lock(travado) { for (const b of botoes.values()) b.disabled = travado; },
  };
}

function buildAudio(card) {
  const d = card.audio = { data: null, busy: false };
  const pan = el("div", "aba");
  pan.append(el("p", "hint", "Ajustes desta quadra. Valem ao vivo, sem derrubar a transmissão, e ficam guardados. Ouça e ajuste: cada passo é reversível."));

  d.estado = el("p", "nota");
  d.estadoTxt = el("span");
  d.estadoIco = el("span");
  d.btnTentar = button("Tentar de novo", "secondary small", () => setAudio(card, { reducao_ruido: d.data.reducao_ruido, corte_agudos: d.data.corte_agudos }));
  d.estado.append(d.estadoIco, d.estadoTxt, d.btnTentar);
  d.estado.hidden = true;

  const bloco = (ico, titulo, opcoes, campo) => {
    const sec = el("section", "bloco");
    const tit = el("div", "bloco-tit");
    const h = el("h3");
    h.append(icon(ico), document.createTextNode(titulo));
    const tag = el("span", "tag", "carregando…");
    tit.append(h, tag);
    const seg = buildSegmented(titulo, opcoes, (v) => {
      if (d.data && d.data[campo] === v && d.data.aplicado_ao_vivo !== false) return;   // já é o escolhido: não chama a API
      setAudio(card, { [campo]: v });
    });
    const desc = el("p", "hint");
    sec.append(tit, seg.root, desc);
    return { sec, tag, seg, desc, opcoes };
  };
  d.corte = bloco("volume", "Corte de agudos", CORTE_OPCOES, "corte_agudos");
  d.ruido = bloco("clock", "Redução de ruído", RUIDO_OPCOES, "reducao_ruido");
  d.btnPadrao = button("Voltar ao padrão do servidor", "secondary", async () => {
    await setAudio(card, { reducao_ruido: d.data.padrao.reducao_ruido, corte_agudos: d.data.padrao.corte_agudos });
  });
  pan.append(d.estado, d.corte.sec, d.ruido.sec, d.btnPadrao);
  return pan;
}

async function loadAudio(card) {
  const d = card.audio;
  try {
    d.data = await api(`/broadcast/${card.id}/audio`);
  } catch (e) {
    d.corte.tag.textContent = "erro ao carregar";
    toast(e.message);
    return;
  }
  renderAudio(card);
}

async function setAudio(card, corpo) {
  const d = card.audio;
  if (d.busy) return;
  d.busy = true;
  d.corte.seg.lock(true); d.ruido.seg.lock(true);
  d.estadoTxt.textContent = "Aplicando…";
  d.estado.className = "nota";
  d.estado.hidden = false;
  d.btnTentar.hidden = true;
  try {
    d.data = await api(`/broadcast/${card.id}/audio`, "PUT", corpo);
    renderAudio(card);
    if (d.data.no_ar && d.data.aplicado_ao_vivo) toast("Áudio ajustado ao vivo.", true);
    else if (!d.data.no_ar) toast("Ajuste guardado: vale quando a transmissão for iniciada.", true);
  } catch (e) {
    toast(e.message);
    await loadAudio(card);
  } finally {
    d.busy = false;
    d.corte.seg.lock(false); d.ruido.seg.lock(false);
  }
}

function renderAudio(card) {
  const d = card.audio, data = d.data;
  if (!data) return;
  for (const [grupo, campo] of [[d.corte, "corte_agudos"], [d.ruido, "reducao_ruido"]]) {
    const v = data[campo];
    grupo.seg.set(v);
    const op = grupo.opcoes.find((o) => o.v === v);
    grupo.desc.textContent = op ? op.desc : "";
    grupo.tag.textContent = v === data.padrao[campo] ? "padrão do servidor" : "personalizado";
    grupo.tag.className = `tag ${v === data.padrao[campo] ? "" : "ok"}`.trim();
  }
  setDisabled(d.btnPadrao, data.corte_agudos === data.padrao.corte_agudos && data.reducao_ruido === data.padrao.reducao_ruido);
  d.estado.hidden = false;
  d.btnTentar.hidden = true;
  if (!data.no_ar) {
    d.estado.className = "nota";
    d.estadoIco.replaceChildren(icon("clock"));
    d.estadoTxt.textContent = "Quadra fora do ar: o ajuste vale quando a transmissão for iniciada.";
  } else if (data.aplicado_ao_vivo) {
    d.estado.className = "nota ok";
    d.estadoIco.replaceChildren(icon("check"));
    d.estadoTxt.textContent = "Aplicado ao vivo agora.";
  } else {
    d.estado.className = "nota warn";
    d.estadoIco.replaceChildren(icon("alert"));
    d.estadoTxt.textContent = "Guardado, mas o ffmpeg no ar não confirmou. Vale quando a transmissão for relançada.";
    d.btnTentar.hidden = false;
  }
}

// --- cartão: montagem e atualização -------------------------------------------------

const ABAS = ["trans", "audio", "dest"];

function buildTabs(card) {
  const tabs = el("div", "tabs");
  tabs.setAttribute("role", "tablist");
  const make = (texto, ico, aba) => {
    const b = el("button", "tab");
    b.type = "button";
    b.setAttribute("role", "tab");
    b.append(icon(ico), document.createTextNode(texto));
    b.addEventListener("click", () => selectTab(card, aba));
    return b;
  };
  card.tabs = { trans: make("Transmissão", "signal", "trans"), audio: make("Áudio", "volume", "audio"), dest: make("Destino", "key", "dest") };
  tabs.addEventListener("keydown", (ev) => {
    if (ev.key !== "ArrowRight" && ev.key !== "ArrowLeft") return;
    const passo = ev.key === "ArrowRight" ? 1 : -1;
    const alvo = ABAS[(ABAS.indexOf(card.aba) + passo + ABAS.length) % ABAS.length];
    selectTab(card, alvo);
    card.tabs[alvo].focus();
  });
  tabs.append(...ABAS.map((a) => card.tabs[a]));
  return tabs;
}

function selectTab(card, which) {
  card.aba = which;
  const paineis = { trans: card.panTrans, audio: card.panAudio, dest: card.panDest };
  for (const a of ABAS) {
    card.tabs[a].setAttribute("aria-selected", String(a === which));
    card.tabs[a].tabIndex = a === which ? 0 : -1;
    paineis[a].hidden = a !== which;
  }
  if (which === "dest") loadDestino(card);
  if (which === "audio") loadAudio(card);
}

function buildCard(quadra) {
  const q = quadra.quadra_id;
  const card = { id: q, root: el("section", "card"), aba: "trans", avisosSig: "" };
  const head = el("header", "card-head");
  const titulo = el("div");
  card.titulo = el("h2");
  card.sub = el("div", "sub");
  titulo.append(card.titulo, card.sub);
  card.badge = el("span", "badge");
  head.append(titulo, card.badge);

  card.chips = el("div", "chips");
  card.progress = el("div", "progress");
  card.bar = el("div", "bar");
  card.progress.append(card.bar);
  card.avisos = el("div", "avisos");

  card.panTrans = buildTransmissao(card);
  card.panAudio = buildAudio(card);
  card.panDest = buildDestino(card);
  card.root.append(head, card.chips, card.progress, card.avisos, buildTabs(card), card.panTrans, card.panAudio, card.panDest);
  for (const a of ABAS) {   // estado inicial das abas, sem disparar carregamento
    const aberta = a === "trans";
    card.tabs[a].setAttribute("aria-selected", String(aberta));
    card.tabs[a].tabIndex = aberta ? 0 : -1;
  }
  card.panAudio.hidden = true;
  card.panDest.hidden = true;
  return card;
}

function applyPlacar(card, p) {
  card.ultimoPlacar = p;
  card.setsa.textContent = p.placar.a.sets; card.pontosa.textContent = p.placar.a.pontos;
  card.setsb.textContent = p.placar.b.sets; card.pontosb.textContent = p.placar.b.pontos;
  card.nomea.textContent = p.nomes.a; card.nomeb.textContent = p.nomes.b;
  card.tita.textContent = p.nomes.a; card.titb.textContent = p.nomes.b;
  if (!card.volEditando) {
    card.volRange.value = Math.min(p.volume, VOLUME_MAX);   // o controle nunca passa do teto
    card.volOut.textContent = `${p.volume}%`;               // mas o número mostra o valor real (a API pode ter posto mais)
    card.volOut.title = p.volume > VOLUME_MAX ? `Acima do máximo do painel (${VOLUME_MAX}%): use − ou o controle para reduzir` : "";
  }
  setDisabled(card.btnVolMais, p.volume >= VOLUME_MAX);
  card.volIcon.replaceChildren(icon(p.audio_da_camera === false ? "mute" : "volume"));
  if (document.activeElement !== card.edA) card.edA.value = p.nomes.a;
  if (document.activeElement !== card.edB) card.edB.value = p.nomes.b;
}

function setBadge(card, text, cls) {
  card.badge.textContent = text;
  card.badge.className = `badge ${cls || ""}`.trim();
}

function chip(ico, text, cls) {
  const c = el("span", `chip ${cls || ""}`.trim());
  c.append(icon(ico), document.createTextNode(text));
  return c;
}

function renderAvisos(card, avisos) {
  const sig = JSON.stringify(avisos.map((a) => [a.tipo, a.msg, a.acao ? a.acao.label : ""]));
  if (sig === card.avisosSig) return;   // só refaz o DOM quando muda: evita roubar o clique de um botão de aviso
  card.avisosSig = sig;
  card.avisos.replaceChildren(...avisos.map((a) => {
    const p = el("p", a.tipo === "erro" ? "aviso erro-aviso" : "aviso");
    p.append(icon("alert"), el("span", "", a.msg));
    if (a.acao) p.append(button(a.acao.label, "secondary small", a.acao.fn));
    return p;
  }));
  card.avisos.hidden = avisos.length === 0;
}

function updateCard(card, q) {
  const p = q.placar, d = q.direta, modo = q.modo;
  card.titulo.textContent = q.nome;
  card.sub.textContent = `${q.esporte ? q.esporte + " · " : ""}${q.quadra_id}`;
  const noAr = modo === "placar" || modo === "direta" || modo === "conflito";
  card.locked = noAr;
  card.root.classList.toggle("no-ar", noAr);
  card.root.classList.toggle("conflito", modo === "conflito");
  card.secOff.hidden = !(modo === "nenhum" || modo === "desconhecido");
  card.secPlacar.hidden = !(modo === "placar" || modo === "conflito");
  card.secDireta.hidden = !(modo === "direta" || modo === "conflito");
  const avisos = [];
  const chips = [];
  let pct = null, nivel = "";

  if (modo === "placar" || modo === "conflito") {
    applyPlacar(card, p);
    chips.push(chip("clock", `no ar há ${fmtDuration(p.no_ar_ha_segundos)}`));
    if (p.encerra_em_segundos != null) {
      const cls = p.encerra_em_segundos < 120 ? "danger" : p.encerra_em_segundos < 600 ? "warn" : "";
      chips.push(chip("clock", `encerra em ${fmtDuration(p.encerra_em_segundos)}`, cls));
      const total = (p.no_ar_ha_segundos || 0) + p.encerra_em_segundos;
      if (total > 0) pct = Math.min(100, ((p.no_ar_ha_segundos || 0) / total) * 100);
      nivel = cls;
    }
    chips.push(p.audio_da_camera === false ? chip("mute", "câmera sem áudio (vai silêncio)", "warn") : chip("volume", "áudio da câmera"));
    if (p.reinicios) chips.push(chip("alert", `${p.reinicios} reinício(s)`, "warn"));
    if (p.transmitindo) setBadge(card, "AO VIVO · placar", "live");
    else setBadge(card, "Conectando…", "connecting");
    if (!p.transmitindo) avisos.push({ tipo: "aviso", msg: `O ffmpeg não está enviando imagem agora (reinícios: ${p.reinicios}). Se persistir, confira a câmera e a stream key.` });
    if (p.travamentos) avisos.push({ tipo: "aviso", msg: `Já travou e foi reiniciado ${p.travamentos}×.` });
  } else if (modo === "direta") {
    setBadge(card, "AO VIVO · direta", "live");
    chips.push(chip("signal", "a câmera envia direto ao YouTube, sem placar"));
  } else if (modo === "desconhecido") {
    setBadge(card, "Câmera sem resposta", "connecting");
    avisos.push({ tipo: "erro", msg: `Não consegui falar com a câmera${d.erro ? `: ${d.erro}` : "."}` });
  } else {
    setBadge(card, "Fora do ar", "");
  }
  if (modo === "conflito") {
    setBadge(card, "CONFLITO", "conflict");
    avisos.push({ tipo: "erro", msg: "Transmissão com placar e live direta ligadas ao mesmo tempo (mesma stream key). Encerre uma das duas." });
  }

  const emUso = q.chave_em_uso_por && !p.no_ar && modo !== "direta" ? q.chave_em_uso_por : null;
  if (emUso) {
    avisos.push({ tipo: "aviso", msg: `A stream key é a mesma de ${nomeDaQuadra(emUso)}, que está no ar. Encerre-a antes de iniciar aqui.` });
  } else if (q.chave_compartilhada_com.length && (modo === "nenhum" || modo === "desconhecido")) {
    chips.push(chip("key", `key compartilhada com ${q.chave_compartilhada_com.map(nomeDaQuadra).join(", ")}: só uma por vez`));
  }
  const semChave = !q.tem_stream_key;
  if (semChave) {
    avisos.push({
      tipo: "aviso", msg: "Esta quadra ainda não tem stream key.",
      acao: { label: "Cadastrar key", fn: async () => { selectTab(card, "dest"); card.dest.keyInput.focus(); } },
    });
  }
  const cheio = overview.transmissoes_com_placar.no_ar >= overview.transmissoes_com_placar.limite && !p.no_ar;
  if (cheio && !semChave) avisos.push({ tipo: "aviso", msg: "Limite de transmissões com placar atingido; encerre outra para iniciar aqui." });
  setDisabled(card.btnPlacar, semChave || cheio || Boolean(emUso));
  setDisabled(card.btnDireta, semChave || Boolean(emUso));

  card.chips.replaceChildren(...chips);
  card.chips.hidden = chips.length === 0;
  card.progress.hidden = pct == null;
  if (pct != null) {
    card.bar.style.width = `${pct.toFixed(1)}%`;   // CSSOM, não o atributo style: a CSP permite
    card.progress.className = `progress ${nivel}`.trim();
  }
  renderAvisos(card, avisos);
  applyLock(card);
  // Aba Áudio aberta: se outro cliente mexeu nos ajustes, ou a transmissão ligou/desligou, recarrega.
  const a = card.audio;
  if (card.aba === "audio" && !a.busy && a.data && (a.data.no_ar !== p.no_ar || a.data.corte_agudos !== p.audio.corte_agudos ||
      a.data.reducao_ruido !== p.audio.reducao_ruido)) loadAudio(card);
}

// --- topo e laço principal ----------------------------------------------------------

function pill(ico, rotulo, valor, cls) {
  const s = el("span", `pill ${cls || ""}`.trim());
  s.append(icon(ico), document.createTextNode(`${rotulo} `), el("b", "", String(valor)));
  return s;
}

function renderTopo(data) {
  const aoVivo = data.quadras.filter((q) => ["placar", "direta", "conflito"].includes(q.modo)).length;
  const lim = data.transmissoes_com_placar;
  $("#pills").replaceChildren(
    pill("signal", "Ao vivo", aoVivo, aoVivo > 0 ? "live" : ""),
    pill("grid", "Com placar", `${lim.no_ar}/${lim.limite}`),
    pill("grid", "Quadras", data.quadras.length),
  );
  document.title = `${aoVivo ? `(${aoVivo} ao vivo) ` : ""}rePET · Painel de transmissão`;
}

function pintarConexao() {
  const box = $("#conexao");
  const t = $("#conexao-txt");
  if (!lastOk) { box.classList.remove("off"); t.textContent = "conectando…"; return; }
  const s = Math.max(0, Math.round((Date.now() - lastOk) / 1000));
  box.classList.toggle("off", falhando);
  t.textContent = falhando ? `sem conexão (último dado há ${s} s)` : s < 3 ? "atualizado agora" : `atualizado há ${s} s`;
}
setInterval(pintarConexao, 1000);

async function refresh() {
  let data;
  try { data = await api("/status"); } catch (e) {
    falhando = true;
    pintarConexao();
    if (!$("#app").hidden) {
      $("#aviso-geral").textContent = `Sem conexão com a API: ${e.message} Mostrando o último estado conhecido.`;
      $("#aviso-geral").hidden = false;
    }
    return;
  }
  if ($("#app").hidden) return;   // saiu/expirou enquanto a resposta vinha: não recria nada por cima do login
  falhando = false;
  lastOk = Date.now();
  pintarConexao();
  $("#aviso-geral").hidden = true;
  overview = data;
  renderTopo(data);
  $("#vazio").hidden = data.quadras.length > 0;
  const container = $("#quadras");
  const ordenadas = [...data.quadras].sort((x, y) => x.nome.localeCompare(y.nome, "pt-BR", { numeric: true }));
  ordenadas.forEach((q, i) => {
    let card = cards.get(q.quadra_id);
    if (!card) {
      card = buildCard(q);
      cards.set(q.quadra_id, card);
    }
    // Só mexe no DOM se a posição mudou: reinserir um nó tira o foco do campo em que se digita.
    if (container.children[i] !== card.root) container.insertBefore(card.root, container.children[i] || null);
    updateCard(card, q);
  });
}

(async function init() {
  try {
    const me = await fetch("/painel/me", { credentials: "same-origin" });
    if (me.ok) { await showApp((await me.json()).usuario); return; }
  } catch { /* cai no login */ }
  showLogin();
})();
