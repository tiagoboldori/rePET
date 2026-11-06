# Transmissão ao vivo no YouTube

O sistema tem **dois modos** de transmitir uma quadra. Eles são independentes, usam a
mesma stream key da quadra (`youtube_stream_key` em `config/cameras.json`) e **não podem
estar ligados ao mesmo tempo na mesma quadra**.

Referência de rotas, com exemplos de `curl`: [`API.md`](API.md).

| | **Live direta** (`/live`) | **Transmissão com placar** (`/broadcast`) |
|---|---|---|
| Quem envia ao YouTube | A **própria câmera** (RTMP configurado por ISAPI) | Um **ffmpeg no servidor** |
| O vídeo passa pelo servidor? | **Não** | Sim (reencode) |
| Placar, sets, nomes na imagem | Não | **Sim** |
| Volume ajustável ao vivo | Não | **Sim** |
| Custo no servidor | Nenhum | ~1 a 1,5 núcleo e 0,4 a 0,7 GB por transmissão |
| Simultâneas | Uma por câmera | **2** no total (`BROADCAST_MAX_CONCURRENT`) |
| Resolução enviada | A da câmera (stream principal) | Sempre 1080p (a quadra2 entrega 1440p e é reduzida) |
| Atraso até o YouTube | O menor possível | Alguns segundos a mais (reencode) |
| Sobrevive a reiniciar a API | Sim | Sim |
| Sobrevive a `systemctl restart repet` | **Sim** (a live é da câmera) | **Não** (o ffmpeg está no cgroup do serviço) |
| Sem áudio na câmera | Sai sem trilha de áudio | Sai com trilha de **silêncio** |
| Ligar/desligar | `POST/DELETE /live/{quadra}` | `POST /broadcast/{quadra}/start` e `/stop` |

**Quando usar qual.** Placar ou volume ao vivo: transmissão com placar. Transmitir sem
nada por cima, ou quando o servidor estiver ocupado: live direta, que não gasta CPU.

Os dois podem ser operados pelo [painel web](API.md#painel-web) (`/painel`, que também edita a key e a URL RTMP de cada quadra na aba *Destino*) ou pela API.

**Para onde cada quadra transmite.** O destino (stream key e, se quiser, URL RTMP) fica no cadastro da
quadra e pode ser consultado e trocado pela API, sem editar arquivo:
[`GET`/`PUT /quadras/{id}/youtube`](API.md#configurar-o-destino-da-transmissão-stream-key-e-rtmp), ou só a key (`.../youtube/key`) ou só a URL (`.../youtube/url`). A troca
vale para os dois modos e **não é aceita com a quadra no ar** (`409`).

---

## Live direta: a câmera empurra o RTMP

```
API ──ISAPI (PUT /ISAPI/Custom/RTMP)──▶ câmera Hikvision ──RTMP──▶ YouTube
        (só configura)                    (envia o stream 101)
```

Nada de vídeo passa pelo servidor. Ligar é gravar na câmera a URL do YouTube com a stream
key e `enabled=true`; desligar é `enabled=false`.

- **Câmeras sem DNS.** A URL leva o **IP** do ingest do YouTube, resolvido pela API a
  cada `POST`. Se o IP mudar no meio de uma live longa, a reconexão da câmera usa o IP
  velho.
- **Desligar apaga a stream key da câmera:** o firmware recusa URL vazia
  (`Invalid XML Content`), então vai um placeholder (`rtmp://127.0.0.1/off`).
- **Áudio:** AAC mono 16 kHz do próprio microfone da câmera. Sem áudio ligado na
  câmera, a live sai sem trilha de áudio.
- **Prazo:** a câmera não tem timer. A API grava o prazo (`LIVE_MAX_SECONDS`) em
  `LIVE_STATE_FILE` e desliga a câmera quando vence; o prazo é reassumido na subida da
  API.
- A câmera reconecta sozinha se o RTMP cair (medido: < 5 s depois de ~20 s de receptor
  fora).
- **Limite:** o ISAPI só diz que o envio está *habilitado*. Se a conexão com o YouTube
  falhar, `no_ar` continua `true`.

## Transmissão com placar: um ffmpeg por quadra

```
câmera ──RTSP──▶ ffmpeg ──RTMP──▶ YouTube
                   │  scale 1080p → drawtext (placar) → volume → x264 + AAC
                   ▲
   API ── arquivos de texto (nomes, sets, pontos) relidos a cada quadro
   API ── FIFO (stdin do ffmpeg): comandos de volume e o 'q' para encerrar
```

- **Áudio.** O áudio da câmera passa, antes do volume e do upmix para 44,1 kHz estéreo, por uma cadeia fixa de filtros nomeados
  (`aresample=16000 -> highpass@hp -> afftdn@dn -> lowpass@lp1 -> lowpass@lp2`) com **dois ajustes por quadra**: a *redução de
  ruído* (`AUDIO_DENOISE`, padrão `medio`) e o *corte de agudos* (`AUDIO_CORTE_AGUDOS`, padrão `medio` = 4 kHz). Medido em áudio
  real das quadras: o corte reduz o chiado agudo em ~3 a ~16 dB conforme o nível, a voz até 3,4 kHz não muda, e o custo é
  **+1,1% de um núcleo** sobre ~114%. Os níveis só trocam parâmetros (mix, frequência), então
  [`PUT /broadcast/{id}/audio`](API.md#áudio-corte-de-agudos-e-redução-de-ruído) e a aba *Áudio* do painel os trocam **ao vivo, sem
  relançar**, pelo mesmo canal do volume (o stdin do ffmpeg): o ffmpeg processa **um comando a cada ~0,5 s**, então a API manda só
  o que mudou e **confirma pelas respostas dele** (`ret:0` aplicou, `ret:-38` o filtro não existe, `ret:-34` valor inválido).
  O ajuste fica gravado no estado (sobrevive a reinício) e, ao readotar um ffmpeg, a API reenvia os gravados; uma transmissão que
  subiu **antes** desta cadeia não tem os filtros (`aplicado_ao_vivo: false`) e só muda ao ser relançada. Com a quadra fora do
  ar o ajuste vale ao iniciar. "off" deixa o filtro transparente (mix=0 / om=i), nunca o remove. A live direta não tem isso (sai da
  câmera). Dica de uso: o `afftdn` só rebaixa ruído estacionário (alguns dB; no `forte` cria som metálico); para chiado agudo o que
  funciona é o corte de agudos.
- **Visual.** Nomes, sets e pontos ficam numa caixa **grená** (RGB `7B1E2B`, 85% de opacidade) no canto
  inferior esquerdo; a constante é `SCOREBOARD_BOX_COLOR` em `api/broadcast.py`. A cor é definida na linha de
  comando do ffmpeg, então **vale na próxima vez que a transmissão for iniciada ou relançada**: uma
  transmissão que já está no ar continua com a cor com que subiu (reiniciar só a API a readota sem trocá-la).
- **Placar.** A API só regrava seis arquivos de uma linha em
  `.data/broadcast/{quadra}/` (`nome_a`, `sets_a`, `pontos_a`, idem `b`) e o ffmpeg os
  relê a cada quadro (`drawtext textfile=…:reload=1`): a mudança aparece em menos de 1 s.
  Uma linha por arquivo, porque o `drawtext` do ffmpeg 8 desenha a quebra de linha como
  um quadradinho.
- **Pontuação simples.** Pontos e sets inteiros, nunca negativos. *Fechar set* soma 1 set
  e **zera os pontos dos dois lados**.
- **Volume.** Em % do volume da câmera (100 = original, teto `BROADCAST_VOLUME_MAX`),
  aplicado ao vivo mandando `volume@vol` ao stdin do ffmpeg, sem reiniciar. Há um limitador
  de pico (`alimiter`). **Jogo novo (`reset=true`, o padrão) volta o volume a 100%**: um
  volume 0 do jogo anterior faria a live nascer muda.
- **Processo destacado.** O ffmpeg roda em sessão própria e com o FIFO aberto em
  leitura+escrita pelo próprio processo; por isso reiniciar a API não o derruba. O
  estado (placar, nomes, volume, pid, prazo) fica em `BROADCAST_STATE_FILE`.
- **Custo medido** (câmeras reais, 1080p20, x264): `ultrafast` ≈ 1,0 núcleo e 0,7 GB;
  `veryfast` ≈ 1,5 núcleo e 1,2 GB. Duas transmissões ocupam ~2,5 dos 24 núcleos. Não há
  GPU utilizável (sem `/dev/dri/renderD*`), então o encode é por CPU.
- **Logs:** `logs/broadcast_{quadra}.log` (rotacionado em 5 MB para `.log.1`). O progresso
  do ffmpeg fica em `.data/broadcast/{quadra}/progress.txt`.

---

## Verificação na subida e monitor

Na subida a API reconcilia o que já está no ar, e depois uma thread pergunta às câmeras
(a cada `LIVE_MONITOR_INTERVAL_SECONDS`, 15 s) se a live direta está ligada. O painel e o
`GET /status` leem o cache dessa verificação, então nunca esperam uma câmera lenta.
Detalhes em [`API.md`](API.md#verificação-na-subida). Resumo:

- ffmpeg de transmissão vivo é readotado; o que caiu é relançado;
- ffmpeg **sem registro** (estado perdido) é readotado com o placar lido do overlay;
- live direta **ligada fora da API** (ex.: pela interface da câmera) ganha o prazo;
- transmissão **e** live direta na mesma quadra viram `modo: "conflito"` (nada é
  desligado sozinho).

---

## Casos de borda

Para cada situação: o que o sistema faz e como foi verificado (**T** = teste automatizado
em `test/`, **R** = teste real com câmera e ffmpeg de verdade).

| Situação | O que acontece | Verificado |
|---|---|---|
| ffmpeg morre (queda de rede, erro) | O supervisor relança com espera de 3 s, dobrando até 30 s; `reinicios` conta | T |
| ffmpeg **vivo mas sem produzir quadros** (YouTube/rede travou a saída, RTSP congelou) | Watchdog: o contador de quadros do `-progress` não avança por 30 s (45 s de tolerância na largada) → `SIGKILL` e relança; `travamentos` conta | T (falso, `SIGSTOP`), R (receptor real congelado) |
| ffmpeg **parado** (`SIGSTOP`), que nem atende ao `q` | Mesmo watchdog, com `SIGKILL` direto | T |
| Câmera ou YouTube fora **na largada** | `start` responde `iniciada`; fica `no_ar: true` com `transmitindo: false` e `reinicios` subindo. O painel mostra "Conectando…" | T |
| Câmera **sem áudio** | Sobe com trilha de silêncio (`audio_da_camera: false`), em vez de o ffmpeg sair em loop com "matches no streams" | T, R (substream 102) |
| **API reinicia** no meio da live | O ffmpeg segue; `resume()` o readota pelo pid; placar e volume continuam; o FIFO segue funcionando | T, R |
| **Reboot diário do servidor às 06:00** (agendado) e `systemctl restart repet` | **Derrubam** os ffmpeg das transmissões com placar (`KillMode=control-group`). Na subida a API **relança sozinha** a que estava no ar, com placar, nomes e volume gravados e o prazo original; a live direta é da câmera e não depende do servidor | T (ffmpeg morto antes do `resume`); reboot real **não** testado |
| **Mesma stream key em várias quadras** (intencional) | Só uma pode estar no ar por vez, em qualquer modo: ligar outra responde `409` (a câmera da outra quadra é consultada na hora); `/status` traz `chave_compartilhada_com` e `chave_em_uso_por`, e o painel desabilita o botão. Se duas já estiverem no ar (ligadas fora da API), a subida avisa no log | T, R (3 quadras com a mesma key, uma de cada vez) |
| Arquivo de estado **corrompido** ou com campos estragados | Não derruba a API: é posto de lado (`.corrupt`) ou os campos são saneados | T |
| ffmpeg **sem registro** no estado (arquivo perdido) | Readotado, com nomes e placar lidos do overlay | T |
| Quadra **removida** do `cameras.json` com transmissão no ar | O ffmpeg é encerrado na subida | T |
| **Prazo vence** com a API fora | Na subida a transmissão é encerrada na hora | T |
| Live direta **ligada fora da API** | Ganha prazo (`LIVE_MAX_SECONDS`) na verificação de subida | T |
| Prazo gravado de live direta que **já não está no ar** | Esquecido na verificação de subida | T |
| **Dois modos na mesma quadra** pela API | `409` em quem tenta ligar por segundo | R |
| Dois modos ligados por fora (interface da câmera) | `modo: "conflito"` no `/status` e no painel; **não** desliga nada | T |
| **Terceira** transmissão com placar | `409` (limite 2) | T, R |
| **Câmera lenta** na sondagem de áudio | A sondagem roda fora do lock global: o placar das outras quadras não trava | T |
| Câmera sem resposta ao verificar a live direta | `modo: "desconhecido"` com a mensagem do erro; nada é derrubado | T |
| **Volume 0** no jogo anterior | `start` com `reset=true` volta a 100% | T |
| Clique duplo / `start` repetido | Idempotente (`ja_no_ar`) | T |
| Log do ffmpeg crescendo | Rotaciona em 5 MB | T |
| Força bruta no login | 5 erros por IP em 1 min → `429` | T, R |
| Sessão adulterada ou de outra senha | `401` | T, R |
| POST por cookie sem `X-Painel` (CSRF) | `403` | R |

**Tempos medidos (teste real, câmera + ffmpeg + receptor RTMP local).** Com o receptor
congelado, a saída do ffmpeg encheu os buffers de rede e parou de produzir quadros; o
watchdog o matou 30 s depois e o ffmpeg relançado reconectou sozinho em ~10 s ao subir um
receptor novo. Reiniciar a API no meio da transmissão: o mesmo ffmpeg seguiu vivo, foi
readotado (`readotadas` no log de subida), e o placar e o comando de volume continuaram
funcionando.

### O que NÃO está coberto

- **YouTube recusando a stream key depois de aceitar a conexão**, ou a live não estar
  agendada no YouTube Studio: do lado do servidor isso parece uma conexão normal. Só se
  vê no YouTube Studio (e indiretamente por `reinicios` subindo). Confirmar o lado do
  YouTube exigiria a API do YouTube com OAuth.
- **Disco cheio** ou relógio do servidor alterado: não tratados.
- **Anúncios e timer de jogo:** não implementados.
- O painel trafega em **HTTP puro**; para acesso fora da rede interna, ponha um proxy
  HTTPS na frente.

---

## Configuração

Todas em `.env` (modelo em `.env.example`); mudar exige reiniciar a API.

| Variável | Padrão | Para quê |
|---|---|---|
| `LIVE_API_KEY` | vazio | Token das rotas de controle (`Authorization: Bearer`, ou o legado `X-Live-Key`). O `REPLAY_API_TOKEN` e as chaves de `API_TOKENS` também valem no Bearer; sem nenhum = `503` |
| `LIVE_MAX_SECONDS` | `7200` | Prazo de qualquer live; `0` = sem limite |
| `YOUTUBE_RTMP_URL` | `rtmp://a.rtmp.youtube.com/live2` | Ingest do YouTube; cada quadra pode ter o seu em `rtmp_url` no `cameras.json` (trocável por `PUT /quadras/{id}/youtube`) |
| `RTMP_ALLOWED_HOSTS` | `youtube.com` | Domínios (e subdomínios) que o `PUT` aceita como `rtmp_url` de uma quadra, além do host da `YOUTUBE_RTMP_URL`; IP só vale por inteiro |
| `API_TOKENS` | vazio | Chaves nomeadas de integração (`nome:token,nome2:token2`), válidas no Bearer das rotas de controle |
| `BROADCAST_MAX_CONCURRENT` | `2` | Transmissões com placar simultâneas |
| `BROADCAST_PRESET` / `BROADCAST_BITRATE` / `BROADCAST_THREADS` | `ultrafast` / `4500k` / `4` | Encode: preset x264, bitrate (sempre `<n>k`) e núcleos por transmissão |
| `BROADCAST_VOLUME_STEP` / `BROADCAST_VOLUME_MAX` | `10` / `400` | Passo do volume e teto (%) |
| `AUDIO_DENOISE` | `medio` | Padrão da redução de ruído do áudio da transmissão com placar: `off`, `leve`, `medio`, `forte`. Ajustável por quadra, ao vivo, pela API e pelo painel |
| `AUDIO_CORTE_AGUDOS` | `medio` | Padrão do corte de agudos: `off`, `suave` (5 kHz), `medio` (4 kHz), `forte` (4 kHz íngreme), `maximo` (3,5 kHz íngreme). Mesmo ajuste por quadra; não afeta a live direta |
| `LIVE_MONITOR_INTERVAL_SECONDS` | `15` | De quanto em quanto tempo se pergunta às câmeras pela live direta |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | vazio | Login do painel (e do `DELETE`); vazio = login `503` |
| `PANEL_SESSION_HOURS` / `PANEL_SECRET` | `12` / vazio | Validade da sessão; segredo extra que assina o cookie |

Em `.env`, comentário só em linha própria (`LIVE_MAX_SECONDS=14400   # 4h` derruba a API).

## Diagnóstico

```bash
# Estado de tudo (painel faz o mesmo)
curl -s http://localhost:8000/status -H "Authorization: Bearer $LIVE_API_KEY"

# A transmissão está mesmo enviando? (transmitindo=false com no_ar=true = tentando)
curl -s http://localhost:8000/broadcast/loc1-quadra1 -H "Authorization: Bearer $LIVE_API_KEY"

tail -f logs/broadcast_loc1-quadra1.log     # ffmpeg da transmissão com placar
grep '\[startup\]\|\[broadcast\]\|\[monitor\]' logs/api.log
```

**Reiniciar só a API** (não derruba nenhuma transmissão): `kill $(cat run/api.pid)`; o
watchdog a sobe em até 30 s e o botão de replay fica sem resposta nesse intervalo. Variáveis
**novas** do `.env` (que o watchdog ainda não tinha) passam a valer nessa subida, porque o
`ensure_services.sh` as carrega; **mudar o valor de uma variável que já existia** exige
reiniciar o serviço ou esperar o reboot das 06:00.
**`systemctl restart repet` e o reboot derrubam as transmissões com placar.** Não matar o ffmpeg
com `pkill -f` por padrão de nome (pode pegar o próprio comando): use o pid em
`.data/broadcast_state.json`.
