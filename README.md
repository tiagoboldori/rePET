# Replay System — backend central (fase de software, sem o botão ainda)

Sistema de replay de vídeo para quadras esportivas. Este repositório cobre
a parte de **software do backend central** (100% centralizado — buffer,
corte de clipe, e a API que aciona tudo isso). O **botão físico** (ESP32 +
ESPHome + Home Assistant) ainda não foi implementado — é a próxima fase.

## Visão geral: o que está rodando e quando

Existem duas coisas bem separadas, com ciclos de vida diferentes:

1. **Captura contínua (nunca para, não depende de evento nenhum).**
   Um processo `ffmpeg` por câmera fica gravando 24/7, cortando o vídeo
   bruto em segmentos de 5s dentro do **buffer**. Isso é o que permite
   existir um "últimos 45 segundos" pra olhar pra trás a qualquer momento.
   Roda como um serviço systemd por câmera (`replay-capture@<quadra_id>`).

2. **Corte do clipe final (acionado por evento).**
   Só acontece quando alguém chama `POST /replay/{quadra_id}` na API.
   Esse é o evento — hoje simulado manualmente ou via `curl`; na versão
   final, disparado pelo Home Assistant quando o botão físico da quadra é
   pressionado. Cada chamada corta os últimos N segundos **a partir do
   instante exato em que a chamada chega** e grava um arquivo novo no
   disco persistente.

Cadeia completa prevista pro botão (ainda não implementada aqui):
```
botão físico (ESP32) --webhook genérico--> Home Assistant (automação)
                                                  |
                                                  v
                                    POST /replay/{quadra_id}  <- ESTE repositório
```
O Home Assistant é quem traduz o payload genérico do botão (`{"quadra": "..."}`)
na chamada específica pra este endpoint — o backend nunca fala com o ESP
diretamente.

## Onde os vídeos ficam salvos

Dois lugares, com papéis e ciclos de vida diferentes — configuráveis por
variável de ambiente (ver `api/config.py`):

| | Variável | Default | O que tem | Ciclo de vida |
|---|---|---|---|---|
| **Buffer bruto** | `BUFFER_ROOT` | `/var/replay` | Segmentos de 5s contínuos, um subdiretório por `quadra_id` (`<BUFFER_ROOT>/<quadra_id>/seg_*.mp4`) | Descartável — apagado pelo cron (`scripts/cleanup_segments.sh`) depois de ~3min. Em produção fica em **tmpfs** (RAM), não em disco. |
| **Clipes finais** | `OUTPUT_DIR` | `/var/replay/output` | Um arquivo por evento de replay: `<quadra_id>_<timestamp>.mp4` | Persistente, em disco de verdade. Retenção pública ainda é um placeholder (ver "Decisões pendentes" no contexto do projeto). |

O `OUTPUT_DIR` é montado pela API em `/clips` (via `StaticFiles`), então
todo clipe final já sai acessível publicamente em:
```
GET /clips/<quadra_id>_<timestamp>.mp4
```
Essa é a mesma URL que a futura página pública da quadra (`GET /quadra/{quadra_id}`,
ainda não implementada) vai usar pra listar/exibir os replays.

## Como configurar quais câmeras são capturadas

Fonte única de verdade: **`config/cameras.json`**.

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
| `SEGMENT_TIME` | `5` | Precisa bater com o valor usado por `capture_camera.sh` |
| `SAFETY_MARGIN` | `2.0` | Margem (segundos) pra considerar um segmento "fechado" |

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
  cameras.json          -> Registro central de câmeras (fonte única de verdade)
systemd/
  replay-capture@.service   -> Unit template (1 instância por câmera)
  env-examples/              -> Exemplo de .env por câmera
scripts/
  generate_camera_envs.py   -> Gera os .env de systemd a partir de cameras.json
  cleanup_segments.sh       -> Limpeza do buffer bruto (rodar via cron)
test/
  run_pipeline_test.sh   -> Testa capture + clipper direto (sem API, sem câmera real)
  run_api_test.sh        -> Testa a API real (uvicorn) + POST via curl, ponta a ponta
```

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

1. ✅ Buffer contínuo + corte de clipe (validado sem hardware).
2. ✅ Endpoint `POST /replay/{quadra_id}` acionando o corte (validado com
   requisição HTTP real).
3. ⬜ Botão físico: WT32-ETH01 + ESPHome (um microcontrolador por local,
   até 8 botões) + automação no Home Assistant chamando este endpoint.
4. ⬜ Persistência em Postgres da tabela `Replay` (hoje o corte só grava o
   arquivo; não há registro em banco ainda).
5. ⬜ `GET /quadra/{quadra_id}` pública (lista os replays recentes) e
   `/admin` protegido (HTTP Basic).
6. ⬜ Unit systemd pra rodar a própria API (hoje só documentado, não
   incluído).
