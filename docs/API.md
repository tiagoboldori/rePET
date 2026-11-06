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
| `POST /broadcast/{quadra_id}/start` | `X-Live-Key` | Liga a transmissão **com placar** (até 2 ao mesmo tempo) |
| `POST /broadcast/{quadra_id}/stop` ou `DELETE /broadcast/{quadra_id}` | `X-Live-Key` | Encerra a transmissão com placar |
| `POST /broadcast/{quadra_id}/ponto/{a\|b}` | `X-Live-Key` | +1 ponto (`?delta=-1` corrige) |
| `POST /broadcast/{quadra_id}/set/{a\|b}` | `X-Live-Key` | +1 set e zera os pontos |
| `POST /broadcast/{quadra_id}/volume/{up\|down}` | `X-Live-Key` | Sobe/desce o volume com a live no ar |
| `GET /broadcast`, `GET /broadcast/{quadra_id}` | `X-Live-Key` | Estado (placar, nomes, volume, tempo no ar) |
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
O clipe termina no instante do clique: a API espera o segmento daquele
instante fechar (até ~2 s) antes de cortar. Quem chama deve usar timeout
generoso (o corte leva alguns segundos; 15 s no ESPHome).

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

## Transmissão com placar (`/broadcast`)

Diferente da live direta acima, aqui o **servidor reencoda o vídeo**: um ffmpeg
por quadra lê a câmera, desenha o placar por cima e empurra o RTMP ao YouTube.
Use a live direta (`/live`) quando não precisar de placar; ela não gasta CPU.

- **Custo medido** (câmeras reais, 1080p, x264 `ultrafast`): ~1 a 1,5 núcleo e
  ~0,4 a 0,7 GB de RAM por transmissão. Limite de **2 ao mesmo tempo**
  (`BROADCAST_MAX_CONCURRENT`); a terceira recebe `409`.
- Todas as câmeras saem em **1080p** (a quadra2 entrega 1440p e é reduzida).
- Atraso da imagem: alguns segundos a mais que a live direta (reencode).
- Mesma chave `X-Live-Key` e mesmo prazo `LIVE_MAX_SECONDS` da live direta.
- Uma quadra não pode estar nos dois modos: ligar um com o outro no ar dá `409`.
- **Sobrevive a reinício da API** (o ffmpeg roda destacado e é readotado), e se o
  ffmpeg cair, a API o relança sozinha (campo `reinicios`).

Lados são sempre `a` e `b`. O placar é simples: pontos e sets, números inteiros.

### Ligar e desligar

```bash
# jogo novo (placar 0x0, volume 100%); o corpo é opcional
curl -X POST http://SERVIDOR:8000/broadcast/loc1-quadra1/start \
  -H "X-Live-Key: SUA_LIVE_API_KEY" -H "Content-Type: application/json" \
  -d '{"nome_a": "Ana e Bia", "nome_b": "Cris e Duda"}'

curl -X POST http://SERVIDOR:8000/broadcast/loc1-quadra1/stop -H "X-Live-Key: SUA_LIVE_API_KEY"
```

Corpo do `start` (todos opcionais): `nome_a`, `nome_b` (até 14 caracteres,
maiúsculas), `reset` (padrão `true`: placar 0x0 **e volume 100%**; com `false`
mantém placar e volume, útil para religar o mesmo jogo) e `volume` (% do da câmera).
É idempotente: já no ar devolve `"status": "ja_no_ar"`.

### Placar

```bash
curl -X POST http://SERVIDOR:8000/broadcast/loc1-quadra1/ponto/a -H "X-Live-Key: ..."
curl -X POST http://SERVIDOR:8000/broadcast/loc1-quadra1/ponto/b -H "X-Live-Key: ..."
curl -X POST "http://SERVIDOR:8000/broadcast/loc1-quadra1/ponto/a?delta=-1" -H "X-Live-Key: ..."  # corrige
curl -X POST http://SERVIDOR:8000/broadcast/loc1-quadra1/set/a -H "X-Live-Key: ..."   # +1 set e zera os pontos dos DOIS lados
curl -X POST "http://SERVIDOR:8000/broadcast/loc1-quadra1/set/a?delta=-1" -H "X-Live-Key: ..."  # só tira 1 set
curl -X PUT  http://SERVIDOR:8000/broadcast/loc1-quadra1/placar -H "X-Live-Key: ..." \
  -H "Content-Type: application/json" -d '{"pontos_a": 40, "pontos_b": 15, "sets_a": 1}'  # correção manual
curl -X POST http://SERVIDOR:8000/broadcast/loc1-quadra1/placar/zerar -H "X-Live-Key: ..."
curl -X PUT  http://SERVIDOR:8000/broadcast/loc1-quadra1/nomes -H "X-Live-Key: ..." \
  -H "Content-Type: application/json" -d '{"nome_a": "Ana e Bia"}'
```

O placar aparece no vídeo em menos de 1 s depois da chamada, e pode ser
ajustado antes de ligar. Pontos e sets nunca ficam negativos.

### Volume

```bash
curl -X POST http://SERVIDOR:8000/broadcast/loc1-quadra1/volume/up   -H "X-Live-Key: ..."   # +10 pontos percentuais
curl -X POST "http://SERVIDOR:8000/broadcast/loc1-quadra1/volume/down?passo=20" -H "X-Live-Key: ..."
curl -X PUT  http://SERVIDOR:8000/broadcast/loc1-quadra1/volume -H "X-Live-Key: ..." \
  -H "Content-Type: application/json" -d '{"nivel": 150}'
```

`nivel` é % do volume da câmera (100 = original, 0 = mudo, máximo
`BROADCAST_VOLUME_MAX`=400). Vale na hora, sem derrubar a live. Há um limitador
de pico, então subir demais satura com menos distorção, mas não perfeito: o
microfone da câmera já chega perto de 0 dBFS em jogo.

### Resposta (todas as rotas devolvem o estado)

```json
{
  "quadra_id": "loc1-quadra1", "no_ar": true,
  "nomes": {"a": "ANA E BIA", "b": "CRIS E DUDA"},
  "placar": {"a": {"pontos": 2, "sets": 1}, "b": {"pontos": 1, "sets": 0}},
  "volume": 100, "no_ar_ha_segundos": 312, "encerra_em_segundos": 6888, "reinicios": 0,
  "audio_da_camera": true
}
```

| Código | Quando |
|---|---|
| `401` / `503` | Chave ausente/errada / `LIVE_API_KEY` não configurada |
| `404` | `quadra_id` não está em `config/cameras.json` |
| `409` | Limite de 2 transmissões, ou a quadra está em live direta (e vice-versa) |
| `422` | Lado diferente de `a`/`b`, ou a quadra sem `youtube_stream_key` |
| `502` | A câmera não respondeu ao checar a live direta |
| `503` | O ffmpeg não estava lendo comandos (reiniciando); tente de novo |

Log do ffmpeg de cada quadra: `logs/broadcast_{quadra_id}.log`.

**Áudio.** A cada início (e a cada relançamento) a API sonda a câmera. Se ela
estiver **sem trilha de áudio**, a transmissão sobe com uma trilha de silêncio em
vez de falhar, e a resposta traz `"audio_da_camera": false` (com `true` a live usa
o som da câmera; `null` = ainda não sondado ou transmissão readotada após reinício
da API). O volume só tem efeito quando há áudio da câmera. Dica: um volume 0 deixa
a live muda; `start` com `reset=true` (padrão) volta o volume a 100%.

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
