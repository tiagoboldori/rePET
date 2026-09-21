# Replay System — backend central

Sistema de replay de vídeo para quadras esportivas. Cobre o **backend
central** (100% centralizado — buffer, corte de clipe, API e página pública
por quadra) e o **botão físico** (ESPHome), já validados de ponta a ponta
com hardware e câmera reais em 2026-09-15.

> **Escopo deste repositório (definido em 2026-09-17): isto é só o motor.**
> Este projeto cobre captura, corte de clipe e uma **API de gerenciamento**
> (endpoints JSON pra um admin consultar/alterar configuração do motor —
> câmeras, retenção, duração de clipe, etc). **Não vai existir página de
> admin aqui** — nenhuma UI web é planejada neste repositório. Qualquer
> painel/frontend que consuma essa API de gerenciamento é responsabilidade
> de outra equipe/projeto. Isso vale tanto pro futuro `/admin` quanto pra
> qualquer nova funcionalidade de gerenciamento que for adicionada depois.

O botão fala **direto com esta API** via HTTP — não existe Home Assistant
no meio dessa chamada (decisão revisada; ver "Cadeia do botão" abaixo). O
firmware de bring-up usado no primeiro teste foi um devkit **ESP8266**
("NodeMCU V3") só porque já estava disponível — o hardware definitivo
planejado é um devkit **ESP32 + módulo Ethernet W5500**, ainda pendente de
compra/novo bring-up.

## Visão geral: o que está rodando e quando

Existem duas coisas bem separadas, com ciclos de vida diferentes:

1. **Captura contínua (nunca para, não depende de evento nenhum).**
   Um processo `ffmpeg` por câmera fica gravando 24/7, cortando o vídeo
   bruto em segmentos de 2s dentro do **buffer**. Isso é o que permite
   existir um "últimos 35 segundos" pra olhar pra trás a qualquer momento.
   Roda como um serviço systemd por câmera (`replay-capture@<quadra_id>`).

2. **Corte do clipe final (acionado por evento).**
   Só acontece quando alguém chama `POST /replay/{quadra_id}` na API.
   Esse é o evento — hoje simulado manualmente ou via `curl`; na versão
   final, disparado pelo firmware ESPHome direto quando o botão físico da
   quadra é pressionado (sem Home Assistant no meio, ver abaixo). Cada
   chamada corta os últimos N segundos **a partir do instante exato em
   que a chamada chega** e grava um arquivo novo no disco persistente.

Cadeia do botão (validada com hardware real em 2026-09-15):
```
botão físico (GPIO) --> ESP (ESPHome, http_request.post) --> POST /replay/{quadra_id}  <- ESTE repositório
```
Sem Home Assistant no meio — o firmware ESPHome chama este endpoint
diretamente (`quadra_id` fixo por botão, hardcoded no YAML de cada
dispositivo). Uma instância de Home Assistant já existe na infraestrutura
do usuário, mas não faz parte desse fluxo.

### Hardware do hub de botões

| | Placa | Status |
|---|---|---|
| **Usada no bring-up (2026-09-15)** | Devkit **ESP8266** vendido como "NodeMCU V3" (chip `ESP8266MOD`) | Só validou a lógica software (Wi-Fi + HTTP + GPIO). **Não é a placa definitiva** — ESP8266 não roda o componente `ethernet:` do ESPHome (é ESP32-only), então não suporta o módulo Ethernet planejado. |
| **Definitiva (planejada, ainda não comprada)** | Devkit **ESP32** genérico (board ESPHome `esp32dev`, tipicamente vendido como "ESP32 DevKitC" ou "NodeMCU-32S", chip `ESP-WROOM-32`) + módulo Ethernet **W5500** por SPI | Suporta `ethernet:` do ESPHome nativamente; ver orçamento de pinos abaixo pra confirmar que cabe 1 hub por local com até 8 botões. |

**Confirmação: cabe 8 botões + W5500 na mesma placa, com folga.** O W5500 via SPI usa só 6 pinos (config já usada nos exemplos deste repo):

| Função | GPIO sugerido |
|---|---|
| SCK | 18 |
| MISO | 19 |
| MOSI | 23 |
| CS | 5 |
| INT | 4 |
| RST | 14 |

Um devkit ESP32 de 30 ou 38 pinos (`esp32dev`) expõe ~22-25 GPIOs utilizáveis no total (descontando os pinos internos de flash, que nem saem no header). Subtraindo os 6 acima reservados pro W5500, sobram **16+ GPIOs livres** — o dobro do que os 8 botões precisam. Sugestão de 8 pinos pros botões, evitando os de boot-strap (`0, 2, 12, 15`) e preferindo os com pull-up interno disponível (os input-only `34/35/36/39` exigiriam resistor pull-up externo, então ficam como reserva se precisar de mais de 8):
```
botões: GPIO 13, 16, 17, 21, 22, 25, 26, 27
```
Ou seja: **não precisa de expansor de GPIO** — é exatamente o motivo pelo qual o WT32-ETH01 (que usa RMII/LAN8720, ~9-10 pinos fixos só pra Ethernet) foi descartado em favor desse devkit + W5500 (ver decisão em "Contexto do projeto").

## Onde os vídeos ficam salvos

Dois lugares, com papéis e ciclos de vida diferentes — configuráveis por
variável de ambiente (ver `api/config.py`):

| | Variável | Default | O que tem | Ciclo de vida |
|---|---|---|---|---|
| **Buffer bruto** | `BUFFER_ROOT` | `/var/replay` | Segmentos de 2s contínuos, um subdiretório por `quadra_id` (`<BUFFER_ROOT>/<quadra_id>/seg_*.mp4`) | Descartável — retenção fixa de **2 minutos**, nada mais (`scripts/cleanup_segments.sh`, default `max_age_min=2`). Em produção fica em **tmpfs** (RAM), não em disco. |
| **Clipes finais** | `OUTPUT_DIR` | `/var/replay/output` | Um arquivo por evento de replay: `<quadra_id>_<timestamp>.mp4` (e `<...>_overlay.mp4` quando o worker do Lara já aplicou a logo) | Persistente, em disco de verdade. Retenção local de `LOCAL_RAW_RETENTION_DAYS` dias (default 3, `scripts/local_retention_loop.py`, checa a cada 1h) — **não é a entrega oficial ao sócio** (essa é do Lara, 7 dias); é só pra essa página de teste interna e pros endpoints `/api/replays/...` não crescerem sem limite. |

O `OUTPUT_DIR` é montado pela API em `/clips` (via `StaticFiles`), então
todo clipe final já sai acessível publicamente em:
```
GET /clips/<quadra_id>_<timestamp>.mp4
```
Essa é a mesma URL que a página pública da quadra (`GET /quadra/{quadra_id}`,
ver seção "Endpoint da API") usa pra listar/exibir os replays.

## Como configurar quais câmeras são capturadas

Fonte única de verdade: **`config/cameras.json`**.

> **Este arquivo tem credencial RTSP real e está no `.gitignore`** — o
> repositório é público, nunca commitar `config/cameras.json` de verdade.
> Use `config/cameras.example.json` (esse sim commitado, com placeholders)
> como ponto de partida:
> ```bash
> cp config/cameras.example.json config/cameras.json
> # depois edite config/cameras.json com os input_url reais
> ```

```json
{
  "quadra_id": "loc1-quadra1",
  "local_id": "loc1",
  "esporte": "futsal",
  "nome": "Quadra 1",
  "input_url": "rtsp://user:senha@10.0.1.11:554/stream1"
}
```

Esse arquivo é usado em três lugares:

1. **Pela API**, pra validar se um `quadra_id` recebido no `POST /replay/{id}`
   é conhecido (404 se não estiver na lista).
2. **Por `scripts/generate_camera_envs.py`**, que lê `cameras.json` e gera
   automaticamente um `.env` por câmera em `/etc/replay-system/cameras/`
   — é esse `.env` que o unit systemd `replay-capture@.service` usa pra
   saber o RTSP daquela câmera.
3. **Por `db/migrate_cameras.py`** (PT-10), que sincroniza `Local`/`Esporte`/
   `Quadra` no banco a partir deste arquivo — roda automaticamente (idempotente)
   toda vez que a API sobe. `esporte` é obrigatório pra isso; entradas sem
   esse campo caem no esporte `"indefinido"`. `Local.nome` hoje é só derivado
   de `local_id` (capitalizado) — não tem outro nome/observação na fonte;
   gerenciamento de locais/esportes via API está no ciclo seguinte (ver
   `PLANO_DE_ACAO.md`).

Fluxo pra adicionar/trocar uma câmera:

```bash
# 1) editar config/cameras.json (adicionar/alterar a entrada da câmera)
# 2) regerar os .env a partir do arquivo central:
python3 scripts/generate_camera_envs.py --out-dir /etc/replay-system/cameras
# 3) (re)ativar o serviço systemd daquela câmera:
systemctl daemon-reload
systemctl enable --now replay-capture@loc1-quadra1
```

Você NUNCA edita os `.env` de `/etc/replay-system/cameras/` diretamente à
mão em produção — eles são gerados a partir do `cameras.json`. Isso evita
a câmera ficar configurada em dois lugares que podem divergir.

### Câmera de produção

**Definida em 2026-09-17: HiLook H.265+.** As câmeras usadas nos testes até
aqui (`.63`/`.86`, Dahua/OEM) foram só por conveniência — não são o hardware
final, não estão de fato apontadas pra uma quadra.

Checklist ao configurar uma câmera HiLook real:
- **Trocar o codec de vídeo pra H.264** na aba de vídeo da própria câmera —
  "H.265+" é só o padrão de fábrica da linha, não uma trava; a câmera deixa
  escolher H.264/H.265/H.265+ por stream. Isso é pré-requisito: todo o
  pipeline de corte hoje depende de `-c copy` (sem reencode), que só
  funciona porque a câmera entrega H.264 nativo (ver nota em "Endpoint da
  API" acima) — em HEVC o clipe final volta a ter problema de reprodução.
- Validar com `ffprobe` no RTSP depois de configurar (`codec_name=h264`),
  não confiar só na configuração salva na interface web.
- HiLook/Hikvision têm uma função nativa de logo (**Picture Overlay**,
  `Configuration > Image > Picture Overlay`) — ver seção "Funcionalidades
  futuras" abaixo pra detalhes e limitações.

## Endpoint da API

```
POST /replay/{quadra_id}
```
- Sem corpo — o `quadra_id` na URL já é toda a informação necessária.
- `404` se `quadra_id` não estiver em `config/cameras.json`.
- `429` se essa MESMA quadra já foi acionada há menos de `TRIGGER_COOLDOWN_SECONDS` (default 15s) — protege contra clique duplo/repique do botão físico. Outras quadras não são afetadas (cooldown é por `quadra_id`). Resposta inclui quanto falta esperar:
  ```json
  {"detail": "Aguarde mais 9.7s antes de acionar 'loc1-quadra1' de novo (intervalo mínimo: 15s)."}
  ```
- `500` se o corte falhar (ex.: buffer vazio/câmera não está gravando ainda).
- `200` com:
  ```json
  {
    "quadra_id": "loc1-quadra1",
    "clip_filename": "loc1-quadra1_20260915165232.mp4",
    "clip_url": "/clips/loc1-quadra1_20260915165232.mp4"
  }
  ```

```
GET /quadra/{quadra_id}   -> página pública (HTML, sem login) listando os
                              replays dessa quadra, mais recente primeiro,
                              com <video controls> embutido. Lê direto do
                              disco (OUTPUT_DIR) — não depende de Postgres.
                              404 se quadra_id não estiver em cameras.json.
GET /clips/<arquivo>.mp4   -> serve o clipe final (StaticFiles sobre OUTPUT_DIR)
GET /health                -> healthcheck simples
```

Endpoints da API de consumo (M5/M6/M7, `/api/...`, público — sem HTTP
Basic, mesmo nível de acesso que `/quadra/{quadra_id}` e `/clips/*` já
têm hoje):

```
GET /api/replays/{replay_id}         -> metadados do replay pelo id (não
                                         pelo nome do arquivo): quadra_id,
                                         criado_em, duração, tamanho,
                                         media_url, estado de envio ao
                                         Lara. 404 se o id não existir.
GET /api/replays/{replay_id}/media   -> entrega o vídeo em si. Prefere o
                                         arquivo com overlay aplicado
                                         (integrations/overlay.py) quando
                                         já existir, senão o bruto. Suporta
                                         requisições parciais (Range/206,
                                         Accept-Ranges, ETag) nativamente
                                         via FileResponse do Starlette —
                                         RNF1 sem código extra.
GET /api/quadras/{quadra_id}/replays -> listagem paginada dos replays da
                                         quadra, mais recente primeiro.
                                         Query params `page` (default 1)
                                         e `page_size` (default 20, teto
                                         100). Resposta:
                                         {quadra_id, page, page_size,
                                          total, items: [...]}.
                                         404 se quadra_id não existir.
```

Endpoint de gerenciamento (M8, protegido por HTTP Basic — M11):

```
DELETE /api/replays/{replay_id}      -> remove o registro e os arquivos
                                         (bruto e com overlay, se houver)
                                         de um replay. Única medida de
                                         moderação disponível — o
                                         conteúdo é público e sem
                                         controle de acesso na
                                         visualização. `401` sem
                                         credencial válida (exige
                                         ADMIN_USERNAME/ADMIN_PASSWORD
                                         configurados — sem default,
                                         ver tabela abaixo). `204` em
                                         caso de sucesso, `404` se o
                                         replay não existir.
```

Variáveis de ambiente que a API lê (`api/config.py`), todas com default
razoável pra dev local:

| Variável | Default | Descrição |
|---|---|---|
| `BUFFER_ROOT` | `/var/replay` | Onde estão os segmentos brutos |
| `OUTPUT_DIR` | `/var/replay/output` | Onde gravar/servir os clipes finais |
| `CAMERAS_FILE` | `config/cameras.json` | Registro de câmeras conhecidas |
| `CLIP_DURATION_SECONDS` | `35` | Duração do clipe cortado |
| `SEGMENT_TIME` | `2` | Precisa bater com o valor usado por `capture_camera.sh` |
| `SAFETY_MARGIN` | `0.5` | Margem (segundos) pra considerar um segmento "fechado" |
| `MAX_STALENESS_SECONDS` | `3*SEGMENT_TIME + SAFETY_MARGIN + 5` (~13s) | Se o segmento fechado mais recente for mais velho que isso, o corte falha (`500`) em vez de devolver um clipe com conteúdo velho — protege contra câmera travada/desconectada com o processo de captura ainda de pé (ver nota abaixo) |
| `TRIGGER_COOLDOWN_SECONDS` | `15` | Intervalo mínimo entre dois acionamentos da MESMA quadra — uma segunda chamada antes disso recebe `429` em vez de disparar outro corte |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | vazio (sem default de propósito) | Credencial HTTP Basic da superfície de gerenciamento (M11) — hoje só protege `DELETE /api/replays/{id}`. Sem configurar, o endpoint recusa toda requisição com `401` (nunca cai num usuário/senha padrão) |

> **`DATABASE_URL` não está na tabela acima de propósito:** é lida direto
> por `db/engine.py`, não por `api/config.py` — mesmo padrão que
> `capture_camera.sh` já usa (cada módulo se configura sozinho), porque o
> futuro worker assíncrono de logo (não é a API) também vai precisar da
> camada de persistência. Default: `sqlite:///.data/repet.db`. **Desde
> 2026-09-17 (PT-02) a API já grava um `Replay` no banco a cada acionamento
> bem-sucedido** — best-effort: se a gravação falhar, o replay já gerado em
> disco continua sendo entregue normalmente (disco é a fonte de verdade
> final, RNF6; só um aviso vai pro log). No startup, a API também roda
> `create_db_and_tables()` e sincroniza `Local`/`Esporte`/`Quadra` a partir
> de `cameras.json` (PT-10, ver seção acima).

> **Nota sobre buffer travado:** se a câmera travar/desconectar mas o
> processo de captura continuar rodando (não crasha, só para de receber
> quadro novo), o buffer fica "parado" com segmentos cada vez mais velhos.
> Sem proteção, o corte usaria esses segmentos velhos e devolveria `200`
> com um clipe que não é o retroativo real (aconteceu de verdade em
> 2026-09-15, gap de ~10min na câmera). Por isso existe `MAX_STALENESS_SECONDS`:
> se o segmento mais recente for mais velho que isso, a API falha
> explicitamente (`500`) em vez de mascarar o problema com um clipe errado.

> **Nota sobre timeout do cliente:** o corte final é `-c copy` (stream copy,
> sem reencode, ver nota abaixo) — bem mais rápido que o reencode antigo.
> Mesmo assim, qualquer cliente que chame `POST /replay/{quadra_id}` (o
> firmware do botão incluso) deve manter um timeout generoso (usamos 15s no
> ESPHome), pra cobrir variação de rede/RTSP.
>
> **Por que o trim final usa `-c copy` em vez de reencodar:** até 2026-09-16
> isso reencodava pra H.264 (`libx264 -preset veryfast`) porque a câmera real
> entregava **HEVC**, e um trim com `-c copy` preservava esse codec no clipe
> final, que não tocava de forma confiável fora do ecossistema Apple. **A
> partir de 2026-09-17, as câmeras foram configuradas pra entregar H.264
> nativamente** (ajuste feito na própria câmera, não no software), o que
> removeu o motivo de reencodar — o trim voltou a usar `-c copy`, caindo de
> ~1200% CPU/~5-8s por acionamento pra CPU quase zero e <1s. **Pré-requisito
> que precisa continuar valendo:** toda câmera cadastrada em `cameras.json`
> precisa estar configurada pra H.264 (não HEVC/outro codec) na própria
> interface de admin dela — isso não é validado pelo software, é uma
> configuração externa à câmera. Se alguma câmera nova/trocada vier em HEVC
> de novo, o clipe final sai em HEVC e volta a ter problema de reprodução;
> nesse caso, reencodar de novo (só pra aquela câmera, ou globalmente) é a
> correção. Trade-off aceito do `-c copy`: o corte cai no keyframe mais
> próximo antes do ponto pedido, não exatamente em `CLIP_DURATION_SECONDS` —
> o clipe pode sair um pouco mais longo que o pedido (nunca mais curto).

> **Nota sobre a duração do clipe:** o documento de contexto original do
> projeto menciona 40s ("De olho no lance"); o valor passou por 45s e caiu
> pra **35s** (2026-09-17). Motivo original era CPU do reencode (que escalava
> com a duração); depois desse mesmo dia o trim passou a usar `-c copy` (ver
> nota acima), então hoje a duração não tem mais efeito relevante sobre CPU
> — 35s ficou só como o valor de produto decidido. Como é uma variável de
> ambiente e não um valor fixo no código, trocar não exige mudança nenhuma
> no código — só ajustar `CLIP_DURATION_SECONDS` quando o valor final for
> decidido.

## Estrutura do repositório

```
api/
  main.py               -> App FastAPI: POST /replay/{quadra_id}, /clips, /health
  config.py             -> Configuração via variáveis de ambiente
capture/
  capture_camera.sh     -> Captura contínua de UMA câmera (buffer em segmentos)
clipper/
  clip_generator.py     -> Corte dos últimos N segundos, com proteção contra
                            o segmento ainda sendo escrito (race condition)
db/
  engine.py             -> Engine SQLite (modo WAL) + sessão (PT-01)
  models.py             -> Local, Esporte, Quadra (PT-10, com espelho local da
                            config do Lara) e Replay (PT-01, com estado de
                            envio ao Lara), índice composto quadra_id+criado_em.
                            Não existe entidade Logo — isso é do Lara.
  migrate_cameras.py    -> Sincroniza Local/Esporte/Quadra a partir de
                            config/cameras.json (PT-10), idempotente, chamado
                            no startup da API (main.py)
  reconcile.py          -> Insere no banco os Replay ausentes por comparação
                            com o disco (PT-03), chamado no startup da API
  local_retention.py    -> Apaga (arquivo + registro) Replay mais velho que
                            LOCAL_RAW_RETENTION_DAYS (S3, não é a entrega ao
                            sócio — essa é do Lara)
integrations/
  lara_client.py         -> Cliente HTTP da API do Lara (/ping, /cameras,
                            heartbeat, upload de vídeo) — mapeia cada status
                            HTTP num tipo de erro próprio
  config_sync.py         -> Pull de GET /cameras com cache por config_hash;
                            baixa overlay novo e atualiza Quadra
  overlay.py             -> Aplica (nunca decide) o overlay em cache no clipe,
                            via ffmpeg, escalando pro tamanho real do vídeo
  upload_queue.py        -> Processa Replay pendente: aplica overlay, envia
                            ao Lara, idempotente por external_id do clipe
  heartbeat.py           -> Heartbeat periódico por câmera; falha só loga
config/
  cameras.json          -> Registro central de câmeras (fonte única de verdade;
                            gitignored — tem credencial real, nunca commitado)
  cameras.example.json  -> Template commitado, copiar pra cameras.json
systemd/
  replay-capture@.service          -> Unit template (1 instância por câmera)
  replay-api.service               -> Unit da API
  replay-buffer-cleanup.service    -> Unit da limpeza do buffer
  replay-local-retention.service   -> Unit da retenção local dos clipes finais
  replay-lara-worker.service       -> Unit do worker do Lara
  env-examples/                     -> replay-system.env.example (compartilhado
                                        pelos units acima, exceto captura) e um
                                        .env por câmera
scripts/
  generate_camera_envs.py   -> Gera os .env de systemd a partir de cameras.json
  cleanup_segments.sh       -> Um passe de limpeza do buffer (retenção: últimos 2min)
  cleanup_loop.sh           -> Roda cleanup_segments.sh em loop (usado pelo start.sh
                                enquanto o cron real de produção não existe)
  local_retention_loop.py   -> Roda db/local_retention.py em loop (a cada 1h)
  ensure_services.sh        -> Sobe/confere API, captura, limpezas e worker do
                                Lara — chamado por start.sh e por watchdog_loop.sh
  watchdog_loop.sh           -> Chama ensure_services.sh a cada 30s, sem rodar
                                testes de novo — supervisão contínua sem systemd
  lara_worker.py            -> Processo com as 3 rotinas de fundo da integração
                                com o Lara: sync de config, fila de envio, heartbeat
  lara_diagnostic.py        -> Chama /ping e /cameras (ao vivo) e mostra, por
                                câmera, se o cache local está sincronizado
test/
  test_db.py               -> pytest: camada de persistência (db/) — criação de
                                tabela, índice e round-trip de insert/consulta
  test_reconcile.py        -> pytest: reconciliação disco->banco (PT-03)
  test_local_retention.py  -> pytest: retenção local dos clipes finais (S3)
  test_lara_integration.py -> pytest: cliente do Lara (mapeamento de erro),
                                cache por config_hash, fila de envio (com backoff),
                                overlay
  run_pipeline_test.sh     -> Testa capture + clipper direto (sem API, sem câmera real)
  run_api_test.sh          -> Testa a API real (uvicorn) + POST via curl, ponta a ponta
start.sh                  -> Sobe venv/deps, carrega .env, roda os testes padrão e
                              chama ensure_services.sh + watchdog_loop.sh
```

## Integração com o Lara (PT-14/PT-15)

O Lara é o sistema de gestão do clube e passa a ser a fonte de verdade da
configuração operacional (orientação de vídeo, duração do clipe e
logomarca) e o repositório de entrega do clipe ao sócio. Este repositório
nunca decide nenhuma dessas três coisas — só consulta, aplica
mecanicamente e envia. Detalhes de arquitetura e requisitos em
`PLANO_DE_ACAO.md` (seção 6) e `PLANEJAMENTO.md`.

Variáveis de ambiente novas (sem default de propósito para as duas
primeiras — são endereço/credencial de outro sistema):

| Variável | Obrigatória | Descrição |
|---|---|---|
| `LARA_BASE_URL` | sim, pra ligar a integração | Base da API do Lara, ex. `https://lara.clube.example/api/replay` |
| `REPLAY_API_TOKEN` | sim, pra ligar a integração | Token pessoal Sanctum, gerado por `php artisan replay:token` do lado do Lara |
| `OVERLAY_CACHE_DIR` | não (default `/var/replay/overlays`) | Onde os overlays baixados ficam em cache local |
| `LARA_POLL_INTERVAL_SECONDS` | não (default `120`) | Intervalo entre pulls de `GET /cameras` e heartbeats |
| `LARA_UPLOAD_POLL_INTERVAL_SECONDS` | não (default `10`) | Intervalo entre passadas da fila de envio de clipes — só afeta a LATÊNCIA de detectar um replay novo pendente, não o ritmo de retentativa de um que já falhou (ver backoff abaixo) |
| `LOCAL_RAW_RETENTION_DAYS` | não (default `3`, a confirmar) | Retenção do arquivo local, usado só pela página de teste `/quadra/{id}` — não é a entrega ao sócio (essa é do Lara, 7 dias) |

**Configuração persistente (recomendado):** `cp .env.example .env`,
preencher `LARA_BASE_URL`/`REPLAY_API_TOKEN`/`ADMIN_USERNAME`/
`ADMIN_PASSWORD` (e qualquer um dos ajustes finos opcionais) e rodar
`./start.sh` de novo — ele carrega `.env` automaticamente se existir,
sem sobrescrever nada que já esteja exportado no ambiente (útil se um
deploy real preferir setar via systemd `EnvironmentFile=` em vez deste
arquivo). `.env` é gitignored — nunca commitar credencial real aqui,
mesmo padrão já usado em `config/cameras.json`. Alternativa sem arquivo:
`export REPLAY_API_TOKEN=...` na sessão de shell antes de `./start.sh`
(não sobrevive a reboot/novo terminal).

Sem `LARA_BASE_URL`/`REPLAY_API_TOKEN` definidos (nem no `.env` nem no
ambiente), `./start.sh` não sobe o worker do Lara e avisa — o resto do
sistema (captura, corte, página pública) continua funcionando
normalmente. Sem `ADMIN_USERNAME`/`ADMIN_PASSWORD`, `DELETE
/api/replays/{id}` recusa toda requisição com `401` (deny-by-default, ver
seção "Endpoint da API"). Diagnóstico manual (chama `/ping` e `/cameras`
ao vivo e compara com o cache local):

```bash
python -m scripts.lara_diagnostic
```

**Backoff da fila de envio (RNF4):** uma falha transitória (401/403/404/
429/5xx/rede) não retenta a cada passada da fila — cada replay pendente
tem seu próprio atraso exponencial (`lara_tentativas`/
`lara_proxima_tentativa_em` em `Replay`, `integrations/upload_queue.py`):
10s, 20s, 40s... até um teto de 10min, zerado em sucesso ou falha
definitiva (422/413). Sem isso, uma indisponibilidade prolongada do Lara
bateria nele a cada `LARA_UPLOAD_POLL_INTERVAL_SECONDS` pra cada replay
pendente.

## Como rodar (jeito rápido)

```bash
./start.sh
```

Faz tudo de uma vez: cria/atualiza o venv (`.venv/`), confere `ffmpeg`,
roda os testes padrão abaixo — `pytest test/` (camada de persistência) e os
dois testes de shell (com log em `logs/test_*.log` e feedback
`[OK]`/`[FALHOU]` no terminal) — e, por fim, garante que os serviços
abaixo estejam no ar (via `scripts/ensure_services.sh`, chamado por ele),
sem subir duplicata do que já estiver rodando (checa PID em `run/*.pid`).
No final imprime as URLs úteis (`/health`, `/quadra/<id>` de cada câmera).

| Serviço | Script | O que faz |
|---|---|---|
| API | `uvicorn api.main:app` | porta 8000 |
| Captura | `capture/capture_camera.sh` (1 por câmera) | buffer contínuo em `BUFFER_ROOT` |
| Limpeza do buffer | `scripts/cleanup_loop.sh` | mantém só os **últimos 2 minutos** de buffer bruto (`cleanup_segments.sh` a cada 30s) |
| Retenção local | `scripts/local_retention_loop.py` | apaga clipe+registro mais velhos que `LOCAL_RAW_RETENTION_DAYS` (checa a cada 1h) — sempre sobe, não depende do Lara |
| Worker do Lara | `scripts/lara_worker.py` | só sobe se `LARA_BASE_URL`/`REPLAY_API_TOKEN` estiverem configurados (`.env` ou ambiente) |

Nenhum desses depende de cron/systemd do sistema pra funcionar (loop
próprio em cada um).

Pra parar um serviço: `kill $(cat run/api.pid)` (ou o
`run/capture_<quadra_id>.pid` / `run/cleanup.pid` / `run/local_retention.pid`
/ `run/lara_worker.pid` / `run/watchdog.pid` correspondente) — mas o
watchdog abaixo sobe ele de novo na próxima checagem, a não ser que você
pare o watchdog também.

### Watchdog (supervisão contínua)

`./start.sh` também sobe `scripts/watchdog_loop.sh` (PID em
`run/watchdog.pid`, log em `logs/watchdog.log`): a cada 30s ele chama de
novo `ensure_services.sh` (sem rodar a suíte de testes de novo) — se
qualquer um dos serviços da tabela acima tiver caído (crash, `kill`
manual, etc.), ele sobe sozinho na próxima passada, sem precisar rodar
`./start.sh` de novo. Só loga quando de fato reinicia algo ou algo falha
ao subir — rodando saudável, fica em silêncio.

**Limitação conhecida:** o próprio watchdog não tem supervisor — se ele
morrer, nada o reinicia sozinho (é bash puro, subido pelo `start.sh`, sem
depender de systemd/root). Pra supervisão sem esse ponto único de falha,
use os units systemd (`systemd/replay-*.service`, `Restart=always` no
nível do sistema operacional) quando for pra produção de verdade — ver
"Rodando a API em produção" abaixo. As duas abordagens fazem a mesma
coisa (manter os processos no ar); o watchdog é a opção que funciona hoje
sem sudo, os units são a opção mais robusta pra quando isso for
instalado como serviço de sistema.

## Como testar localmente (sem câmera real, sem hardware)

**Teste 0 — camada de persistência (`db/`) e migração de câmeras, sem
câmera/API nenhuma:**
```bash
python -m pytest test/test_db.py -v
```
Roda com `python -m pytest` (não `pytest` puro) — precisa do diretório raiz
do repo no `sys.path` pra importar `db.models`, e `python -m` garante isso.
Cobre: criação de tabela/índice, integridade referencial (Replay exige
Quadra existente), e a sincronização idempotente de `db/migrate_cameras.py`.

Nenhum dos dois testes abaixo precisa de câmera de verdade — ambos usam uma
fonte sintética (`ffmpeg testsrc`) no lugar do RTSP. Já rodei os dois aqui;
ambos passaram (clipe final de `CLIP_DURATION_SECONDS`s, H264, servido
corretamente).

**Teste 1 — só a lógica de captura + corte (sem subir a API):**
```bash
bash test/run_pipeline_test.sh
```

**Teste 2 — endpoint HTTP real (uvicorn + curl), incluindo caso de erro:**
```bash
bash test/run_api_test.sh
```
Esse cobre: quadra desconhecida (404), trigger válido (200 + clipe
gerado), download do clipe pela URL pública retornada, validação do
arquivo com `ffprobe`, e (desde PT-10/PT-02) que o replay foi registrado
no banco e que a quadra foi migrada de `cameras.json` corretamente — em
banco isolado (`$WORKDIR/repet_test.db`), não no `.data/repet.db` de dev.

## Rodando a API em produção

Rápido, sem systemd (mesma ideia do `./start.sh`, manual):
```bash
pip install -r requirements.txt   # fastapi, uvicorn
export BUFFER_ROOT=/var/replay
export OUTPUT_DIR=/var/replay/output
export CAMERAS_FILE=/opt/replay-system/config/cameras.json
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

**Com systemd (recomendado pra produção de verdade — `Restart=always` no
próprio sistema operacional, sobrevive a reboot, sem depender do
watchdog em bash descrito acima):** `systemd/` tem um unit por serviço,
todos compartilhando um único `EnvironmentFile` (mesmas chaves do `.env`
de dev, valores em `/var/replay` de verdade em vez de `.data/`):

| Unit | Serviço |
|---|---|
| `replay-api.service` | API |
| `replay-capture@.service` | captura — 1 instância por câmera, `systemctl enable --now replay-capture@loc1-quadra1` etc., env próprio em `/etc/replay-system/cameras/<quadra_id>.env` |
| `replay-buffer-cleanup.service` | limpeza do buffer |
| `replay-local-retention.service` | retenção local dos clipes finais |
| `replay-lara-worker.service` | worker do Lara — só habilite depois de preencher `LARA_BASE_URL`/`REPLAY_API_TOKEN` no env compartilhado |

```bash
sudo mkdir -p /opt/replay-system /etc/replay-system/cameras
sudo cp -r . /opt/replay-system   # ou git clone direto lá
cd /opt/replay-system && sudo python3 -m venv .venv && sudo .venv/bin/pip install -r requirements.txt
sudo cp systemd/env-examples/replay-system.env.example /etc/replay-system/replay-system.env
sudo "$EDITOR" /etc/replay-system/replay-system.env   # preencher de verdade
sudo cp systemd/env-examples/loc1-quadra3.env /etc/replay-system/cameras/loc1-quadra1.env  # 1 por câmera
sudo "$EDITOR" /etc/replay-system/cameras/loc1-quadra1.env
sudo useradd -r -s /usr/sbin/nologin replay   # se ainda não existir
sudo chown -R replay:replay /opt/replay-system /var/replay
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now replay-api replay-buffer-cleanup replay-local-retention
sudo systemctl enable --now replay-capture@loc1-quadra1
# sudo systemctl enable --now replay-lara-worker   # só depois de configurar o Lara
```
Não testado neste repositório (exigiria root numa máquina real) — os
units seguem o mesmo padrão do `replay-capture@.service` já existente
desde antes, que também nunca foi instalado aqui. Confira `WorkingDirectory`/
caminhos de `.venv` se o deploy real não usar `/opt/replay-system`.

## Funcionalidades futuras (não implementadas)

**Logo/marca d'água queimada no clipe: IMPLEMENTADA desde a pivotagem pro
Lara (17/09/2026), não é mais "futura" — ver `integrations/overlay.py` e
a seção "Integração com o Lara" acima.** O bloco abaixo (discutido em
2026-09-17, antes da pivotagem) previa um cadastro de logo local que
nunca chegou a ser codado; ficou substituído pelo Lara, que decide a
logo e já entrega ela composta. Os fatos técnicos sobre custo de CPU
abaixo continuam valendo pra implementação real de hoje — mantidos como
referência:
- Overlay reverte a otimização `-c copy` pro clipe que leva logo —
  overlay exige decodificar+recodificar o vídeo inteiro (limitação de
  qualquer codec preditivo tipo H.264, não é limitação do ffmpeg
  especificamente). Implementado com preset `ultrafast` do libx264
  (decidido aqui em 2026-09-17, confirmado na implementação real).
  Avaliado e descartado na época: forçar `profile=baseline` (o
  `ultrafast` já desliga B-frames/CABAC/multi-ref por conta própria,
  ganho adicional seria marginal); trocar de codec por VP9/AV1 (mais
  pesados de codificar que H.264, na direção errada); MJPEG intra-only
  (mais rápido de codificar, mas arquivo final bem maior — ruim pra
  servir publicamente).
- Alavanca real pra baixar o custo, ainda não aplicada: **aceleração de
  hardware de vídeo** (Intel Quick Sync via VAAPI, ou NVENC/NVDEC da
  Nvidia) — decode+overlay+encode rodando num bloco de silício dedicado
  em vez da CPU de uso geral. Estimativa: cai de ~1000-1200% CPU pra CPU
  de um dígito só. Depende do hardware do servidor central (ver seção
  abaixo, "parado indefinidamente" segundo o responsável) — não é
  pré-requisito pra funcionar, só pra reduzir custo de fila em pico.
- **Alternativa nativa da câmera (HiLook/Hikvision, Picture Overlay):**
  levantada e avaliada antes da pivotagem pro Lara (BMP 24-bit, máximo
  128×128px, sem confirmação de suporte a transparência nem de
  automação via API/ISAPI) — deixou de ser relevante, o Lara já resolve
  a composição da logo do lado dele. Mantido só como registro histórico,
  não é mais caminho ativo.

**Música/trilha de áudio.** Ainda não implementado. Barato quando for:
não exige tocar no vídeo (`-c:v copy` continua valendo), só o áudio é
(re)codificado — custo de CPU desprezível, compatível com a otimização
atual do trim.

### Servidor central (hardware — ainda em aberto)

Ainda não decidido entre **Mini PC/NUC** e **Raspberry Pi 5 8GB** como
compute único do backend central (ver decisão de arquitetura "backend
central único" acima).

A balança pesa mais pra NUC (com Intel Quick Sync, se o modelo tiver)
desde 2026-09-17: a feature de logo queimado (acima) precisa de
aceleração de hardware de vídeo pra não pesar demais na CPU, e — pelo que
se sabe, **ainda não confirmado na especificação oficial** — o Raspberry
Pi 5 removeu o encoder de vídeo em hardware que modelos anteriores tinham
(mantém só decode acelerado). Se confirmado, o RPi5 não teria como
acelerar esse reencode de jeito nenhum, nem encaixando uma GPU dedicada
(sem slot PCIe de verdade). Não testável na VM de desenvolvimento atual
(Hyper-V sem GPU passada) — só validável com o hardware real escolhido.

## Próximos passos

1. ✅ Buffer contínuo + corte de clipe (validado sem hardware, depois com
   câmera RTSP real).
2. ✅ Endpoint `POST /replay/{quadra_id}` acionando o corte (validado com
   requisição HTTP real e, em 2026-09-15, com botão físico real).
3. ✅ `GET /quadra/{quadra_id}` pública (lista os replays recentes, direto
   do disco). Falta estilizar/melhorar visualmente.
4. 🔶 Botão físico: ESPHome chamando este endpoint direto (sem HA)
   validado com devkit **ESP8266** de bring-up. Falta comprar/testar o
   devkit **ESP32 + módulo Ethernet W5500** definitivo e replicar pros
   demais botões/locais.
5. ✅ Persistência em SQLite (`db/`, ver `PLANEJAMENTO.md`/`PLANO_DE_ACAO.md`
   — decisão revisada de Postgres pra SQLite em 2026-09-17): modelos
   `Local`/`Esporte`/`Quadra` migrados de `cameras.json` (PT-10), `Replay`
   registrado a cada acionamento (PT-02) e reconciliado com o disco a cada
   start da API (PT-03). Integração com o Lara (PT-14/PT-15: pull de
   configuração, aplicação de overlay, envio do clipe, heartbeat,
   diagnóstico) substituiu o pacote de cadastro/hierarquia de logo local —
   não existe mais entidade `Logo` neste projeto, ver seção "Integração
   com o Lara" abaixo. `GET /api/replays/{replay_id}`, `.../media` e
   `GET /api/quadras/{quadra_id}/replays` (M5/M6/M7, PT-04/PT-05/PT-06)
   expõem o replay por id e por quadra (paginado). `DELETE
   /api/replays/{replay_id}` (M8, PT-07) remove registro e arquivo.
6. ✅ Autenticação HTTP Basic (M11, PT-08) protegendo o único endpoint de
   gerenciamento existente hoje (o `DELETE` acima) —
   `ADMIN_USERNAME`/`ADMIN_PASSWORD`, sem default. Só endpoints JSON, sem
   página web (ver nota de escopo no topo do README). Frontend/painel que
   consumir essa API é de outro projeto.
7. ✅ Limpeza do buffer (retenção fixa de 2min, `scripts/cleanup_loop.sh`)
   e retenção local dos clipes finais (`LOCAL_RAW_RETENTION_DAYS`,
   `scripts/local_retention_loop.py`, S3) — as duas sobem automaticamente
   pelo `start.sh`, sem depender de cron do sistema.
8. ✅ Supervisão dos serviços: watchdog em bash (`scripts/watchdog_loop.sh`,
   sobe junto com `./start.sh`, reinicia o que cair a cada 30s, sem
   depender de systemd/root) **e** units systemd de verdade
   (`systemd/replay-*.service`, `Restart=always` no sistema operacional —
   ver "Rodando a API em produção") pra quando isso for instalado como
   serviço de sistema. Os units não foram instalados/testados neste
   repositório (exigiria root numa máquina real).
