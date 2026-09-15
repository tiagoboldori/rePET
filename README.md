# Replay System — backend central

Sistema de replay de vídeo para quadras esportivas. Cobre o **backend
central** (100% centralizado — buffer, corte de clipe, API e página pública
por quadra) e o **botão físico** (ESPHome), já validados de ponta a ponta
com hardware e câmera reais em 2026-09-15.

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
   bruto em segmentos de 3s dentro do **buffer**. Isso é o que permite
   existir um "últimos 45 segundos" pra olhar pra trás a qualquer momento.
   Roda como um serviço systemd por câmera (`replay-capture@<quadra_id>`).

2. **Corte do clipe final (acionado por evento).**
   Só acontece quando alguém chama `POST /replay/{quadra_id}` na API.
   Esse é o evento — hoje simulado manualmente ou via `curl`; na versão
   final, disparado pelo Home Assistant quando o botão físico da quadra é
   pressionado. Cada chamada corta os últimos N segundos **a partir do
   instante exato em que a chamada chega** e grava um arquivo novo no
   disco persistente.

Cadeia do botão (validada com hardware real em 2026-09-15):
```
botão físico (GPIO) --> ESP (ESPHome, http_request.post) --> POST /replay/{quadra_id}  <- ESTE repositório
```
Sem Home Assistant no meio — o firmware ESPHome chama este endpoint
diretamente (`quadra_id` fixo por botão, hardcoded no YAML de cada
dispositivo). Uma instância de Home Assistant já existe na infraestrutura
do usuário, mas não faz parte desse fluxo.

## Onde os vídeos ficam salvos

Dois lugares, com papéis e ciclos de vida diferentes — configuráveis por
variável de ambiente (ver `api/config.py`):

| | Variável | Default | O que tem | Ciclo de vida |
|---|---|---|---|---|
| **Buffer bruto** | `BUFFER_ROOT` | `/var/replay` | Segmentos de 3s contínuos, um subdiretório por `quadra_id` (`<BUFFER_ROOT>/<quadra_id>/seg_*.mp4`) | Descartável — retenção fixa de **2 minutos**, nada mais (`scripts/cleanup_segments.sh`, default `max_age_min=2`). Em produção fica em **tmpfs** (RAM), não em disco. |
| **Clipes finais** | `OUTPUT_DIR` | `/var/replay/output` | Um arquivo por evento de replay: `<quadra_id>_<timestamp>.mp4` | Persistente, em disco de verdade. Retenção pública ainda é um placeholder (ver "Decisões pendentes" no contexto do projeto). |

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
  "nome": "Quadra 1",
  "input_url": "rtsp://user:senha@10.0.1.11:554/stream1"
}
```

Esse arquivo é usado em dois lugares:

1. **Pela API**, pra validar se um `quadra_id` recebido no `POST /replay/{id}`
   é conhecido (404 se não estiver na lista).
2. **Por `scripts/generate_camera_envs.py`**, que lê `cameras.json` e gera
   automaticamente um `.env` por câmera em `/etc/replay-system/cameras/`
   — é esse `.env` que o unit systemd `replay-capture@.service` usa pra
   saber o RTSP daquela câmera.

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

## Endpoint da API

```
POST /replay/{quadra_id}
```
- Sem corpo — o `quadra_id` na URL já é toda a informação necessária.
- `404` se `quadra_id` não estiver em `config/cameras.json`.
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

Variáveis de ambiente que a API lê (`api/config.py`), todas com default
razoável pra dev local:

| Variável | Default | Descrição |
|---|---|---|
| `BUFFER_ROOT` | `/var/replay` | Onde estão os segmentos brutos |
| `OUTPUT_DIR` | `/var/replay/output` | Onde gravar/servir os clipes finais |
| `CAMERAS_FILE` | `config/cameras.json` | Registro de câmeras conhecidas |
| `CLIP_DURATION_SECONDS` | `45` | Duração do clipe cortado |
| `SEGMENT_TIME` | `3` | Precisa bater com o valor usado por `capture_camera.sh` |
| `SAFETY_MARGIN` | `2.0` | Margem (segundos) pra considerar um segmento "fechado" |
| `MAX_STALENESS_SECONDS` | `3*SEGMENT_TIME + SAFETY_MARGIN + 5` (~16s) | Se o segmento fechado mais recente for mais velho que isso, o corte falha (`500`) em vez de devolver um clipe com conteúdo velho — protege contra câmera travada/desconectada com o processo de captura ainda de pé (ver nota abaixo) |

> **Nota sobre buffer travado:** se a câmera travar/desconectar mas o
> processo de captura continuar rodando (não crasha, só para de receber
> quadro novo), o buffer fica "parado" com segmentos cada vez mais velhos.
> Sem proteção, o corte usaria esses segmentos velhos e devolveria `200`
> com um clipe que não é o retroativo real (aconteceu de verdade em
> 2026-09-15, gap de ~10min na câmera). Por isso existe `MAX_STALENESS_SECONDS`:
> se o segmento mais recente for mais velho que isso, a API falha
> explicitamente (`500`) em vez de mascarar o problema com um clipe errado.

> **Nota sobre timeout do cliente:** com câmera real em 1080p, o corte
> (concat + reencode) leva ~7-8s neste servidor — bem mais que com a fonte
> sintética dos testes. Qualquer cliente que chame `POST /replay/{quadra_id}`
> (o firmware do botão incluso) precisa de um timeout generoso (usamos 15s
> no ESPHome); um timeout de poucos segundos vai dar falso-negativo.

> **Nota sobre 40s vs 45s:** o documento de contexto original do projeto
> menciona 40s ("De olho no lance"); o valor usado agora é 45s (pedido
> numa mensagem anterior). Como é uma variável de ambiente e não um valor
> fixo no código, trocar não exige mudança nenhuma no código — só ajustar
> `CLIP_DURATION_SECONDS` quando o valor final for decidido.

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
config/
  cameras.json          -> Registro central de câmeras (fonte única de verdade;
                            gitignored — tem credencial real, nunca commitado)
  cameras.example.json  -> Template commitado, copiar pra cameras.json
systemd/
  replay-capture@.service   -> Unit template (1 instância por câmera)
  env-examples/              -> Exemplo de .env por câmera
scripts/
  generate_camera_envs.py   -> Gera os .env de systemd a partir de cameras.json
  cleanup_segments.sh       -> Um passe de limpeza do buffer (retenção: últimos 2min)
  cleanup_loop.sh           -> Roda cleanup_segments.sh em loop (usado pelo start.sh
                                enquanto o cron real de produção não existe)
test/
  run_pipeline_test.sh   -> Testa capture + clipper direto (sem API, sem câmera real)
  run_api_test.sh        -> Testa a API real (uvicorn) + POST via curl, ponta a ponta
start.sh                  -> Sobe venv/deps, roda os testes padrão, a API, a captura de
                              cada câmera e a limpeza do buffer
```

## Como rodar (jeito rápido)

```bash
./start.sh
```

Faz tudo de uma vez: cria/atualiza o venv (`.venv/`), confere `ffmpeg`,
roda os dois testes padrão abaixo (com log em `logs/test_*.log` e
feedback `[OK]`/`[FALHOU]` no terminal) e, por fim, garante que a API, a
captura de cada câmera de `config/cameras.json` **e a limpeza do buffer**
estejam no ar — sem subir duplicata se já estiverem rodando (checa PID em
`run/*.pid`). No final imprime as URLs úteis (`/health`, `/quadra/<id>`
de cada câmera).

A limpeza (`scripts/cleanup_loop.sh`) roda `cleanup_segments.sh` a cada
30s, mantendo só os **últimos 2 minutos** de buffer bruto — o resto é
apagado. Isso vale tanto pra segmentos antigos que já estavam acumulados
quanto pros novos que forem chegando; não depende de cron do sistema
estar instalado.

Pra parar um serviço subido por ele: `kill $(cat run/api.pid)` (ou o
`run/capture_<quadra_id>.pid` / `run/cleanup.pid` correspondente).

Isso é um jeito manual de "subir tudo" pra essa fase de desenvolvimento —
**não substitui** os units systemd (produção real ainda depende do que
está pendente em "Próximos passos": unit da própria API, tmpfs, cron real
de sistema em vez do loop do `start.sh`).

## Como testar localmente (sem câmera real, sem hardware)

Nenhum dos dois testes abaixo precisa de câmera de verdade — ambos usam uma
fonte sintética (`ffmpeg testsrc`) no lugar do RTSP. Já rodei os dois aqui;
ambos passaram (clipe final de 45.000000s, H264, servido corretamente).

**Teste 1 — só a lógica de captura + corte (sem subir a API):**
```bash
bash test/run_pipeline_test.sh
```

**Teste 2 — endpoint HTTP real (uvicorn + curl), incluindo caso de erro:**
```bash
bash test/run_api_test.sh
```
Esse cobre: quadra desconhecida (404), trigger válido (200 + clipe
gerado), download do clipe pela URL pública retornada, e validação do
arquivo com `ffprobe`.

## Rodando a API em produção

```bash
pip install -r requirements.txt   # fastapi, uvicorn
export BUFFER_ROOT=/var/replay
export OUTPUT_DIR=/var/replay/output
export CAMERAS_FILE=/opt/replay-system/config/cameras.json
uvicorn api.main:app --host 0.0.0.0 --port 8000
```
(Em produção, isso também vira um unit systemd — ainda não incluído aqui,
mesmo padrão do `replay-capture@.service`.)

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
5. ⬜ Persistência em Postgres da tabela `Replay` (hoje o corte só grava o
   arquivo; não há registro em banco ainda).
6. ⬜ `/admin` protegido (HTTP Basic).
7. 🔶 Limpeza do buffer (retenção fixa de 2min) — funcionando via
   `scripts/cleanup_loop.sh`, subido automaticamente pelo `start.sh`. Cron
   real de sistema (produção com systemd) ainda não instalado.
8. ⬜ Unit systemd pra rodar a própria API (hoje só documentado/via
   `start.sh` manual, não incluído como serviço de sistema).
