# rePET — motor de captura e envio de replays

Motor de replay de vídeo para quadras esportivas. Grava continuamente as
câmeras das quadras, e quando o botão físico da quadra é apertado **corta
os últimos ~35 segundos**, aplica orientação, logo e música de fundo e
**envia o clipe por HTTP a uma plataforma externa** (sistema de gestão do
clube), que é quem entrega o vídeo ao sócio.

> **Este repositório não é mais o backend central.** Ele é só o *motor*:
> captura, corte, processamento e envio. Decisões de produto — orientação
> do vídeo, duração do clipe, logomarca, entrega e retenção ao sócio —
> pertencem à plataforma externa; aqui só se consulta essa configuração, aplica-se
> mecanicamente e envia-se o resultado. Não existe (nem vai existir) UI web
> de administração neste repositório; qualquer painel é de outro projeto.

## Como funciona

```
botão físico (ESP32) ──HTTP──> POST /replay/{quadra_id}
                                      │
   ffmpeg 24/7 (por câmera)           ▼
   RTSP ─> buffer em segmentos ─> corte dos últimos N s (-c copy)
                                      │
                                      ▼  registro Replay (SQLite)
                         worker de integração (fila de envio)
              orientação + overlay ─> música ─> POST /cameras/{id}/videos
                                      │
                                      ▼
                           plataforma externa
```

Dois ciclos de vida independentes:

1. **Captura contínua** — um `ffmpeg` por câmera grava 24/7 segmentos de 2s
   no buffer (tmpfs em produção, retenção fixa de 2 min). É isso que
   permite olhar "os últimos 35s" a qualquer momento.
2. **Corte por evento** — `POST /replay/{quadra_id}` corta os últimos N
   segundos a partir do instante da chamada e grava o clipe bruto em disco.
   Um worker assíncrono então processa e envia à plataforma externa, com retentativa.

O botão chama a API **direto**, sem Home Assistant no meio (`quadra_id`
fixo por botão no firmware ESPHome).

## Integração com plataforma externa

Toda a integração fica em `integrations/` e roda no worker
`scripts/lara_worker.py` (endereço em `LARA_BASE_URL`), com três rotinas de fundo:

| Rotina | O que faz |
|---|---|
| **Sync de config** (`config_sync.py`) | `GET /cameras`, com cache por `config_hash`; baixa overlay novo e atualiza a `Quadra` local (orientação, duração, overlay) |
| **Fila de envio** (`upload_queue.py`) | Processa cada `Replay` pendente: render → música → upload. Idempotente por `external_id` (= `Replay.id`) |
| **Heartbeat** (`heartbeat.py`) | Avisa periodicamente que cada câmera está viva; falha só loga |

**Processamento antes do envio**, nesta ordem:

1. **Orientação + overlay** (`render.py`, usa `orientation.py` e
   `overlay.py`) — crop centralizado pro aspect ratio pedido
   (`vertical` = 9:16, `horizontal` = 16:9) e logo queimada por cima.
   Quando os dois se aplicam (caso comum) é **um único passe de ffmpeg**.
   Se nenhum se aplica, não há reencode.
2. **Música de fundo** (`audio.py`) — decisão local (não vem da plataforma):
   sorteia uma faixa de `assets/music/`, usa o trecho inicial na duração
   do clipe, com fade in/out. Sem faixas, é no-op. As câmeras não têm
   áudio, então só entra a trilha.

O arquivo final fica em `Replay.arquivo_processado` e é o que é enviado.

**Envio:** `POST {LARA_BASE_URL}/cameras/{id}/videos` (multipart) com
`Authorization: Bearer <REPLAY_API_TOKEN>`, `recorded_at` (instante do
botão, em horário local com offset explícito, ex. `-03:00`),
`duration_seconds` e `external_id`. Falha transitória (401/403/404/429/5xx/
rede) é retentada com backoff exponencial por replay (10s, 20s, 40s… teto
de 10 min); 422/413 são falhas definitivas, sem retry.

Diagnóstico ao vivo (chama `/ping` e `/cameras` e compara com o cache local):

```bash
python -m scripts.lara_diagnostic
```

Sem `LARA_BASE_URL`/`REPLAY_API_TOKEN`, o worker não sobe; captura, corte
e página de teste continuam funcionando normalmente.

## Câmeras

Fonte única: **`config/cameras.json`** (gitignored — contém credencial RTSP;
o repositório é público, **nunca commitar credencial real**). Parta do
template:

```bash
cp config/cameras.example.json config/cameras.json   # editar input_url reais
```

```json
{
  "quadra_id": "loc1-quadra1",
  "local_id": "loc1",
  "esporte": "futsal",
  "nome": "Quadra 1",
  "input_url": "rtsp://user:senha@10.0.1.11:554/stream1"
}
```

Usado pela API (valida `quadra_id`), por `scripts/generate_camera_envs.py`
(gera os `.env` de systemd por câmera em `/etc/replay-system/cameras/` —
não edite esses à mão) e por `db/migrate_cameras.py` (sincroniza
`Local`/`Esporte`/`Quadra` no banco a cada start da API, idempotente).

Adicionar/trocar câmera:

```bash
# 1) editar config/cameras.json
python3 scripts/generate_camera_envs.py --out-dir /etc/replay-system/cameras
systemctl daemon-reload
systemctl enable --now replay-capture@loc1-quadra1
```

**Câmera de produção: HiLook H.265+.** Pré-requisito: configurar o stream
para **H.264** na própria câmera (o corte usa `-c copy`, sem reencode, e em
HEVC o clipe final tem problema de reprodução). Validar com `ffprobe` no
RTSP (`codec_name=h264`) — o software não valida isso.

## API

Público (sem autenticação):

| Endpoint | Descrição |
|---|---|
| `POST /replay/{quadra_id}` | Aciona o corte. `404` quadra desconhecida; `429` se a mesma quadra foi acionada há menos de `TRIGGER_COOLDOWN_SECONDS` (15s); `500` se o corte falhar (buffer vazio ou parado — ver `MAX_STALENESS_SECONDS`); `200` com `{quadra_id, clip_filename, clip_url}` |
| `GET /api/replays/{replay_id}` | Metadados do replay, incluindo estado do envio |
| `GET /api/replays/{replay_id}/media` | Vídeo (prefere o processado). Range/206 e ETag; `Cache-Control: no-cache` |
| `GET /api/quadras/{quadra_id}/replays` | Listagem paginada (`page`, `page_size` ≤ 100), mais recente primeiro |
| `GET /quadra/{quadra_id}` | Página HTML simples de teste interno com os replays da quadra |
| `GET /clips/<arquivo>.mp4` | Clipe bruto (StaticFiles sobre `OUTPUT_DIR`) |
| `GET /health` | Healthcheck |

Protegido por HTTP Basic (`ADMIN_USERNAME`/`ADMIN_PASSWORD`, sem default —
sem configurar, responde `401` sempre):

| Endpoint | Descrição |
|---|---|
| `DELETE /api/replays/{replay_id}` | Remove registro e arquivos (`204`; `404` se não existe) |

O corte usa `-c copy`, então cai no keyframe anterior ao ponto pedido: o
clipe pode sair um pouco mais longo que `CLIP_DURATION_SECONDS`, nunca
mais curto. Clientes (inclusive o firmware) devem usar timeout generoso
(15s no ESPHome).

## Configuração

Configuração persistente: `cp .env.example .env`, preencher e rodar
`./start.sh` (carrega o `.env` sem sobrescrever o que já estiver exportado).
`.env` é gitignored. Em produção com systemd, use o `EnvironmentFile`.
`BUFFER_ROOT`, `OUTPUT_DIR`, `CAMERAS_FILE`, `DATABASE_URL` e
`SEGMENT_TIME` são fixados pelo `start.sh` em dev (tudo em `.data/`).

| Variável | Default | Descrição |
|---|---|---|
| `LARA_BASE_URL` | — (obrigatória p/ integração) | Base da API da plataforma externa, ex. `https://plataforma.example/api/replay` |
| `REPLAY_API_TOKEN` | — (obrigatória p/ integração) | Token Sanctum, gerado com `php artisan replay:token` na plataforma |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | vazio | Credencial do `DELETE`; vazio = sempre `401` |
| `BUFFER_ROOT` | `/var/replay` | Segmentos brutos (`<BUFFER_ROOT>/<quadra_id>/seg_*.mp4`) |
| `OUTPUT_DIR` | `/var/replay/output` | Clipes finais (`<quadra_id>_<timestamp>.mp4`, mais variantes `_oriented`/`_overlay`) |
| `CAMERAS_FILE` | `config/cameras.json` | Registro de câmeras |
| `DATABASE_URL` | `sqlite:///.data/repet.db` | Lida por `db/engine.py` |
| `CLIP_DURATION_SECONDS` | `35` | Duração do corte |
| `SEGMENT_TIME` | `2` | Deve bater com `capture_camera.sh` |
| `SAFETY_MARGIN` | `0.5` | Margem (s) para considerar um segmento fechado |
| `MAX_STALENESS_SECONDS` | ~13 | Segmento fechado mais velho que isso ⇒ corte falha (`500`) em vez de entregar clipe velho (câmera travada) |
| `TRIGGER_COOLDOWN_SECONDS` | `15` | Intervalo mínimo por quadra |
| `OVERLAY_CACHE_DIR` | `/var/replay/overlays` | Cache local dos overlays da plataforma |
| `LARA_POLL_INTERVAL_SECONDS` | `120` | Pull de `/cameras` |
| `LARA_HEARTBEAT_INTERVAL_SECONDS` | `120` | Heartbeat |
| `LARA_UPLOAD_POLL_INTERVAL_SECONDS` | `10` | Latência para detectar replay pendente (não é o ritmo de retry) |
| `LOCAL_RAW_RETENTION_DAYS` | `3` | Retenção local dos clipes (só para a página de teste; a entrega oficial é da plataforma) |
| `MUSIC_DIR` / `MUSIC_VOLUME` / `MUSIC_FADE_SECONDS` | `assets/music` / `0.5` / `1.5` | Música de fundo (ver `assets/music/README.md`) |

## Estrutura

```
api/            FastAPI: POST /replay, endpoints /api/*, /quadra, /clips, /health
capture/        capture_camera.sh — captura contínua de UMA câmera
clipper/        clip_generator.py — corte dos últimos N s (+ ffprobe helpers)
db/             SQLite (WAL): modelos Local/Esporte/Quadra/Replay, migração de
                cameras.json, reconciliação disco→banco, retenção local
integrations/   Integração: client, config_sync, upload_queue, heartbeat,
                render/orientation/overlay, audio
scripts/        worker de integração, loops de limpeza/retenção, watchdog,
                ensure_services, geração de .env por câmera, diagnóstico
systemd/        units (api, capture@, cleanup, local-retention, lara-worker)
                + env-examples
assets/music/   trilhas de fundo
config/         cameras.example.json (template; cameras.json é gitignored)
test/           pytest (db, reconcile, retenção, integração externa) e
                testes de shell (pipeline e API ponta a ponta)
```

## Rodando

### Dev / sem systemd

```bash
./start.sh
```

Cria o venv, confere `ffmpeg`, roda os testes e garante no ar (sem duplicar,
via PID em `run/*.pid`): API (porta 8000), captura (1 por câmera), limpeza
do buffer (últimos 2 min), retenção local (a cada 1h) e worker de integração (se
configurado). Também sobe o **watchdog** (`scripts/watchdog_loop.sh`), que
chama `ensure_services.sh` a cada 30s e reinicia o que cair.

Limitações: o watchdog não tem supervisor próprio. **Nunca rode dois
`start.sh`/watchdogs como usuários diferentes** na mesma máquina — um não
enxerga o PID do outro e acumulam `ffmpeg` duplicado (já causou clipes com
"vídeo voltando"). `ensure_services.sh` recusa subir duplicata se achar o
mesmo comando rodando (`pgrep -f`); se avisar, mate o órfão antes. O
watchdog também não recarrega o `.env` sozinho — reinicie o worker após
editar.

### Produção (systemd)

Um unit por serviço, todos com `Restart=always` e um `EnvironmentFile`
compartilhado:

```bash
sudo mkdir -p /opt/replay-system /etc/replay-system/cameras
sudo cp -r . /opt/replay-system
cd /opt/replay-system && sudo python3 -m venv .venv && sudo .venv/bin/pip install -r requirements.txt
sudo cp systemd/env-examples/replay-system.env.example /etc/replay-system/replay-system.env
sudo cp systemd/env-examples/loc1-quadra3.env /etc/replay-system/cameras/loc1-quadra1.env  # 1 por câmera
sudo useradd -r -s /usr/sbin/nologin replay
sudo chown -R replay:replay /opt/replay-system /var/replay
sudo cp systemd/*.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now replay-api replay-buffer-cleanup replay-local-retention
sudo systemctl enable --now replay-capture@loc1-quadra1
sudo systemctl enable --now replay-lara-worker   # só após configurar a integração
```

Os units **não foram instalados/testados neste repositório** (exigem root
numa máquina real); confira `WorkingDirectory` e o caminho do `.venv` se o
deploy não usar `/opt/replay-system`.

### Testes

```bash
python -m pytest test/ -v         # db, reconcile, retenção local, integração externa
bash test/run_pipeline_test.sh    # captura + corte, fonte sintética (ffmpeg testsrc)
bash test/run_api_test.sh         # uvicorn + curl ponta a ponta, banco isolado
```

Nenhum precisa de câmera ou hardware.

## Hub de botões (ESPHome)

Cadeia validada com hardware real em 2026-09-15 (devkit ESP8266 de
bring-up). O hardware definitivo é um **ESP32 DevKit V1 (WROOM-32) +
módulo Ethernet W5500 por SPI**, 1 por local, até 8 botões — ainda
pendente de compra/novo bring-up. Evitar ENC28J60 (instável).

**W5500 (7 fios, MISO/MOSI não se cruzam):**

| DevKit | GPIO | W5500 |
|---|---|---|
| D19 | 19 | MISO |
| D18 | 18 | MOSI |
| D5 | 5 | SCS |
| TX2 | 17 | SCLK |
| RX2 | 16 | RST |
| GND / 3V3 | — | GND / 3V3 (INT desconectado) |

**Botões** (`INPUT_PULLUP`, cada um entre o GPIO e GND, sem resistor
externo): quadra 1→GPIO32, 2→33, 3→25, 4→26, 5→27, 6→14, 7→12, 8→13.
Reservas: `D4, D21, D22, D23`.

Pontos de atenção:
- **GPIO12 (quadra 7) é pino de strapping** — nunca colocar pull-up externo
  (nível alto no reset seleciona flash 1,8V e a placa não inicia). O aviso
  de strapping do ESPHome na compilação é esperado.
- **Capacitores de desacoplamento** (100nF + 10µF) o mais perto possível do
  módulo de rede; o W5500 puxa ~150mA em rajadas e sem isso o link cai só em
  produção.
- **Tensão do módulo:** módulos pequenos (W5500 Lite) são 3,3V apenas e
  queimam com 5V. Terra comum obrigatório.
- **WROOM vs WROVER:** GPIO16/17 são consumidos pela PSRAM no WROVER —
  conferir a serigrafia.
- Se instável, reduzir `clock_speed` do SPI (mínimo 8MHz) e manter fios
  curtos (~5cm).
- Conferir MISO×MOSI e TX2×RX2 (trocar inverte SCLK/RST); uma linha
  deslocada nos botões reporta a quadra errada silenciosamente.

Validação antes da solda: (1) protoboard com módulo + **um** botão, Ethernet
subindo com IP fixo; (2) energizar com o botão da quadra 7 pressionado — deve
iniciar; (3) só então replicar pros 8 e passar à perfboard.

## Estado atual

| Área | Estado |
|---|---|
| Captura contínua, corte, API de replay | ✅ validado com câmera RTSP real |
| Persistência SQLite, reconciliação disco→banco, retenção local | ✅ |
| Integração externa (sync, orientação, overlay, música, fila com backoff, heartbeat, diagnóstico) | ✅ 1º envio confirmado em 2026-10-05; auditada campo a campo contra a especificação |
| `recorded_at` em horário local com offset | ✅ corrigido; confirmar na plataforma após fila represada |
| Supervisão (watchdog em bash + units systemd) | ✅ watchdog em uso; units não instalados |
| Botão físico definitivo (ESP32 + W5500) | 🔶 lógica validada com ESP8266; falta o hardware definitivo e replicar por local |
| Contrato completo da API externa (`docs/replay-api.md`) | ⚠️ nunca lido; implementação baseada na especificação resumida |
| Aceleração de vídeo por hardware (Quick Sync/NVENC) | ⏳ não aplicada; hoje orientação/overlay reencodam em CPU (`libx264 veryfast`, CRF 23) |
| Servidor de produção (Mini PC/NUC vs. Raspberry Pi 5) | ⏳ em aberto; NUC favorecido pelo encode por hardware |
