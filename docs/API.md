# API do rePET

Referência de todas as rotas, com exemplos de `curl`. A API é FastAPI e sobe na
porta `8000` (`uvicorn api.main:app --port 8000`). Nos exemplos:

- `SERVIDOR` = host/IP da máquina da API (`http://SERVIDOR:8000`);
- `loc1-quadra1` = um `quadra_id` de `config/cameras.json`;
- `SUA_LIVE_API_KEY`, `SEU_USUARIO`, `SUA_SENHA` = valores do seu `.env`
  (**nunca** coloque credencial real em arquivo versionado).

A documentação interativa (Swagger) é gerada pelo FastAPI em
`http://SERVIDOR:8000/docs`.

## Resumo

| Rota | Autenticação | Para quê |
|---|---|---|
| `POST /replay/{quadra_id}` | nenhuma | Grava (corta) o replay dos últimos segundos |
| `POST /live/{quadra_id}` | `X-Live-Key` | Liga a live no YouTube |
| `POST /live/{quadra_id}/stop` ou `DELETE /live/{quadra_id}` | `X-Live-Key` | Desliga a live |
| `GET /live/{quadra_id}` | `X-Live-Key` | Diz se a live está no ar |
| `GET /api/replays/{replay_id}` | nenhuma | Metadados de um replay |
| `GET /api/replays/{replay_id}/media` | nenhuma | Baixa/assiste o vídeo do replay |
| `GET /api/quadras/{quadra_id}/replays` | nenhuma | Lista paginada de replays da quadra |
| `DELETE /api/replays/{replay_id}` | HTTP Basic | Apaga um replay |
| `GET /clips/{arquivo}.mp4` | nenhuma | Clipe bruto direto do disco |
| `GET /quadra/{quadra_id}` | nenhuma | Página HTML de teste com os replays |
| `GET /health` | nenhuma | Healthcheck |

Erros vêm no formato `{"detail": "mensagem"}`.

---

## Gravar replay

`POST /replay/{quadra_id}`

Sem corpo e sem autenticação: é chamada direto pelo firmware do botão físico.
O servidor corta os últimos `CLIP_DURATION_SECONDS` (35 s por padrão) do buffer
da câmera, até o instante da chamada. O clipe é gerado com `-c copy`, então
pode sair um pouco mais longo que o configurado, nunca mais curto. O envio ao
Lara é feito depois, em segundo plano, pelo worker.

```bash
curl -X POST http://SERVIDOR:8000/replay/loc1-quadra1
```

Resposta `200`:

```json
{
  "quadra_id": "loc1-quadra1",
  "clip_filename": "loc1-quadra1_20261027100537.mp4",
  "clip_url": "/clips/loc1-quadra1_20261027100537.mp4"
}
```

O `replay_id` é o nome do arquivo sem `.mp4` (`loc1-quadra1_20261027100537`).
Quem chama deve usar timeout generoso (o corte leva alguns segundos; 15 s no
ESPHome).

| Código | Quando |
|---|---|
| `404` | `quadra_id` não está em `config/cameras.json` |
| `429` | A mesma quadra foi acionada há menos de `TRIGGER_COOLDOWN_SECONDS` (15 s); a mensagem diz quanto esperar |
| `500` | Não foi possível cortar (buffer vazio ou parado, ver `MAX_STALENESS_SECONDS`) |

---

## Subir a live no YouTube

A live é feita **pela própria câmera**: a API configura a câmera (ISAPI
Hikvision) para empurrar o RTMP direto ao YouTube, sem passar vídeo pelo
servidor. A stream key de cada quadra fica em `youtube_stream_key` no
`config/cameras.json`.

Todas as rotas de live exigem o header `X-Live-Key` com o valor de
`LIVE_API_KEY` (a mesma chave vale para todas as quadras).

### Ligar

`POST /live/{quadra_id}`

```bash
curl -X POST http://SERVIDOR:8000/live/loc1-quadra1 \
  -H "X-Live-Key: SUA_LIVE_API_KEY"
```

Resposta `200`:

```json
{ "quadra_id": "loc1-quadra1", "status": "iniciada", "max_seconds": 14400.0 }
```

É idempotente: se a câmera já está no ar com a mesma stream key, devolve
`"status": "ja_no_ar"` sem refazer nada. `max_seconds` é o prazo
(`LIVE_MAX_SECONDS`); passado esse tempo a API desliga a câmera sozinha
(`null` = sem limite). A imagem demora alguns segundos para aparecer no YouTube
Studio.

### Desligar

`POST /live/{quadra_id}/stop` ou `DELETE /live/{quadra_id}` (o `POST` existe
para quem só consegue fazer POST).

```bash
curl -X POST http://SERVIDOR:8000/live/loc1-quadra1/stop \
  -H "X-Live-Key: SUA_LIVE_API_KEY"

# equivalente
curl -X DELETE http://SERVIDOR:8000/live/loc1-quadra1 \
  -H "X-Live-Key: SUA_LIVE_API_KEY"
```

Resposta `200`: `{"quadra_id": "loc1-quadra1", "status": "encerrada"}`, ou
`"nao_estava_no_ar"` se já estava desligada. Ao desligar, a stream key também
é apagada da câmera.

### Ver se está no ar

`GET /live/{quadra_id}`

```bash
curl http://SERVIDOR:8000/live/loc1-quadra1 -H "X-Live-Key: SUA_LIVE_API_KEY"
# {"quadra_id":"loc1-quadra1","no_ar":true}
```

O estado é lido da própria câmera. Atenção: ela diz apenas que o envio está
*habilitado*. Se a conexão com o YouTube falhar, o `no_ar` continua `true`; a
confirmação de que a imagem chegou só existe no YouTube Studio.

| Código | Quando |
|---|---|
| `401` | Header `X-Live-Key` ausente ou errado |
| `404` | `quadra_id` não está em `config/cameras.json` |
| `422` | A quadra não tem `youtube_stream_key` no `cameras.json` (só no `POST`) |
| `502` | Câmera não respondeu/recusou, ou o DNS do YouTube não resolveu |
| `503` | `LIVE_API_KEY` não está configurada no servidor |

Observações:

- A live sobrevive a reinício da API; o prazo fica em `LIVE_STATE_FILE`
  (`.data/live_state.json`) e é reassumido quando a API sobe.
- As câmeras estão sem DNS, então a URL leva o IP do ingest do YouTube,
  resolvido a cada `POST`.
- A câmera manda o stream principal (1080p H.264) com o áudio dela (AAC mono
  16 kHz). Se o áudio da câmera estiver desligado, a live sai sem trilha de
  áudio.

---

## Consultar replays

### Metadados

`GET /api/replays/{replay_id}`

```bash
curl http://SERVIDOR:8000/api/replays/loc1-quadra1_20261027100537
```

```json
{
  "id": "loc1-quadra1_20261027100537",
  "quadra_id": "loc1-quadra1",
  "criado_em": "2026-10-27T10:05:37",
  "duracao_segundos": 35.4,
  "tamanho_bytes": 21043210,
  "media_url": "/api/replays/loc1-quadra1_20261027100537/media",
  "lara_status": "enviado",
  "lara_enviado_em": "2026-10-27T10:07:02"
}
```

`lara_status` é `pendente`, `enviado` ou `falha`. `404` se o replay não existe.

### Vídeo

`GET /api/replays/{replay_id}/media`

Serve o vídeo processado (com música) se já existir; senão, o bruto. Aceita
`Range` (`206`) e ETag, com `Cache-Control: no-cache`. `404` se o replay não
existe ou se a retenção local já removeu o arquivo.

```bash
curl -o replay.mp4 http://SERVIDOR:8000/api/replays/loc1-quadra1_20261027100537/media
```

### Listagem por quadra

`GET /api/quadras/{quadra_id}/replays?page=1&page_size=20`

Do mais recente ao mais antigo. `page_size` vai de 1 a 100 (padrão 20).

```bash
curl "http://SERVIDOR:8000/api/quadras/loc1-quadra1/replays?page=1&page_size=10"
```

```json
{
  "quadra_id": "loc1-quadra1",
  "page": 1,
  "page_size": 10,
  "total": 42,
  "items": [ { "id": "...", "media_url": "...", "lara_status": "enviado" } ]
}
```

`404` se a quadra não existe.

### Clipe bruto e página de teste

- `GET /clips/{arquivo}.mp4`: o clipe bruto, direto do disco (é o
  `clip_url` devolvido pelo `POST /replay`).
- `GET /quadra/{quadra_id}`: página HTML simples com os replays da quadra,
  para teste interno.

---

## Apagar um replay

`DELETE /api/replays/{replay_id}`: HTTP Basic com `ADMIN_USERNAME` e
`ADMIN_PASSWORD`. Remove o registro e os arquivos (bruto e processado).
Sem essas variáveis configuradas, responde `401` sempre.

```bash
curl -X DELETE -u SEU_USUARIO:SUA_SENHA \
  http://SERVIDOR:8000/api/replays/loc1-quadra1_20261027100537
```

`204` sem corpo em caso de sucesso; `404` se não existe; `401` credencial
inválida.

---

## Healthcheck

```bash
curl http://SERVIDOR:8000/health
# {"status":"ok"}
```
