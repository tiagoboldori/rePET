"use strict";
/* Painel de transmissão. Sem dependências. Todo texto vindo da API entra por textContent. */

const POLL_MS = 2000;
const $ = (sel, root = document) => root.querySelector(sel);
const cards = new Map(); // quadra_id -> {el, ...}
let pollTimer = null;
let overview = null;

// --- utilidades -------------------------------------------------------------

function fmtDuration(total) {
  if (total == null) return "–";
  const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
  return h > 0 ? `${h}h${String(m).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

let toastTimer = null;
function toast(message, ok = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = ok ? "toast ok" : "toast";
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, ok ? 2000 : 5000);
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
  if (!resp.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : `Erro ${resp.status}`);
  }
  return data;
}

// --- login ------------------------------------------------------------------

function showLogin() {
  clearInterval(pollTimer);
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#usuario").focus();
}

async function showApp(user) {
  $("#login").hidden = true;
  $("#app").hidden = false;
  $("#usuario-logado").textContent = user;
  await refresh();
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
    if (!resp.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Erro ${resp.status}`);
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

// --- cartões ----------------------------------------------------------------

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function button(label, cls, onclick) {
  const b = el("button", cls, label);
  b.type = "button";
  b.addEventListener("click", async () => {
    b.disabled = true;
    try { await onclick(); } catch (e) { toast(e.message); } finally { b.disabled = false; }
  });
  return b;
}

function buildSide(card, side) {
  const q = card.id;
  const box = el("div", "lado");
  card[`nome${side}`] = el("div", "nome");
  card[`sets${side}`] = el("div", "sets", "0");
  card[`pontos${side}`] = el("div", "pontos", "0");
  const celSets = el("div", "cel"), celPontos = el("div", "cel");
  celSets.append(el("div", "rotulo", "Sets"), card[`sets${side}`]);
  celPontos.append(el("div", "rotulo", "Pontos"), card[`pontos${side}`]);
  const acoes = el("div", "acoes");
  const call = (path, method = "POST") => async () => { applyPlacar(card, await api(`/broadcast/${q}/${path}`, method)); };
  acoes.append(
    button("+1 ponto", "mais", call(`ponto/${side}`)),
    button("−1", "secondary", call(`ponto/${side}?delta=-1`)),
    button("Fechar set", "", async () => {
      const nome = card[`nome${side}`].textContent;
      if (!confirm(`Fechar o set para ${nome}? Os pontos dos dois lados voltam a 0.`)) return;
      applyPlacar(card, await api(`/broadcast/${q}/set/${side}`, "POST"));
    }),
    button("−1 set", "secondary", call(`set/${side}?delta=-1`)),
  );
  box.append(card[`nome${side}`], celSets, celPontos, acoes);
  return box;
}

function buildCard(quadra) {
  const q = quadra.quadra_id;
  const card = { id: q, root: el("section", "card") };
  const head = el("header");
  const titulo = el("div");
  card.titulo = el("h2");
  titulo.append(card.titulo, el("div", "sub", q));
  card.badge = el("span", "badge");
  head.append(titulo, card.badge);
  card.info = el("p", "info");
  card.aviso = el("p", "aviso"); card.aviso.hidden = true;

  // ---- fora do ar
  card.secOff = el("div", "sec");
  const nomes = el("div", "nomes");
  card.inA = el("input"); card.inA.placeholder = "Dupla A"; card.inA.maxLength = 14; card.inA.setAttribute("aria-label", "Nome da dupla A");
  card.inB = el("input"); card.inB.placeholder = "Dupla B"; card.inB.maxLength = 14; card.inB.setAttribute("aria-label", "Nome da dupla B");
  nomes.append(card.inA, card.inB);
  card.btnPlacar = button("Iniciar transmissão com placar", "primary", async () => {
    const body = { nome_a: card.inA.value || null, nome_b: card.inB.value || null };
    await api(`/broadcast/${q}/start`, "POST", body);
    toast("Transmissão iniciada.", true);
    await refresh();
  });
  card.btnDireta = button("Iniciar live direta (sem placar)", "secondary", async () => {
    await api(`/live/${q}`, "POST");
    toast("Live direta iniciada.", true);
    await refresh();
  });
  card.secOff.append(nomes, card.btnPlacar, card.btnDireta);

  // ---- com placar
  card.secPlacar = el("div", "sec");
  const placar = el("div", "placar");
  placar.append(buildSide(card, "a"), buildSide(card, "b"));
  const vol = el("div", "volume");
  card.volOut = el("output", "", "100%");
  vol.append(
    button("−", "", async () => applyPlacar(card, await api(`/broadcast/${q}/volume/down`, "POST"))),
    card.volOut,
    button("+", "", async () => applyPlacar(card, await api(`/broadcast/${q}/volume/up`, "POST"))),
  );
  const det = el("details");
  det.append(el("summary", "", "Nomes e correções"));
  const detBox = el("div", "sec");
  const n2 = el("div", "nomes");
  card.edA = el("input"); card.edA.maxLength = 14; card.edA.setAttribute("aria-label", "Nome da dupla A");
  card.edB = el("input"); card.edB.maxLength = 14; card.edB.setAttribute("aria-label", "Nome da dupla B");
  n2.append(card.edA, card.edB);
  const linha = el("div", "linha");
  linha.append(
    button("Salvar nomes", "secondary", async () => {
      applyPlacar(card, await api(`/broadcast/${q}/nomes`, "PUT", { nome_a: card.edA.value, nome_b: card.edB.value }));
      toast("Nomes salvos.", true);
    }),
    button("Zerar placar", "danger", async () => {
      if (!confirm("Zerar pontos e sets dos dois lados?")) return;
      applyPlacar(card, await api(`/broadcast/${q}/placar/zerar`, "POST"));
    }),
  );
  detBox.append(n2, linha);
  det.append(detBox);
  card.btnStopPlacar = button("Encerrar transmissão", "danger", async () => {
    if (!confirm(`Encerrar a transmissão de ${card.titulo.textContent}?`)) return;
    await api(`/broadcast/${q}/stop`, "POST");
    toast("Transmissão encerrada.", true);
    await refresh();
  });
  card.secPlacar.append(placar, el("div", "sub", "Volume da câmera"), vol, det, card.btnStopPlacar);

  // ---- direta
  card.secDireta = el("div", "sec");
  card.btnStopDireta = button("Encerrar live direta", "danger", async () => {
    if (!confirm(`Encerrar a live direta de ${card.titulo.textContent}?`)) return;
    await api(`/live/${q}/stop`, "POST");
    toast("Live direta encerrada.", true);
    await refresh();
  });
  card.secDireta.append(card.btnStopDireta);

  card.root.append(head, card.info, card.aviso, card.secOff, card.secPlacar, card.secDireta);
  return card;
}

function applyPlacar(card, p) {
  card.setsa.textContent = p.placar.a.sets; card.pontosa.textContent = p.placar.a.pontos;
  card.setsb.textContent = p.placar.b.sets; card.pontosb.textContent = p.placar.b.pontos;
  card.nomea.textContent = p.nomes.a; card.nomeb.textContent = p.nomes.b;
  card.volOut.textContent = `${p.volume}%`;
  if (document.activeElement !== card.edA) card.edA.value = p.nomes.a;
  if (document.activeElement !== card.edB) card.edB.value = p.nomes.b;
}

function setBadge(card, text, cls) {
  card.badge.textContent = text;
  card.badge.className = `badge ${cls || ""}`.trim();
}

function updateCard(card, q) {
  const p = q.placar, d = q.direta;
  card.titulo.textContent = q.nome;
  const modo = q.modo;
  card.secOff.hidden = !(modo === "nenhum" || modo === "desconhecido");
  card.secPlacar.hidden = !(modo === "placar" || modo === "conflito");
  card.secDireta.hidden = !(modo === "direta" || modo === "conflito");
  const avisos = [];

  if (modo === "placar" || modo === "conflito") {
    applyPlacar(card, p);
    const partes = [`no ar há ${fmtDuration(p.no_ar_ha_segundos)}`];
    if (p.encerra_em_segundos != null) partes.push(`encerra em ${fmtDuration(p.encerra_em_segundos)}`);
    partes.push(p.audio_da_camera === false ? "câmera sem áudio (vai silêncio)" : "áudio da câmera");
    card.info.textContent = partes.join(" · ");
    if (p.transmitindo) setBadge(card, "AO VIVO · placar", "live");
    else setBadge(card, "Conectando…", "connecting");
    if (!p.transmitindo) avisos.push(`O ffmpeg não está enviando imagem agora (reinícios: ${p.reinicios}). Se persistir, confira a câmera e a stream key.`);
    if (p.travamentos) avisos.push(`Já travou e foi reiniciado ${p.travamentos}×.`);
  } else if (modo === "direta") {
    setBadge(card, "AO VIVO · direta", "live");
    card.info.textContent = "A câmera envia direto ao YouTube (sem placar).";
  } else if (modo === "desconhecido") {
    setBadge(card, "Câmera sem resposta", "connecting");
    card.info.textContent = "";
    avisos.push(`Não consegui falar com a câmera${d.erro ? `: ${d.erro}` : "."}`);
  } else {
    setBadge(card, "Fora do ar", "");
    card.info.textContent = "";
  }
  if (modo === "conflito") {
    setBadge(card, "CONFLITO", "conflict");
    avisos.push("Transmissão com placar e live direta ligadas ao mesmo tempo (mesma stream key). Encerre uma das duas.");
  }

  const emUso = q.chave_em_uso_por && !p.no_ar && modo !== "direta" ? q.chave_em_uso_por : null;
  if (emUso) {
    const outra = overview.quadras.find((x) => x.quadra_id === emUso);
    avisos.push(`A stream key é a mesma de ${outra ? outra.nome : emUso}, que está no ar. Encerre-a antes de iniciar aqui.`);
  } else if (q.chave_compartilhada_com.length && (modo === "nenhum" || modo === "desconhecido")) {
    const nomes = q.chave_compartilhada_com.map((id) => (overview.quadras.find((x) => x.quadra_id === id) || { nome: id }).nome);
    card.info.textContent = `Compartilha a stream key com ${nomes.join(", ")}: só uma por vez.`;
  }
  const semChave = !q.tem_stream_key;
  if (semChave) avisos.push("Esta quadra não tem youtube_stream_key em cameras.json.");
  const cheio = overview.transmissoes_com_placar.no_ar >= overview.transmissoes_com_placar.limite && !p.no_ar;
  if (cheio && !semChave) avisos.push("Limite de transmissões com placar atingido; encerre outra para iniciar aqui.");
  card.btnPlacar.disabled = semChave || cheio || Boolean(emUso);
  card.btnDireta.disabled = semChave || Boolean(emUso);
  card.aviso.hidden = avisos.length === 0;
  card.aviso.textContent = avisos.join(" ");
}

// --- laço principal ---------------------------------------------------------

async function refresh() {
  let data;
  try { data = await api("/status"); } catch (e) {
    if (!$("#app").hidden) { $("#aviso-geral").textContent = `Sem conexão com a API: ${e.message}`; $("#aviso-geral").hidden = false; }
    return;
  }
  $("#aviso-geral").hidden = true;
  overview = data;
  const lim = data.transmissoes_com_placar;
  $("#resumo").textContent = `Transmissões com placar: ${lim.no_ar}/${lim.limite}`;
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
