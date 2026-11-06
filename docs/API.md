# API do rePET

Referência de todas as rotas, com exemplos de requisição (`curl`), corpo e
resposta. A API é FastAPI e sobe na porta `8000`
(`uvicorn api.main:app --port 8000`).

A documentação interativa (Swagger), gerada pelo FastAPI, fica em
`http://SERVIDOR:8000/docs`.

## Convenções dos exemplos

- `SERVIDOR` = host/IP da máquina da API (`http://SERVIDOR:8000`);
- `loc1-quadra1` = um `quadra_id` de `config/cameras.json`;
- `SEU_TOKEN` = uma chave de `API_TOKENS`, a `LIVE_API_KEY` **ou** o `REPLAY_API_TOKEN` do seu `.env` (veja [Autenticação](#autenticação));
- `SEU_USUARIO`, `SUA_SENHA` = `ADMIN_USERNAME`/`ADMIN_PASSWORD` do seu `.env`
  (**nunca** coloque credencial real em arquivo versionado);
- os valores nas respostas (tempos, ids, tamanhos) são ilustrativos.

## Índice

- [Resumo das rotas](#resumo)
- [Autenticação](#autenticação)
- [Formato dos erros](#formato-dos-erros)
- [Gravar replay](#gravar-replay)
- [Subir a live no YouTube](#subir-a-live-no-youtube)
- [Configurar o destino da transmissão (stream key e RTMP)](#configurar-o-destino-da-transmissão-stream-key-e-rtmp)
- [Transmissão com placar](#transmissão-com-placar-broadcast)
  - [Áudio: corte de agudos e redução de ruído](#áudio-corte-de-agudos-e-redução-de-ruído)
- [Estado geral](#estado-geral-get-status)
- [Painel web](#painel-web)
- [Consultar replays](#consultar-replays)
- [Apagar um replay](#apagar-um-replay)
- [Healthcheck](#healthcheck)
- [Exemplo de ponta a ponta: um jogo](#exemplo-de-ponta-a-ponta-um-jogo)

## Resumo

| Rota | Autenticação | Para quê |
|---|---|---|
| `POST /replay/{quadra_id}` | nenhuma | Grava (corta) o replay dos últimos segundos |
| `POST /live/{quadra_id}` | `Bearer` | Liga a live no YouTube |
| `POST /live/{quadra_id}/stop` ou `DELETE /live/{quadra_id}` | `Bearer` | Desliga a live |
| `GET /live/{quadra_id}` | `Bearer` | Diz se a live está no ar |
| `GET /quadras/{quadra_id}/youtube` | `Bearer` | Destino da transmissão da quadra (key mascarada e URL RTMP) |
| `PUT /quadras/{quadra_id}/youtube` | `Bearer` | Troca a stream key e/ou a URL RTMP da quadra (não vale com ela no ar) |
| `GET`/`PUT /quadras/{quadra_id}/youtube/key` | `Bearer` | Lê (**inteira**) e troca só a stream key |
| `GET`/`PUT /quadras/{quadra_id}/youtube/url` | `Bearer` | Lê e troca só a URL RTMP (`null` volta à global) |
| `POST /broadcast/{quadra_id}/start` | `Bearer` | Liga a transmissão **com placar** (até 2 ao mesmo tempo) |
| `POST /broadcast/{quadra_id}/stop` ou `DELETE /broadcast/{quadra_id}` | `Bearer` | Encerra a transmissão com placar |
| `POST /broadcast/{quadra_id}/ponto/{a\|b}` | `Bearer` | +1 ponto (`?delta=-1` corrige) |
| `POST /broadcast/{quadra_id}/set/{a\|b}` | `Bearer` | +1 set e zera os pontos |
| `PUT /broadcast/{quadra_id}/placar` | `Bearer` | Correção manual do placar |
| `POST /broadcast/{quadra_id}/placar/zerar` | `Bearer` | Zera pontos e sets |
| `PUT /broadcast/{quadra_id}/nomes` | `Bearer` | Troca os nomes das duplas |
| `POST /broadcast/{quadra_id}/volume/{up\|down}` | `Bearer` | Sobe/desce o volume com a live no ar |
| `PUT /broadcast/{quadra_id}/volume` | `Bearer` | Define o volume |
| `GET`/`PUT /broadcast/{quadra_id}/audio` | `Bearer` | Lê e troca o corte de agudos e a redução de ruído (valem ao vivo, sem relançar) |
| `GET /broadcast`, `GET /broadcast/{quadra_id}` | `Bearer` | Estado (placar, nomes, volume, tempo no ar) |
| `GET /status` | `Bearer` ou sessão do painel | Uma linha por quadra com os dois modos (direta e placar) |
| `GET /painel` | página pública; login dentro | Painel web de controle da transmissão (porta 8000, a mesma da API) |
| `POST /painel/login`, `POST /painel/logout`, `GET /painel/me` | credencial `ADMIN_*` / cookie | Sessão do painel |
| `GET /api/replays/{replay_id}` | nenhuma | Metadados de um replay |
| `GET /api/replays/{replay_id}/media` | nenhuma | Baixa/assiste o vídeo do replay |
| `GET /api/quadras/{quadra_id}/replays` | nenhuma | Lista paginada de replays da quadra |
| `DELETE /api/replays/{replay_id}` | HTTP Basic | Apaga um replay |
| `GET /clips/{arquivo}.mp4` | nenhuma | Clipe bruto direto do disco |
| `GET /quadra/{quadra_id}` | nenhuma | Página HTML de teste com os replays |
| `GET /health` | nenhuma | Healthcheck |

---

## Autenticação

São quatro mecanismos, conforme a rota:

| Mecanismo | Onde se usa | Como enviar |
|---|---|---|
| **Bearer token** (recomendado) | `/live`, `/broadcast`, `/status` (integrações e scripts) | Header `Authorization: Bearer <token>` |
| **`X-Live-Key`** (legado) | as mesmas rotas | Header `X-Live-Key: <LIVE_API_KEY>`; continua valendo, mas prefira o Bearer |
| **Sessão do painel** | as mesmas rotas, quando chamadas pelo painel web | Cookie `repet_painel` (criado em `POST /painel/login`) + header `X-Painel: 1` nas rotas que **mudam** algo |
| **HTTP Basic** | `DELETE /api/replays/{id}` | `-u SEU_USUARIO:SUA_SENHA` (`ADMIN_USERNAME` / `ADMIN_PASSWORD`) |

### Qual token mandar no Bearer

O servidor aceita **qualquer um** destes valores como token, e todos abrem exatamente as mesmas
rotas:

| Token | Variável no servidor | Quando usar |
|---|---|---|
| Chave nomeada de integração | `API_TOKENS` (`nome:token,nome2:token2`) | **Recomendado para cada integração nova**: uma chave por sistema, revogável sozinha |
| Token próprio da API de live | `LIVE_API_KEY` | Scripts internos; é também a chave do header legado `X-Live-Key` |
| Token do Lara | `REPLAY_API_TOKEN` (o Sanctum, gerado com `php artisan replay:token`) | Quando o Lara já tem esse token e prefere não guardar um segundo segredo |

Gerar e cadastrar uma chave nova (o `nome` é só um rótulo; o que vai no Bearer é o token):

```bash
python3 -c "import secrets; print('repet_live_' + secrets.token_urlsafe(32))"
# acrescente ao .env do servidor (uma linha, pares separados por vírgula):
# API_TOKENS=painel-web:repet_live_xxxxxxxx
# reinicie a API para valer. Para revogar, apague o par e reinicie.
```

O token do Lara é enviado inteiro, com o `id|` na frente (por exemplo `7|abc...`). A
variável que não estiver configurada simplesmente não é aceita (nunca vale vazio).

```http
POST /live/loc1-quadra1 HTTP/1.1
Host: SERVIDOR:8000
Authorization: Bearer SEU_TOKEN
```

```bash
curl -X POST http://SERVIDOR:8000/live/loc1-quadra1 \
  -H "Authorization: Bearer SEU_TOKEN"
```

Regras:

- Se a requisição trouxer `Authorization: Bearer ...`, **só ele é avaliado**: token errado dá `401`
  mesmo que venham também um `X-Live-Key` correto ou um cookie válido.
- O esquema `Bearer` não diferencia maiúsculas (`bearer` vale). Outro esquema em `Authorization`
  (por exemplo `Basic`) é ignorado nessas rotas.
- O token do Lara e as chaves de `API_TOKENS` **só** valem via `Bearer`; no `X-Live-Key` continua valendo só a `LIVE_API_KEY`.
- Rotas fora de `/live`, `/broadcast` e `/status` **não** usam Bearer: `POST /replay/{quadra_id}` e as
  consultas de replay seguem abertas, e o `DELETE` de replay segue com HTTP Basic.
- O token dá controle total das transmissões. Guarde-o só no **servidor** da aplicação que integra
  (nunca no navegador, no JavaScript do front, em URL ou em log). Veja o CORS abaixo.
- O tráfego é HTTP puro: fora da rede interna, ponha um proxy HTTPS na frente, senão o token trafega
  em claro.

**CORS.** A API não habilita CORS. Chamadas feitas direto do navegador (de outra origem) são
bloqueadas por ele, e isso é intencional: a integração deve chamar a API a partir do **backend** da
aplicação (no Lara, um controller/serviço Laravel), que repassa o resultado ao front.

Respostas de falha de autenticação:

| Situação | Código | Corpo |
|---|---|---|
| Bearer errado ou vazio | `401` (com `WWW-Authenticate: Bearer`) | `{"detail": "Token inválido."}` |
| `X-Live-Key` errada | `401` | `{"detail": "Chave inválida."}` |
| Sem credencial e sem sessão | `401` (com `WWW-Authenticate: Bearer`) | `{"detail": "Chave inválida ou sessão ausente."}` |
| Sessão (cookie) sem o header `X-Painel: 1` em rota que muda algo | `403` | `{"detail": "Falta o header X-Painel."}` |
| Nenhum token (`LIVE_API_KEY`, `REPLAY_API_TOKEN`, `API_TOKENS`) configurado no servidor | `503` | `{"detail": "Nenhum token configurado (LIVE_API_KEY / REPLAY_API_TOKEN / API_TOKENS)."}` |
| `X-Live-Key` enviada, mas `LIVE_API_KEY` não configurada | `503` | `{"detail": "LIVE_API_KEY não configurada."}` |
| HTTP Basic errado ou não configurado | `401` (com `WWW-Authenticate: Basic`) | `{"detail": "Credencial inválida."}` |

## Formato dos erros

Os erros que a API devolve de propósito têm sempre este formato:

```json
{ "detail": "mensagem em português" }
```

A **única exceção** é a validação automática do FastAPI (corpo malformado,
número inválido em `?delta=`, campo obrigatório ausente...), que responde `422`
com uma lista:

```json
{
  "detail": [
    {
      "type": "missing",
      "loc": ["body", "nivel"],
      "msg": "Field required",
      "input": {}
    }
  ]
}
```

Lembre de tratar os dois formatos: `detail` é `string` na maioria dos casos e
`array` nesse caso.

Nas mensagens de `404` de quadra, o servidor escreve o **caminho completo** do
`cameras.json` (por exemplo `/home/.../config/cameras.json`); os exemplos abaixo
abreviam para `config/cameras.json`.

---

## Gravar replay

`POST /replay/{quadra_id}`

Sem corpo e sem autenticação: é chamada direto pelo firmware do botão físico.
O servidor corta os últimos `CLIP_DURATION_SECONDS` (35 s por padrão) do buffer
da câmera, até o instante da chamada. O clipe é gerado com `-c copy`, então
pode sair um pouco mais longo que o configurado, nunca mais curto. O envio ao
Lara é feito depois, em segundo plano, pelo worker.

| Parte | Valor |
|---|---|
| Path | `quadra_id`: id da quadra em `config/cameras.json` |
| Headers | nenhum obrigatório |
| Corpo | nenhum |

**Requisição**

```bash
curl -X POST http://SERVIDOR:8000/replay/loc1-quadra1 --max-time 15
```

**Resposta `200`**

```json
{
  "quadra_id": "loc1-quadra1",
  "clip_filename": "loc1-quadra1_20261027100537.mp4",
  "clip_url": "/clips/loc1-quadra1_20261027100537.mp4"
}
```

| Campo | Tipo | Descrição |
|---|---|---|
| `quadra_id` | string | A quadra acionada |
| `clip_filename` | string | Nome do arquivo gerado (`{quadra_id}_{AAAAMMDDHHMMSS}.mp4`) |
| `clip_url` | string | Caminho do clipe bruto (`GET /clips/...`); relativo ao servidor |

O `replay_id` é o nome do arquivo sem `.mp4` (`loc1-quadra1_20261027100537`);
use-o em `GET /api/replays/{replay_id}`. O clipe termina no instante do clique:
a API espera o segmento daquele instante fechar (até ~2 s) antes de cortar.
Quem chama deve usar timeout generoso (o corte leva alguns segundos; 15 s no
ESPHome).

**Erros**

| Código | Quando | Exemplo de corpo |
|---|---|---|
| `404` | `quadra_id` não está em `config/cameras.json` | `{"detail": "quadra_id 'loc9-x' não está em config/cameras.json. Confira config/cameras.json."}` |
| `429` | A mesma quadra foi acionada há menos de `TRIGGER_COOLDOWN_SECONDS` (15 s) | `{"detail": "Aguarde mais 9.4s antes de acionar 'loc1-quadra1' de novo (intervalo mínimo: 15s)."}` |
| `500` | Não foi possível cortar (buffer vazio ou parado, ver `MAX_STALENESS_SECONDS`) | `{"detail": "<motivo do corte>"}` |

---

## Subir a live no YouTube

A live é feita **pela própria câmera**: a API configura a câmera (ISAPI
Hikvision) para empurrar o RTMP direto ao YouTube, sem passar vídeo pelo
servidor. A stream key de cada quadra fica em `youtube_stream_key` no
`config/cameras.json` e pode ser consultada e trocada pela
[API de destino da transmissão](#configurar-o-destino-da-transmissão-stream-key-e-rtmp).

Todas as rotas de live exigem `Authorization: Bearer <token>` (a `LIVE_API_KEY` ou o
`REPLAY_API_TOKEN`; o mesmo token vale para todas as quadras; o header legado `X-Live-Key` também funciona). As rotas de `/live` e
`/broadcast` também aceitam a **sessão do [painel web](#painel-web)** (cookie), que
é como o painel as usa sem expor a chave. Os dois modos, quando usar cada um e os
casos de borda estão em [`docs/LIVE.md`](LIVE.md).

Nenhuma das três rotas tem corpo.

### Ligar

`POST /live/{quadra_id}`

**Requisição**

```bash
curl -X POST http://SERVIDOR:8000/live/loc1-quadra1 \
  -H "Authorization: Bearer SEU_TOKEN"
```

**Resposta `200`**

```json
{ "quadra_id": "loc1-quadra1", "status": "iniciada", "max_seconds": 7200.0 }
```

| Campo | Tipo | Descrição |
|---|---|---|
| `quadra_id` | string | A quadra |
| `status` | string | `iniciada` ou `ja_no_ar` |
| `max_seconds` | número ou `null` | Prazo (`LIVE_MAX_SECONDS`, padrão 7200); `null` = sem limite |

É idempotente: se a câmera já está no ar com a mesma stream key, devolve
`"status": "ja_no_ar"` sem refazer nada:

```json
{ "quadra_id": "loc1-quadra1", "status": "ja_no_ar", "max_seconds": 7200.0 }
```

Passado o prazo, a API desliga a câmera sozinha. A imagem demora alguns
segundos para aparecer no YouTube Studio.

### Desligar

`POST /live/{quadra_id}/stop` ou `DELETE /live/{quadra_id}` (o `POST` existe
para quem só consegue fazer POST).

**Requisição**

```bash
curl -X POST http://SERVIDOR:8000/live/loc1-quadra1/stop \
  -H "Authorization: Bearer SEU_TOKEN"

# equivalente
curl -X DELETE http://SERVIDOR:8000/live/loc1-quadra1 \
  -H "Authorization: Bearer SEU_TOKEN"
```

**Resposta `200`**

```json
{ "quadra_id": "loc1-quadra1", "status": "encerrada" }
```

`status` é `encerrada`, ou `nao_estava_no_ar` se já estava desligada. Ao
desligar, a stream key também é apagada da câmera.

### Ver se está no ar

`GET /live/{quadra_id}`

**Requisição**

```bash
curl http://SERVIDOR:8000/live/loc1-quadra1 -H "Authorization: Bearer SEU_TOKEN"
```

**Resposta `200`**

```json
{ "quadra_id": "loc1-quadra1", "no_ar": true }
```

O estado é lido da própria câmera (pode levar alguns segundos se ela estiver
lenta). Atenção: ela diz apenas que o envio está *habilitado*. Se a conexão com o
YouTube falhar, o `no_ar` continua `true`; a confirmação de que a imagem chegou
só existe no YouTube Studio. Para um estado que não espera a câmera, use
[`GET /status`](#estado-geral-get-status).

### Erros de `/live`

| Código | Quando | Exemplo de corpo |
|---|---|---|
| `401` | Token ausente ou errado | `{"detail": "Token inválido."}` (ou `"Chave inválida ou sessão ausente."` se nada foi enviado) |
| `404` | `quadra_id` não está em `config/cameras.json` | `{"detail": "quadra_id 'loc9-x' não está em config/cameras.json."}` |
| `409` | (`POST`) A quadra está em transmissão com placar, ou outra quadra de **mesma stream key** já está no ar | `{"detail": "'loc1-quadra1' está em transmissão com placar; encerre-a antes (POST /broadcast/loc1-quadra1/stop)."}` |
| `422` | (`POST`) A quadra não tem `youtube_stream_key` no `cameras.json` | `{"detail": "'loc1-quadra2' não tem youtube_stream_key em config/cameras.json."}` |
| `502` | Câmera não respondeu/recusou, ou o DNS do YouTube não resolveu | `{"detail": "<motivo devolvido pela câmera>"}` |
| `503` | Nenhum token (`LIVE_API_KEY` / `REPLAY_API_TOKEN` / `API_TOKENS`) configurado no servidor | `{"detail": "Nenhum token configurado (LIVE_API_KEY / REPLAY_API_TOKEN / API_TOKENS)."}` |

Observações:

- A live sobrevive a reinício da API; o prazo fica em `LIVE_STATE_FILE`
  (`.data/live_state.json`) e é reassumido quando a API sobe.
- As câmeras estão sem DNS, então a URL leva o IP do ingest do YouTube,
  resolvido a cada `POST`.
- A câmera manda o stream principal (1080p H.264) com o áudio dela (AAC mono
  16 kHz). Se o áudio da câmera estiver desligado, a live sai sem trilha de
  áudio.

---

## Configurar o destino da transmissão (stream key e RTMP)

Cada quadra transmite para o destino cadastrado nela: a **stream key** (`youtube_stream_key`) e,
opcionalmente, a **URL RTMP** (`rtmp_url`; sem ela vale a global `YOUTUBE_RTMP_URL`). Estas duas rotas
leem e trocam esses valores no `config/cameras.json`, e valem para a live direta **e** para a
transmissão com placar. Quem liga a transmissão continua **sem** enviar a key: ela é lida daqui.

- A rota **combinada** abaixo nunca devolve a key inteira: o `GET` mostra só se existe e os 4 últimos
  caracteres (e só se a key tiver 16 caracteres ou mais). Para ler a key inteira, use a rota
  [`/youtube/key`](#rotas-separadas-só-a-key-só-a-url).
- A troca vale **na hora** (a próxima vez que a quadra for ligada já usa o valor novo).
- **Não é possível trocar com a quadra transmitindo** (live direta ou com placar): responde `409`.
  Encerre a transmissão, troque e ligue de novo.
- Exigem Bearer, como as demais rotas de controle. Quem trocou fica registrado no log do servidor
  (`[youtube] ...`), sem a key.

### Ver: `GET /quadras/{quadra_id}/youtube`

```bash
curl -s http://SERVIDOR:8000/quadras/loc1-quadra1/youtube \
  -H "Authorization: Bearer SEU_TOKEN"
```

```json
{
  "quadra_id": "loc1-quadra1",
  "tem_stream_key": true,
  "stream_key_final": "qrst",
  "rtmp_url": "rtmp://a.rtmp.youtube.com/live2",
  "rtmp_url_padrao": true,
  "rtmp_hosts_permitidos": ["youtube.com", "a.rtmp.youtube.com"],
  "chave_compartilhada_com": [],
  "chave_em_uso_por": null,
  "no_ar": false
}
```

| Campo | Significado |
|---|---|
| `tem_stream_key` | A quadra tem key (sem ela, ligar qualquer modo dá `422`) |
| `stream_key_final` | Últimos 4 caracteres da key, ou `null` (sem key, ou key curta demais para mostrar) |
| `rtmp_url` | URL RTMP **efetiva** (a da quadra, ou a global) |
| `rtmp_url_padrao` | `true` = a quadra usa a global; `false` = tem URL própria |
| `rtmp_hosts_permitidos` | Hosts que o `PUT` aceita em `rtmp_url` (veja abaixo) |
| `chave_compartilhada_com` | Outras quadras com a **mesma** key (permitido; só uma pode estar no ar por vez) |
| `chave_em_uso_por` | Qual delas está no ar agora (`null` = nenhuma) |
| `no_ar` | Esta quadra está transmitindo em algum modo (estado em cache, até ~15 s de atraso). Se `true`, o `PUT` dará `409` |

### Trocar: `PUT /quadras/{quadra_id}/youtube`

Corpo JSON; envie **pelo menos um** campo (o que não vier não muda):

| Campo | Tipo | Descrição |
|---|---|---|
| `stream_key` | string | Nova key: 8 a 128 caracteres entre letras, números, `-` e `_` (sem espaços). **Não pode ser `null`**: a API troca a key, não a remove |
| `rtmp_url` | string ou `null` | Nova URL, no formato `rtmp://host[:porta][/app]`. **`null` apaga a URL da quadra** e volta à global |

Regras da `rtmp_url` (existem porque o servidor e a câmera passam a empurrar o vídeo para lá):

- só `rtmp://` (não `rtmps://`), sem usuário, senha, query ou fragmento; a key vai em `stream_key`, não na URL;
- o host precisa estar na lista `rtmp_hosts_permitidos`: por padrão `youtube.com` e seus subdomínios
  (`a.rtmp.youtube.com`, `b.rtmp.youtube.com`...), mais o host da `YOUTUBE_RTMP_URL` global. Para liberar
  outro destino, quem administra o servidor acrescenta o domínio em `RTMP_ALLOWED_HOSTS` no `.env` e
  reinicia a API. IP só vale se estiver listado por inteiro;
- a URL é normalizada (host em minúsculas, sem `/` no fim).

```bash
# trocar só a key
curl -s -X PUT http://SERVIDOR:8000/quadras/loc1-quadra2/youtube \
  -H "Authorization: Bearer SEU_TOKEN" -H "Content-Type: application/json" \
  -d '{"stream_key": "abcd-efgh-ijkl-mnop-qrst"}'

# trocar key e servidor RTMP de uma vez
curl -s -X PUT http://SERVIDOR:8000/quadras/loc1-quadra2/youtube \
  -H "Authorization: Bearer SEU_TOKEN" -H "Content-Type: application/json" \
  -d '{"stream_key": "abcd-efgh-ijkl-mnop-qrst", "rtmp_url": "rtmp://b.rtmp.youtube.com/live2"}'

# voltar a quadra à URL global
curl -s -X PUT http://SERVIDOR:8000/quadras/loc1-quadra2/youtube \
  -H "Authorization: Bearer SEU_TOKEN" -H "Content-Type: application/json" \
  -d '{"rtmp_url": null}'
```

**Resposta `200`**: o mesmo objeto do `GET`, com `status` na frente: `"atualizada"`, ou `"sem_mudanca"`
se o valor enviado já era o atual (nada é regravado).

```json
{
  "status": "atualizada",
  "quadra_id": "loc1-quadra2",
  "tem_stream_key": true,
  "stream_key_final": "qrst",
  "rtmp_url": "rtmp://b.rtmp.youtube.com/live2",
  "rtmp_url_padrao": false,
  "rtmp_hosts_permitidos": ["youtube.com", "a.rtmp.youtube.com"],
  "chave_compartilhada_com": [],
  "chave_em_uso_por": null,
  "no_ar": false
}
```

**Erros**

| Código | Quando | `detail` de exemplo |
|---|---|---|
| `401` / `503` | Token ausente/errado; nenhum token configurado | ver [Autenticação](#autenticação) |
| `404` | `quadra_id` não está em `config/cameras.json` | `quadra_id 'x' não está em .../cameras.json.` |
| `409` | A quadra está transmitindo | `'loc1-quadra2' está transmitindo; encerre a transmissão antes de trocar a key ou a URL RTMP.` |
| `422` | Corpo sem `stream_key` nem `rtmp_url` | `informe stream_key e/ou rtmp_url.` |
| `422` | `stream_key` nula | `stream_key não pode ser nula: a API troca a key, não a remove.` |
| `422` | `stream_key` fora do formato | `stream_key inválida: use de 8 a 128 caracteres entre letras, números, '-' e '_' (sem espaços).` |
| `422` | `rtmp_url` fora do formato ou com host não permitido | `host 'evil.example.com' não permitido; hosts aceitos: youtube.com, a.rtmp.youtube.com (ajuste RTMP_ALLOWED_HOSTS no servidor para liberar outro).` |
| `422` | Tipo errado (ex.: `stream_key` numérica): `detail` é **lista** | ver [Formato dos erros](#formato-dos-erros) |
| `500` | Não foi possível ler ou gravar o `cameras.json` | `não consegui gravar a configuração das câmeras.` |

As mensagens de erro nunca repetem a key enviada. Se o `409` aparecer, consulte `GET /status` para ver
em que modo a quadra está e encerre com `/live/{quadra_id}/stop` ou `/broadcast/{quadra_id}/stop`.

### Rotas separadas: só a key, só a URL

Além da rota combinada acima (que **não** devolve a key inteira), há uma rota para cada valor. As duas
seguem as mesmas regras: Bearer, validação, `409` com a quadra transmitindo, gravação atômica e registro no
log de quem leu/trocou (`[youtube] ...`, sem a key).

| Rota | Para quê |
|---|---|
| `GET /quadras/{quadra_id}/youtube/key` | A stream key, **inteira** |
| `PUT /quadras/{quadra_id}/youtube/key` | Troca a stream key |
| `GET /quadras/{quadra_id}/youtube/url` | A URL RTMP efetiva |
| `PUT /quadras/{quadra_id}/youtube/url` | Troca a URL RTMP (`null` volta à global) |

> **Atenção:** `GET .../youtube/key` devolve a credencial do canal em claro. Foi uma decisão consciente
> (a aplicação roda em rede fechada e a rota exige token), mas qualquer pessoa ou sistema que tenha um
> token passa a poder ler a key, e ela aparecerá nas respostas e nos históricos de quem chamar. Cada leitura
> fica registrada no log. Se um dia a aplicação sair da rede fechada, volte a usar só a rota combinada.

#### Key: `GET` e `PUT /quadras/{quadra_id}/youtube/key`

```bash
curl -s http://SERVIDOR:8000/quadras/loc1-quadra1/youtube/key \
  -H "Authorization: Bearer SEU_TOKEN"
```

```json
{
  "quadra_id": "loc1-quadra1",
  "tem_stream_key": true,
  "stream_key": "abcd-efgh-ijkl-mnop-qrst",
  "chave_compartilhada_com": [],
  "no_ar": false
}
```

Quadra sem key: `"tem_stream_key": false` e `"stream_key": null`. `chave_compartilhada_com` lista as outras
quadras com a mesma key; `no_ar: true` significa que o `PUT` dará `409`.

```bash
curl -s -X PUT http://SERVIDOR:8000/quadras/loc1-quadra1/youtube/key \
  -H "Authorization: Bearer SEU_TOKEN" -H "Content-Type: application/json" \
  -d '{"stream_key": "abcd-efgh-ijkl-mnop-qrst"}'
```

Corpo: `{"stream_key": "..."}`, **obrigatório**, 8 a 128 caracteres entre letras, números, `-` e `_`; `null` é
recusado (a API troca a key, não a remove). Resposta `200`: o mesmo objeto do `GET` com `status` na frente
(`"atualizada"` ou `"sem_mudanca"`).

#### URL: `GET` e `PUT /quadras/{quadra_id}/youtube/url`

```bash
curl -s http://SERVIDOR:8000/quadras/loc1-quadra1/youtube/url \
  -H "Authorization: Bearer SEU_TOKEN"
```

```json
{
  "quadra_id": "loc1-quadra1",
  "rtmp_url": "rtmp://a.rtmp.youtube.com/live2",
  "rtmp_url_padrao": true,
  "rtmp_url_global": "rtmp://a.rtmp.youtube.com/live2",
  "rtmp_hosts_permitidos": ["youtube.com", "a.rtmp.youtube.com"],
  "no_ar": false
}
```

`rtmp_url` é a efetiva; `rtmp_url_padrao: true` = a quadra usa a global (`rtmp_url_global`). Esta rota
**nunca** mostra a key.

```bash
# URL própria da quadra
curl -s -X PUT http://SERVIDOR:8000/quadras/loc1-quadra1/youtube/url \
  -H "Authorization: Bearer SEU_TOKEN" -H "Content-Type: application/json" \
  -d '{"rtmp_url": "rtmp://b.rtmp.youtube.com/live2"}'

# voltar à global
curl -s -X PUT http://SERVIDOR:8000/quadras/loc1-quadra1/youtube/url \
  -H "Authorization: Bearer SEU_TOKEN" -H "Content-Type: application/json" \
  -d '{"rtmp_url": null}'
```

Corpo: `{"rtmp_url": "rtmp://host[:porta][/app]"}` ou `{"rtmp_url": null}`, **obrigatório** (corpo sem o campo dá
`422`). Só `rtmp://`, host em `rtmp_hosts_permitidos`, sem usuário/senha/query. Resposta `200`: o mesmo objeto
do `GET` com `status` na frente.

**Erros** (os mesmos da rota combinada, mais estes dois): `422` `informe stream_key.` e `422`
`informe rtmp_url (use null para voltar à URL global).` quando o campo obrigatório falta.

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
- Mesma autorização (Bearer) e mesmo prazo `LIVE_MAX_SECONDS` da live direta.
- Uma quadra não pode estar nos dois modos: ligar um com o outro no ar dá `409`.
- **Sobrevive a reinício da API** (o ffmpeg roda destacado e é readotado), e se o
  ffmpeg cair, a API o relança sozinha (campo `reinicios`).

O placar aparece numa caixa **grená** no canto inferior esquerdo do vídeo (a cor vale quando a transmissão
é iniciada ou relançada, não muda numa que já está no ar). Lados são sempre `a` e `b` (aceita maiúscula). O placar é simples: pontos e
sets, números inteiros que **nunca ficam negativos** (valores abaixo de 0 viram 0).

### Todas as rotas `/broadcast`

Todas exigem `Authorization: Bearer` (ou sessão do painel) e `quadra_id` existente em `cameras.json`.
Todas devolvem o [objeto de estado](#objeto-de-estado) (o `start`/`stop` somam `status`).

| Método e rota | Corpo / parâmetros | O que faz |
|---|---|---|
| `GET /broadcast` | | Todas as quadras, quem está no ar e o limite |
| `GET /broadcast/{quadra_id}` | | Estado de uma quadra |
| `POST /broadcast/{quadra_id}/start` | opcional: `nome_a`, `nome_b`, `reset` (padrão `true`), `volume` | Liga (idempotente) |
| `POST /broadcast/{quadra_id}/stop` · `DELETE /broadcast/{quadra_id}` | | Encerra |
| `POST /broadcast/{quadra_id}/ponto/{lado}` | `?delta=1` (`-1` corrige) | Ponto em `a` ou `b` |
| `POST /broadcast/{quadra_id}/set/{lado}` | `?delta=1` (`-1` só tira o set) | +1 set e zera os pontos dos dois lados |
| `PUT /broadcast/{quadra_id}/placar` | `pontos_a`, `pontos_b`, `sets_a`, `sets_b` (os que vierem) | Correção manual |
| `POST /broadcast/{quadra_id}/placar/zerar` | | Zera pontos e sets |
| `PUT /broadcast/{quadra_id}/nomes` | `nome_a`, `nome_b` | Troca os nomes (até 14 caracteres, maiúsculas) |
| `POST /broadcast/{quadra_id}/volume/{up\|down}` | `?passo=10` (%) | Sobe/desce o volume |
| `PUT /broadcast/{quadra_id}/volume` | `{"nivel": 150}` (%) | Define o volume |
| `GET`/`PUT /broadcast/{quadra_id}/audio` | `{"corte_agudos": "medio", "reducao_ruido": "medio"}` (um ou os dois) | [Áudio](#áudio-corte-de-agudos-e-redução-de-ruído): vale ao vivo |

As rotas de placar, nomes e volume funcionam **com a transmissão fora do ar**
também: o valor fica gravado e vale quando ela for ligada com `reset: false`.

### Objeto de estado

É o que quase toda rota de `/broadcast` devolve:

```json
{
  "quadra_id": "loc1-quadra1",
  "no_ar": true,
  "nomes": { "a": "ANA E BIA", "b": "CRIS E DUDA" },
  "placar": {
    "a": { "pontos": 2, "sets": 1 },
    "b": { "pontos": 1, "sets": 0 }
  },
  "volume": 100,
  "audio": { "reducao_ruido": "medio", "corte_agudos": "medio" },
  "no_ar_ha_segundos": 312,
  "encerra_em_segundos": 6888,
  "reinicios": 0,
  "travamentos": 0,
  "transmitindo": true,
  "audio_da_camera": true
}
```

| Campo | Tipo | Descrição |
|---|---|---|
| `quadra_id` | string | A quadra |
| `no_ar` | bool | A transmissão foi pedida e está ativa |
| `nomes.a`, `nomes.b` | string | Nomes das duplas como aparecem no vídeo (padrão `DUPLA A` / `DUPLA B`) |
| `placar.{a,b}.pontos` | int | Pontos no set atual |
| `placar.{a,b}.sets` | int | Sets ganhos |
| `volume` | int | % do volume da câmera (100 = original, 0 = mudo) |
| `audio` | objeto | Ajustes de áudio desta quadra: `reducao_ruido` e `corte_agudos` (veja [Áudio](#áudio-corte-de-agudos-e-redução-de-ruído)) |
| `no_ar_ha_segundos` | int ou `null` | Há quanto tempo está no ar; `null` fora do ar |
| `encerra_em_segundos` | int ou `null` | Quanto falta para o prazo `LIVE_MAX_SECONDS`; `null` fora do ar ou sem limite |
| `reinicios` | int | Quantas vezes a API relançou o ffmpeg desta transmissão |
| `travamentos` | int | Quantas vezes o ffmpeg ficou vivo mas parado (sem quadros por 30 s) e foi relançado |
| `transmitindo` | bool | `true` só se o ffmpeg está vivo **e produzindo quadros agora** |
| `audio_da_camera` | bool ou `null` | `false` = câmera sem áudio, a live vai com silêncio; `null` = não sondado |

Fora do ar, o mesmo objeto vem com `no_ar: false`, `no_ar_ha_segundos: null`,
`encerra_em_segundos: null` e `transmitindo: false`; nomes, placar e volume
continuam com o último valor.

### Ligar e desligar

#### Ligar

`POST /broadcast/{quadra_id}/start`

**Corpo (JSON, opcional; todos os campos opcionais)**

| Campo | Tipo | Padrão | Descrição |
|---|---|---|---|
| `nome_a` | string | `DUPLA A` | Nome do lado A (até 14 caracteres; vira maiúscula) |
| `nome_b` | string | `DUPLA B` | Nome do lado B |
| `reset` | bool | `true` | `true` = jogo novo: placar 0x0 **e volume 100%**. `false` = mantém placar e volume (para religar o mesmo jogo) |
| `volume` | int | 100 (ou o último, se `reset: false`) | % do volume da câmera, entre 0 e `BROADCAST_VOLUME_MAX` (400) |

Nome: só ficam letras, números, espaço e `. & ' -`; o resto é descartado.

**Exemplo 1: jogo novo, com nomes**

```bash
curl -X POST http://SERVIDOR:8000/broadcast/loc1-quadra1/start \
  -H "Authorization: Bearer SEU_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"nome_a": "Ana e Bia", "nome_b": "Cris e Duda"}'
```

**Exemplo 2: sem corpo** (placar 0x0, nomes `DUPLA A`/`DUPLA B`, volume 100%)

```bash
curl -X POST http://SERVIDOR:8000/broadcast/loc1-quadra1/start \
  -H "Authorization: Bearer SEU_TOKEN"
```

**Exemplo 3: religar o mesmo jogo** (a API caiu, ou você encerrou sem querer)

```bash
curl -X POST http://SERVIDOR:8000/broadcast/loc1-quadra1/start \
  -H "Authorization: Bearer SEU_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reset": false}'
```

**Resposta `200`** (exemplo 1; o `status` vem na frente do objeto de estado)

```json
{
  "status": "iniciada",
  "quadra_id": "loc1-quadra1",
  "no_ar": true,
  "nomes": { "a": "ANA E BIA", "b": "CRIS E DUDA" },
  "placar": {
    "a": { "pontos": 0, "sets": 0 },
    "b": { "pontos": 0, "sets": 0 }
  },
  "volume": 100,
  "no_ar_ha_segundos": 0,
  "encerra_em_segundos": 7200,
  "reinicios": 0,
  "travamentos": 0,
  "transmitindo": false,
  "audio_da_camera": true
}
```

`transmitindo` pode vir `false` nos primeiros segundos, até o ffmpeg produzir o
primeiro quadro; consulte `GET /broadcast/{quadra_id}` para confirmar.

É idempotente: com a transmissão já no ar, devolve `"status": "ja_no_ar"` com o
estado atual, **sem** mexer em placar, nomes ou volume (o corpo é ignorado).

#### Desligar

`POST /broadcast/{quadra_id}/stop` ou `DELETE /broadcast/{quadra_id}`

Sem corpo.

```bash
curl -X POST http://SERVIDOR:8000/broadcast/loc1-quadra1/stop \
  -H "Authorization: Bearer SEU_TOKEN"
```

**Resposta `200`** (placar e nomes ficam guardados)

```json
{
  "status": "encerrada",
  "quadra_id": "loc1-quadra1",
  "no_ar": false,
  "nomes": { "a": "ANA E BIA", "b": "CRIS E DUDA" },
  "placar": {
    "a": { "pontos": 11, "sets": 2 },
    "b": { "pontos": 7, "sets": 1 }
  },
  "volume": 100,
  "no_ar_ha_segundos": null,
  "encerra_em_segundos": null,
  "reinicios": 0,
  "travamentos": 0,
  "transmitindo": false,
  "audio_da_camera": null
}
```

`status` é `encerrada` ou `nao_estava_no_ar`.

### Estado

```bash
# uma quadra
curl http://SERVIDOR:8000/broadcast/loc1-quadra1 -H "Authorization: Bearer SEU_TOKEN"
```

Resposta: o [objeto de estado](#objeto-de-estado).

```bash
# todas as quadras registradas, e quais estão no ar
curl http://SERVIDOR:8000/broadcast -H "Authorization: Bearer SEU_TOKEN"
```

```json
{
  "no_ar": ["loc1-quadra1"],
  "limite": 2,
  "transmissoes": {
    "loc1-quadra1": { "quadra_id": "loc1-quadra1", "no_ar": true, "...": "objeto de estado" },
    "loc1-quadra2": { "quadra_id": "loc1-quadra2", "no_ar": false, "...": "objeto de estado" }
  }
}
```

`no_ar` lista os `quadra_id` ativos; `limite` é `BROADCAST_MAX_CONCURRENT`.
`transmissoes` traz as quadras que a API já registrou (não só as no ar); para saber
quem está transmitindo, use o campo `no_ar`.

### Placar

#### Ponto

`POST /broadcast/{quadra_id}/ponto/{lado}?delta=1`

| Parte | Valor |
|---|---|
| Path `lado` | `a` ou `b` |
| Query `delta` | inteiro, padrão `1`; `-1` corrige um ponto dado por engano |
| Corpo | nenhum |

```bash
# +1 ponto para a dupla A
curl -X POST http://SERVIDOR:8000/broadcast/loc1-quadra1/ponto/a \
  -H "Authorization: Bearer SEU_TOKEN"

# desfaz um ponto da dupla A
curl -X POST "http://SERVIDOR:8000/broadcast/loc1-quadra1/ponto/a?delta=-1" \
  -H "Authorization: Bearer SEU_TOKEN"
```

**Resposta `200`**: o [objeto de estado](#objeto-de-estado) já com o placar novo
(aqui, depois de um `ponto/a` com o jogo em 1x1):

```json
{
  "quadra_id": "loc1-quadra1",
  "no_ar": true,
  "nomes": { "a": "ANA E BIA", "b": "CRIS E DUDA" },
  "placar": {
    "a": { "pontos": 2, "sets": 0 },
    "b": { "pontos": 1, "sets": 0 }
  },
  "volume": 100,
  "no_ar_ha_segundos": 120,
  "encerra_em_segundos": 7080,
  "reinicios": 0,
  "travamentos": 0,
  "transmitindo": true,
  "audio_da_camera": true
}
```

#### Set

`POST /broadcast/{quadra_id}/set/{lado}?delta=1`

Soma 1 set ao lado **e zera os pontos dos dois lados**. Com `delta=-1`, só tira
1 set (não mexe nos pontos).

```bash
# a dupla A ganhou o set: +1 set e pontos 0x0
curl -X POST http://SERVIDOR:8000/broadcast/loc1-quadra1/set/a \
  -H "Authorization: Bearer SEU_TOKEN"

# fechou o set pro lado errado: tira 1 set (pontos ficam como estão)
curl -X POST "http://SERVIDOR:8000/broadcast/loc1-quadra1/set/a?delta=-1" \
  -H "Authorization: Bearer SEU_TOKEN"
```

**Resposta `200`**: o objeto de estado. Antes, `a: 6 pontos / 0 sets`,
`b: 4 pontos / 0 sets`; depois do `set/a`:

```json
{
  "...": "...",
  "placar": {
    "a": { "pontos": 0, "sets": 1 },
    "b": { "pontos": 0, "sets": 0 }
  }
}
```

#### Correção manual

`PUT /broadcast/{quadra_id}/placar`

Grava **exatamente** os valores enviados. Campos omitidos (ou `null`) não mudam.
Valores negativos viram 0.

**Corpo (JSON)**

| Campo | Tipo | Descrição |
|---|---|---|
| `pontos_a` | int | Pontos do lado A |
| `pontos_b` | int | Pontos do lado B |
| `sets_a` | int | Sets do lado A |
| `sets_b` | int | Sets do lado B |

```bash
# o placar real é 40x15, com a dupla A com 1 set
curl -X PUT http://SERVIDOR:8000/broadcast/loc1-quadra1/placar \
  -H "Authorization: Bearer SEU_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"pontos_a": 40, "pontos_b": 15, "sets_a": 1}'
```

**Resposta `200`**: o objeto de estado, com `placar.a = {pontos: 40, sets: 1}` e
`placar.b = {pontos: 15, sets: <o que já estava>}`.

Um corpo vazio `{}` é aceito e não muda nada.

#### Zerar

`POST /broadcast/{quadra_id}/placar/zerar`: zera pontos e sets dos dois lados
(nomes e volume ficam). Sem corpo.

```bash
curl -X POST http://SERVIDOR:8000/broadcast/loc1-quadra1/placar/zerar \
  -H "Authorization: Bearer SEU_TOKEN"
```

**Resposta `200`**: o objeto de estado com `placar` todo em `0`.

#### Nomes

`PUT /broadcast/{quadra_id}/nomes`

**Corpo (JSON)**: `nome_a` e/ou `nome_b`; o que não vier não muda.

| Campo | Tipo | Descrição |
|---|---|---|
| `nome_a` | string | Nome do lado A (até 14 caracteres; vira maiúscula) |
| `nome_b` | string | Nome do lado B |

Uma string vazia (ou só com caracteres descartados) volta ao nome padrão
(`DUPLA A` / `DUPLA B`).

```bash
curl -X PUT http://SERVIDOR:8000/broadcast/loc1-quadra1/nomes \
  -H "Authorization: Bearer SEU_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"nome_a": "Ana e Bia"}'
```

**Resposta `200`**: o objeto de estado com `nomes.a = "ANA E BIA"`.

O placar aparece no vídeo em menos de 1 s depois da chamada, e pode ser
ajustado antes de ligar.

### Volume

`nivel` é % do volume da câmera (100 = original, 0 = mudo, máximo
`BROADCAST_VOLUME_MAX`=400; o **painel web** se limita a 100% por conta própria, a API não); acima/abaixo dos limites o valor é ajustado, sem
erro. Vale na hora, sem derrubar a live.

#### Subir/descer

`POST /broadcast/{quadra_id}/volume/{direcao}?passo=10`

| Parte | Valor |
|---|---|
| Path `direcao` | `up` ou `down` (outro valor: `422`) |
| Query `passo` | inteiro, em pontos percentuais; padrão `BROADCAST_VOLUME_STEP` (10) |
| Corpo | nenhum |

```bash
# +10 pontos percentuais (de 100 para 110)
curl -X POST http://SERVIDOR:8000/broadcast/loc1-quadra1/volume/up \
  -H "Authorization: Bearer SEU_TOKEN"

# -20 pontos percentuais
curl -X POST "http://SERVIDOR:8000/broadcast/loc1-quadra1/volume/down?passo=20" \
  -H "Authorization: Bearer SEU_TOKEN"
```

**Resposta `200`**: o objeto de estado com o `volume` novo (aqui `110`).

#### Definir

`PUT /broadcast/{quadra_id}/volume`

**Corpo (JSON)**

| Campo | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `nivel` | int | sim | Volume em % do da câmera |

```bash
curl -X PUT http://SERVIDOR:8000/broadcast/loc1-quadra1/volume \
  -H "Authorization: Bearer SEU_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"nivel": 150}'
```

**Resposta `200`**: o objeto de estado com `"volume": 150`.

Há um limitador de pico, então subir demais satura com menos distorção, mas não
perfeito: o microfone da câmera já chega perto de 0 dBFS em jogo.

### Erros de `/broadcast`

| Código | Quando | Exemplo de corpo |
|---|---|---|
| `401` / `403` / `503` | Token ausente/errado; cookie sem `X-Painel`; nenhum token configurado | `{"detail": "Token inválido."}` |
| `404` | `quadra_id` não está em `config/cameras.json` | `{"detail": "quadra_id 'loc9-x' não está em config/cameras.json."}` |
| `409` | Limite de 2 transmissões | `{"detail": "limite de 2 transmissões simultâneas atingido."}` |
| `409` | A quadra está em live direta (`start`) | `{"detail": "'loc1-quadra1' está em live direta; encerre-a antes (POST /live/loc1-quadra1/stop)."}` |
| `409` | Outra quadra com a **mesma stream key** já está no ar (vale para `/live` também) | `{"detail": "A stream key de 'loc1-quadra1' é a mesma de 'loc1-quadra2', que já está no ar; encerre 'loc1-quadra2' antes."}` |
| `422` | `lado` diferente de `a`/`b` | `{"detail": "lado deve ser 'a' ou 'b'."}` |
| `422` | `direcao` diferente de `up`/`down` | `{"detail": "direcao deve ser 'up' ou 'down'."}` |
| `422` | A quadra sem `youtube_stream_key` (`start`) | `{"detail": "a quadra não tem youtube_stream_key em cameras.json."}` |
| `422` | Corpo inválido (ex.: `PUT .../volume` sem `nivel`, ou `nivel` não numérico) | lista de validação, ver [Formato dos erros](#formato-dos-erros) |
| `502` | A câmera não respondeu ao checar a live direta (`start`) | `{"detail": "<motivo devolvido pela câmera>"}` |
| `503` | O ffmpeg não estava lendo comandos (reiniciando); tente de novo | `{"detail": "o ffmpeg não está lendo comandos agora (reiniciando?); tente de novo."}` |

Log do ffmpeg de cada quadra: `logs/broadcast_{quadra_id}.log`.

**`no_ar` x `transmitindo`.** `no_ar` diz que a transmissão foi pedida e está
ativa; `transmitindo` só é `true` se o ffmpeg está vivo **e produzindo quadros agora**.
`no_ar: true` com `transmitindo: false` é "tentando": câmera ou YouTube fora, ffmpeg
reiniciando (veja `reinicios`). `travamentos` conta quantas vezes o ffmpeg ficou vivo
mas parado (sem quadros por 30 s) e foi morto e relançado pelo watchdog.

**Áudio.** A cada início (e a cada relançamento) a API sonda a câmera. Se ela
estiver **sem trilha de áudio**, a transmissão sobe com uma trilha de silêncio em
vez de falhar, e a resposta traz `"audio_da_camera": false` (com `true` a live usa
o som da câmera; `null` = ainda não sondado ou transmissão readotada após reinício
da API). O volume só tem efeito quando há áudio da câmera. Dica: um volume 0 deixa
a live muda; `start` com `reset=true` (padrão) volta o volume a 100%.

### Áudio: corte de agudos e redução de ruído

O áudio da câmera passa, antes do volume, por uma cadeia fixa de filtros que tem **dois ajustes por quadra**,
ligados por padrão no nível **médio**:

| Ajuste | Níveis | O que faz |
|---|---|---|
| `corte_agudos` | `off`, `suave` (5 kHz), `medio` (4 kHz), `forte` (4 kHz íngreme), `maximo` (3,5 kHz íngreme) | Tira o chiado agudo. Medido em áudio real: reduz ~3, ~6,5, ~11 e ~16 dB do agudo (4–8 kHz) nos quatro níveis, e **a voz e o som até 3,4 kHz não mudam**. O preço é o som ficar mais abafado |
| `reducao_ruido` | `off`, `leve`, `medio`, `forte` | Corta o zumbido grave (< 100 Hz) e rebaixa o ruído de fundo (`afftdn`). É discreta de propósito: não tira chiado misturado com o som, e o nível `forte` pode deixar um som metálico |

O padrão de cada quadra vem de `AUDIO_CORTE_AGUDOS` e `AUDIO_DENOISE` no `.env` (os dois `medio`); depois que alguém
ajusta uma quadra, o valor fica **gravado** e sobrevive a reinício da API. Custo medido no comando real: ~+1% de um
núcleo por transmissão. Não afeta a **live direta** (o vídeo sai da câmera, sem passar pelo servidor) nem os replays.
Sem áudio da câmera nada é aplicado, e se o ffmpeg da máquina não tiver o filtro `afftdn` a live sobe sem ele (o corte
de agudos continua) e o log avisa.

#### Ver e trocar: `GET` e `PUT /broadcast/{quadra_id}/audio`

```bash
curl -s http://SERVIDOR:8000/broadcast/loc1-quadra1/audio -H "Authorization: Bearer SEU_TOKEN"
```

```json
{
  "quadra_id": "loc1-quadra1",
  "reducao_ruido": "medio",
  "corte_agudos": "medio",
  "padrao": { "reducao_ruido": "medio", "corte_agudos": "medio" },
  "niveis": {
    "reducao_ruido": ["off", "leve", "medio", "forte"],
    "corte_agudos": ["off", "suave", "medio", "forte", "maximo"]
  },
  "no_ar": true,
  "aplicado_ao_vivo": true
}
```

| Campo | Significado |
|---|---|
| `reducao_ruido`, `corte_agudos` | Os ajustes em vigor desta quadra (o gravado, ou o padrão do servidor) |
| `padrao` | O padrão do servidor (`AUDIO_DENOISE` e `AUDIO_CORTE_AGUDOS`) |
| `niveis` | Os valores aceitos |
| `no_ar` | A transmissão com placar desta quadra está no ar |
| `aplicado_ao_vivo` | `null` com a quadra fora do ar (vale ao iniciar). Com ela no ar: `true` = o ffmpeg **confirmou** o ajuste; `false` = não confirmou (o ajuste fica gravado e vale ao relançar) |

`PUT` troca o que vier no corpo (pelo menos um campo; o outro não muda; `null` é recusado). **Com a transmissão no ar
vale na hora, sem relançar nem cortar a live**: a API manda ao ffmpeg só o que mudou e espera a confirmação dele
(cada comando leva ~0,1 a 0,5 s, então a resposta pode demorar um ou dois segundos). Fora do ar só grava.

```bash
# corte de agudos médio (4 kHz): o ponto de partida recomendado
curl -s -X PUT http://SERVIDOR:8000/broadcast/loc1-quadra1/audio \
  -H "Authorization: Bearer SEU_TOKEN" -H "Content-Type: application/json" \
  -d '{"corte_agudos": "medio"}'

# mais firme, e sem a redução de ruído
curl -s -X PUT http://SERVIDOR:8000/broadcast/loc1-quadra1/audio \
  -H "Authorization: Bearer SEU_TOKEN" -H "Content-Type: application/json" \
  -d '{"corte_agudos": "forte", "reducao_ruido": "off"}'
```

Resposta `200`: o mesmo objeto do `GET` com `status` na frente (`"atualizada"` ou `"sem_mudanca"`). Repetir o `PUT`
com o mesmo valor quando `aplicado_ao_vivo` veio `false` **tenta aplicar de novo**.

| Código | Quando | `detail` de exemplo |
|---|---|---|
| `401` / `403` / `503` | Token ausente/errado; cookie sem `X-Painel`; nenhum token | ver [Autenticação](#autenticação) |
| `404` | `quadra_id` não está em `config/cameras.json` | `quadra_id 'x' não está em .../cameras.json.` |
| `422` | Nível desconhecido | `corte_agudos deve ser um de: off, suave, medio, forte, maximo.` |
| `422` | Corpo sem nenhum campo | `informe reducao_ruido e/ou corte_agudos.` |
| `422` | Campo `null` | `corte_agudos não pode ser nulo (omita o campo para não mudar).` |
| `422` | Tipo errado (`detail` é **lista**) | ver [Formato dos erros](#formato-dos-erros) |

O campo `audio` (`{"reducao_ruido", "corte_agudos"}`) também vem no [objeto de estado](#objeto-de-estado) de toda
rota de `/broadcast` e no `GET /status`. Uma transmissão que **subiu antes desta versão** não tem os filtros que aceitam a
troca ao vivo: a API detecta isso ao reiniciar (`aplicado_ao_vivo: false`) e o ajuste vale quando ela for relançada.
A caixa do placar e o áudio são decididos no ffmpeg de cada quadra, então o ajuste de uma quadra não afeta as outras.

---

## Estado geral (`GET /status`)

Uma chamada devolve as quadras com os dois modos, sem consultar câmera nenhuma na
hora: o estado da live direta vem de uma verificação periódica
(`LIVE_MONITOR_INTERVAL_SECONDS`, padrão 15 s) que roda em segundo plano. É a
rota usada pelo painel.

**Requisição**

```bash
curl http://SERVIDOR:8000/status -H "Authorization: Bearer SEU_TOKEN"
```

**Resposta `200`**

```json
{
  "quadras": [
    {
      "quadra_id": "loc1-quadra1",
      "nome": "Quadra 1",
      "esporte": "padel",
      "tem_stream_key": true,
      "chave_compartilhada_com": ["loc1-quadra2"],
      "chave_em_uso_por": null,
      "modo": "placar",
      "direta": { "no_ar": false, "erro": null, "verificado_ha_segundos": 7 },
      "placar": { "quadra_id": "loc1-quadra1", "no_ar": true, "...": "o mesmo objeto de GET /broadcast/{quadra_id}" }
    },
    {
      "quadra_id": "loc1-quadra2",
      "nome": "Quadra 2",
      "esporte": "padel",
      "tem_stream_key": true,
      "chave_compartilhada_com": ["loc1-quadra1"],
      "chave_em_uso_por": "loc1-quadra1",
      "modo": "nenhum",
      "direta": { "no_ar": false, "erro": null, "verificado_ha_segundos": 7 },
      "placar": { "quadra_id": "loc1-quadra2", "no_ar": false, "...": "objeto de estado" }
    }
  ],
  "transmissoes_com_placar": { "no_ar": 1, "limite": 2 },
  "verificacao_na_subida": {
    "direta": [],
    "placar": ["loc1-quadra1"],
    "conflitos": []
  }
}
```

| Campo (por quadra) | Descrição |
|---|---|
| `nome`, `esporte` | Vêm do `cameras.json` |
| `tem_stream_key` | A quadra tem `youtube_stream_key` (sem ela, `start` dá `422`) |
| `chave_compartilhada_com` | Outras quadras com a **mesma** `youtube_stream_key` (duplicar é permitido: um canal só) |
| `chave_em_uso_por` | Qual quadra de mesma key está no ar agora (`null` = nenhuma); enquanto uma estiver no ar, em **qualquer** modo, ligar outra responde `409` |
| `modo` | `placar`, `direta`, `conflito` (os dois ligados), `nenhum` ou `desconhecido` (a câmera não respondeu e não há transmissão com placar) |
| `direta.no_ar` | `true`/`false`, ou `null` se a câmera não respondeu (veja `direta.erro`) |
| `direta.erro` | Mensagem do último erro de consulta à câmera; `null` se deu certo. Antes da 1ª verificação: `"ainda não verificado"` |
| `direta.verificado_ha_segundos` | Idade da última verificação |
| `placar` | O [objeto de estado](#objeto-de-estado) da transmissão com placar |

### Verificação na subida

Quando a API sobe ela reconcilia o que já estava no ar, e escreve o resultado no log
(`[startup]`) e em `verificacao_na_subida`:

- **Transmissões com placar:** o ffmpeg que segue vivo é readotado (pelo pid gravado);
  o que caiu é relançado; um ffmpeg **sem registro** (arquivo de estado perdido) é
  readotado com nomes e placar lidos do overlay; transmissão de quadra que saiu do
  `cameras.json` é encerrada; prazo vencido durante a queda encerra na hora.
- **Live direta:** a API pergunta a cada câmera (ISAPI) se está empurrando. Uma live
  ligada fora da API, por exemplo na interface web da câmera, **ganha o prazo**
  `LIVE_MAX_SECONDS` (senão nunca expiraria); prazo gravado de live que já não está no
  ar é esquecido.
- **Conflito:** transmissão com placar **e** live direta ao mesmo tempo na mesma
  quadra (mesma stream key) é apontada no log e no `modo: "conflito"`. A API **não
  desliga nada sozinha**: quem decide qual encerrar é o operador.

O que isto vê é o lado do servidor e da câmera. Se o YouTube de fato recebe e publica a
imagem só se confirma no YouTube Studio (a API do YouTube exigiria OAuth, que não está
configurado).

---

## Painel web

`http://SERVIDOR:8000/painel`: página para operar a transmissão pelo celular ou
computador (layout responsivo, tema claro/escuro com alternância manual, identidade grená).
O topo mostra contadores (quantas quadras ao vivo, transmissões com placar `n/limite`) e um
indicador de conexão com a API. Cada quadra é um cartão com **duas abas**:

**Aba Transmissão**, conforme o modo:

- **Fora do ar:** nomes das duplas e botões *Iniciar transmissão com placar* e *Iniciar
  live direta*.
- **Com placar:** um placar que espelha a caixa grená da transmissão; por dupla, *+1 ponto*,
  *− ponto*, *Fechar set* (pede confirmação: zera os pontos) e *− set*; volume por controle
  deslizante e −/+, **limitado a 100%** (o painel nunca envia mais que isso e o + para no teto; se a API
  tiver posto um valor maior, o painel mostra o valor real e o − volta direto a 100%); *Nomes e correções* (editar nomes, corrigir pontos e sets, zerar placar);
  *Encerrar*. Mostra o tempo no ar, o tempo que falta até o prazo (barra que muda de cor perto do
  fim), reinícios e se a câmera tem áudio.
- **Direta:** *Encerrar*.
- Avisos: quadra sem stream key (com atalho para cadastrá-la), limite de 2 transmissões,
  ffmpeg tentando conectar, câmera sem resposta, key compartilhada com outra quadra no ar,
  conflito entre os dois modos.

**Aba Áudio** (usa [`/broadcast/{id}/audio`](#áudio-corte-de-agudos-e-redução-de-ruído)): dois controles de escolha
única, *Corte de agudos* (Off, Suave, Médio, Forte, Máximo) e *Redução de ruído* (Off, Leve, Média, Forte), cada um com a
descrição do nível escolhido e a etiqueta "padrão do servidor" ou "personalizado". Clicar aplica **na hora**, sem
relançar a transmissão. Uma nota diz o estado: *Aplicado ao vivo agora* (o ffmpeg confirmou), *Quadra fora do ar* (vale ao
iniciar) ou *não confirmou* (guardado, vale ao relançar; botão *Tentar de novo*). *Voltar ao padrão do servidor* restaura
os dois. Se outra pessoa mexer, a aba se atualiza sozinha.

**Aba Destino** (usa as rotas [`/quadras/{id}/youtube`](#configurar-o-destino-da-transmissão-stream-key-e-rtmp)):

- **Stream key:** mostra se está cadastrada e os 4 últimos caracteres; *Ver atual* revela a key
  inteira (ela some sozinha em 30 s, e cada leitura fica no log do servidor); o campo e *Salvar key*
  trocam a key, com confirmação e validação do formato antes de enviar.
- **Servidor RTMP:** mostra a URL em uso, se é a padrão do servidor ou própria da quadra, e os hosts
  aceitos; *Salvar URL* troca e *Usar a padrão* volta à global (com confirmação).
- **Quadra transmitindo:** os campos ficam travados com um aviso (a API recusaria com `409`); ler a
  key continua permitido. Quem trocou fica registrado no log do servidor como `painel:<usuário>`.

A tela atualiza a cada 2 s a partir de `GET /status` (e pausa quando a aba do navegador fica em segundo
plano). As ações pedem confirmação em janela própria, não no `confirm()` do navegador.

**Arquivos e segurança.** `api/static/painel.{html,css,js}`, sem dependências nem fontes externas. A
resposta do painel leva `Content-Security-Policy: default-src 'self'`, então **não use** script ou estilo
inline, `innerHTML` nem imagens `data:`: todo texto vindo da API entra como texto, e os ícones são SVG
montados pelo DOM. `test/test_panel_static.py` trava essas regras. Ao mudar CSS/JS, aumente o `?v=` no
`painel.html` para o navegador não ficar com a versão antiga em cache.

**Login.** Usa `ADMIN_USERNAME` e `ADMIN_PASSWORD` (os mesmos do `DELETE`); sem eles o
login responde `503`. A sessão é um cookie `repet_painel` assinado (HMAC), `HttpOnly` e
`SameSite=Strict`, válido por `PANEL_SESSION_HOURS` (12 h). Reiniciar a API não desloga
ninguém; trocar a senha (ou `PANEL_SECRET`) desloga todos.

Essas três rotas não aparecem no Swagger (`/docs`).

### Login

`POST /painel/login`

**Corpo (JSON)**

| Campo | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `usuario` | string | sim | `ADMIN_USERNAME` |
| `senha` | string | sim | `ADMIN_PASSWORD` |

```bash
curl -i -c cookies.txt -X POST http://SERVIDOR:8000/painel/login \
  -H "Content-Type: application/json" \
  -d '{"usuario": "SEU_USUARIO", "senha": "SUA_SENHA"}'
```

**Resposta `200`**: o cookie vem no header `Set-Cookie`, o corpo só confirma o usuário.

```http
HTTP/1.1 200 OK
set-cookie: repet_painel=<token assinado>; HttpOnly; Max-Age=43200; Path=/; SameSite=strict
content-type: application/json

{"usuario": "SEU_USUARIO"}
```

| Código | Quando | Corpo |
|---|---|---|
| `401` | Usuário ou senha errados | `{"detail": "Usuário ou senha inválidos."}` |
| `429` | 5 erros do mesmo IP em 1 min | `{"detail": "Muitas tentativas; aguarde um minuto."}` |
| `503` | `ADMIN_*` não configurados no servidor | `{"detail": "Defina ADMIN_USERNAME e ADMIN_PASSWORD no servidor."}` |

### Sessão (`me`) e saída

```bash
curl -b cookies.txt http://SERVIDOR:8000/painel/me
# {"usuario": "SEU_USUARIO"}      (401 {"detail": "Sem sessão."} se não houver cookie válido)

curl -b cookies.txt -c cookies.txt -X POST http://SERVIDOR:8000/painel/logout
# {"status": "saiu"}
```

### Usando a sessão nas outras rotas

Com cookie, as rotas que mudam algo exigem também o header `X-Painel: 1` (defesa
contra CSRF; o painel o envia sozinho), senão `403`. Os `GET` não precisam dele. Quem
usa o header `Authorization: Bearer` (ou o legado `X-Live-Key`) não precisa dele.

```bash
curl -b cookies.txt -X POST http://SERVIDOR:8000/broadcast/loc1-quadra1/ponto/a \
  -H "X-Painel: 1"

curl -b cookies.txt http://SERVIDOR:8000/status
```

**Segurança.** O tráfego é HTTP puro: na rede interna a senha segue em claro; para
acesso de fora, ponha um proxy HTTPS na frente.

---

## Consultar replays

### Metadados

`GET /api/replays/{replay_id}`

**Requisição**

```bash
curl http://SERVIDOR:8000/api/replays/loc1-quadra1_20261027100537
```

**Resposta `200`**

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

| Campo | Tipo | Descrição |
|---|---|---|
| `id` | string | O `replay_id` |
| `quadra_id` | string | A quadra |
| `criado_em` | string (ISO 8601) | Instante do clique, no horário do servidor (sem fuso) |
| `duracao_segundos` | número | Duração do arquivo gerado |
| `tamanho_bytes` | int | Tamanho do arquivo bruto |
| `media_url` | string | Caminho do vídeo (`GET .../media`) |
| `lara_status` | string | `pendente`, `enviado` ou `falha` |
| `lara_enviado_em` | string ou `null` | Quando subiu ao Lara; `null` enquanto `pendente` ou `falha` |

Um replay recém-gravado, ainda não enviado, vem com `"lara_status": "pendente"` e
`"lara_enviado_em": null`.

**Erro `404`**

```json
{ "detail": "replay 'loc1-quadra1_20990101000000' não encontrado." }
```

### Vídeo

`GET /api/replays/{replay_id}/media`

Serve o vídeo processado (com música) se já existir; senão, o bruto. Responde
`video/mp4`, aceita `Range` (`206`) e ETag, com `Cache-Control: no-cache`.

```bash
# baixar o arquivo
curl -o replay.mp4 http://SERVIDOR:8000/api/replays/loc1-quadra1_20261027100537/media

# pedir só o começo (útil para player/preview)
curl -H "Range: bytes=0-1023" -o trecho.bin \
  http://SERVIDOR:8000/api/replays/loc1-quadra1_20261027100537/media
```

Em HTML: `<video controls src="/api/replays/loc1-quadra1_20261027100537/media"></video>`.

| Código | Quando | Corpo |
|---|---|---|
| `404` | O replay não existe | `{"detail": "replay '...' não encontrado."}` |
| `404` | A retenção local já removeu o arquivo | `{"detail": "arquivo do replay '...' não está mais em disco (retenção local já removeu?)."}` |

### Listagem por quadra

`GET /api/quadras/{quadra_id}/replays?page=1&page_size=20`

Do mais recente ao mais antigo.

| Query | Tipo | Padrão | Descrição |
|---|---|---|---|
| `page` | int | `1` | Página; valores menores que 1 viram 1 |
| `page_size` | int | `20` | Itens por página; limitado entre 1 e 100 (fora disso, a API ajusta, sem erro) |

**Requisição**

```bash
curl "http://SERVIDOR:8000/api/quadras/loc1-quadra1/replays?page=1&page_size=2"
```

**Resposta `200`**

```json
{
  "quadra_id": "loc1-quadra1",
  "page": 1,
  "page_size": 2,
  "total": 42,
  "items": [
    {
      "id": "loc1-quadra1_20261027100537",
      "quadra_id": "loc1-quadra1",
      "criado_em": "2026-10-27T10:05:37",
      "duracao_segundos": 35.4,
      "tamanho_bytes": 21043210,
      "media_url": "/api/replays/loc1-quadra1_20261027100537/media",
      "lara_status": "enviado",
      "lara_enviado_em": "2026-10-27T10:07:02"
    },
    {
      "id": "loc1-quadra1_20261027094412",
      "quadra_id": "loc1-quadra1",
      "criado_em": "2026-10-27T09:44:12",
      "duracao_segundos": 35.2,
      "tamanho_bytes": 20877340,
      "media_url": "/api/replays/loc1-quadra1_20261027094412/media",
      "lara_status": "pendente",
      "lara_enviado_em": null
    }
  ]
}
```

Cada item tem o mesmo formato de [Metadados](#metadados). `total` é a contagem de
todos os replays da quadra (não só da página); para percorrer tudo, aumente `page`
até `page * page_size >= total`. Uma página além do fim devolve `"items": []`.

**Erro `404`**: `{"detail": "quadra 'loc9-x' não encontrada."}`. A quadra precisa
estar no banco, e a API a copia do `cameras.json` **só ao subir**: depois de
adicionar uma quadra nova ao arquivo, reinicie a API (aqui o `404` aparece mesmo
com a quadra no `cameras.json`).

### Clipe bruto e página de teste

- `GET /clips/{arquivo}.mp4`: o clipe bruto, direto do disco (é o
  `clip_url` devolvido pelo `POST /replay`).

  ```bash
  curl -o clipe.mp4 http://SERVIDOR:8000/clips/loc1-quadra1_20261027100537.mp4
  ```

- `GET /quadra/{quadra_id}`: página HTML simples com os replays da quadra,
  para teste interno (`404` com `{"detail": ...}` se a quadra não existe no
  `cameras.json`).

---

## Apagar um replay

`DELETE /api/replays/{replay_id}`: HTTP Basic com `ADMIN_USERNAME` e
`ADMIN_PASSWORD`. Remove o registro e os arquivos (bruto e processado).
Sem essas variáveis configuradas, responde `401` sempre.

**Requisição**

```bash
curl -i -X DELETE -u SEU_USUARIO:SUA_SENHA \
  http://SERVIDOR:8000/api/replays/loc1-quadra1_20261027100537
```

O header enviado é `Authorization: Basic base64(usuario:senha)`.

**Resposta**

```http
HTTP/1.1 204 No Content
```

| Código | Quando | Corpo |
|---|---|---|
| `204` | Apagado | nenhum |
| `401` | Credencial inválida, ou `ADMIN_*` não configurados (header `WWW-Authenticate: Basic`) | `{"detail": "Credencial inválida."}`, ou `{"detail": "Autenticação de administrador não configurada (ADMIN_USERNAME/ADMIN_PASSWORD)."}` |
| `404` | O replay não existe | `{"detail": "replay '...' não encontrado."}` |

A operação não pode ser desfeita.

---

## Healthcheck

`GET /health`, sem autenticação.

```bash
curl http://SERVIDOR:8000/health
```

```json
{ "status": "ok" }
```

---

## Exemplo de ponta a ponta: um jogo

Fluxo típico de operação de uma quadra com placar, usando só `curl`:

```bash
API=http://SERVIDOR:8000
KEY="Authorization: Bearer SEU_TOKEN"
Q=loc1-quadra1
JSON="Content-Type: application/json"

# 1. antes de ligar: confira se a quadra está livre
curl -s $API/status -H "$KEY"

# 2. liga a transmissão com os nomes das duplas
curl -s -X POST $API/broadcast/$Q/start -H "$KEY" -H "$JSON" \
  -d '{"nome_a": "Ana e Bia", "nome_b": "Cris e Duda"}'

# 3. durante o jogo
curl -s -X POST $API/broadcast/$Q/ponto/a -H "$KEY"
curl -s -X POST $API/broadcast/$Q/ponto/b -H "$KEY"
curl -s -X POST "$API/broadcast/$Q/ponto/b?delta=-1" -H "$KEY"   # errou: desfaz
curl -s -X POST $API/broadcast/$Q/set/a -H "$KEY"                # A fechou o set
curl -s -X PUT  $API/broadcast/$Q/volume -H "$KEY" -H "$JSON" -d '{"nivel": 130}'

# 4. alguém apertou o botão de replay (o firmware faz esta chamada; sem autenticação)
curl -s -X POST $API/replay/$Q --max-time 15

# 5. fim de jogo
curl -s -X POST $API/broadcast/$Q/stop -H "$KEY"
```

Em Python, o mesmo com `requests`:

```python
import requests

API = "http://SERVIDOR:8000"
HEADERS = {"Authorization": "Bearer SEU_TOKEN"}
QUADRA = "loc1-quadra1"

r = requests.post(
    f"{API}/broadcast/{QUADRA}/start",
    headers=HEADERS,
    json={"nome_a": "Ana e Bia", "nome_b": "Cris e Duda"},
    timeout=30,  # o start sonda a câmera (alguns segundos)
)
r.raise_for_status()  # em erro, r.json()["detail"] tem a mensagem

r = requests.post(f"{API}/broadcast/{QUADRA}/ponto/a", headers=HEADERS, timeout=10)
print(r.json()["placar"])  # {"a": {"pontos": 1, "sets": 0}, "b": {...}}

r = requests.post(f"{API}/replay/{QUADRA}", timeout=15)
print(r.json()["clip_url"])
```
