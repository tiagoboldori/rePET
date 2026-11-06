# Prompt: integrar o painel de controle das transmissões do rePET

> Como usar: copie **tudo abaixo da linha** e cole como a primeira mensagem de um agente Claude
> que tenha acesso ao código da aplicação web que vai receber o painel. Antes de colar, troque
> `SERVIDOR` pelo host/IP real da API do rePET. **Não cole o token neste texto**: entregue-o ao
> agente por variável de ambiente ou pelo gerenciador de segredos da aplicação.
>
> O contrato abaixo foi validado contra a API real (rotas, corpos, códigos e mensagens). A
> referência completa e atualizada fica em `docs/API.md` do repositório do rePET.

---

# Tarefa

Você vai integrar à nossa aplicação web um **painel de controle de transmissões ao vivo de quadras
esportivas**, falando com a API do **rePET** (um serviço nosso, já em produção). O painel deve
permitir, por quadra: ligar e desligar a transmissão, marcar o placar (pontos e sets), editar os
nomes das duplas, ajustar o volume e acompanhar o estado em tempo quase real.

Antes de escrever código, **explore o projeto** em que você está: descubra a linguagem, o framework,
como são feitos controllers/serviços, autenticação de usuários, front-end, configuração por
variáveis de ambiente e testes. Siga as convenções que já existem. Se algo essencial for ambíguo
(onde o painel mora, quem pode vê-lo, qual o padrão de UI), pergunte antes de decidir.

## Como a integração deve ser desenhada (regras fixas)

1. **O token nunca vai para o navegador.** A API do rePET não habilita CORS (de propósito) e o token
   dá controle total das transmissões. Crie uma **camada de servidor** na nossa aplicação (um
   serviço/cliente HTTP mais controllers/rotas próprias) que chama o rePET com o token e repassa o
   resultado ao front. O front só fala com o backend da nossa aplicação, usando a autenticação de
   usuário que ela já tem. Só usuários autorizados da nossa aplicação podem acionar essas rotas.
2. **Configuração por ambiente:** `REPET_API_URL` (por exemplo `http://SERVIDOR:8000`, sem barra no
   fim) e `REPET_API_TOKEN`. Nada de URL ou token escrito no código ou versionado.
3. **Nunca registre o token em log**, nem a stream key do YouTube, nem o header `Authorization`.
4. O tráfego é HTTP puro na rede interna. Se a nossa aplicação ficar fora dessa rede, avise-me: é
   preciso um proxy HTTPS na frente da API antes de usar.

## Autenticação (Bearer)

Toda rota de controle exige:

```
Authorization: Bearer <token>
```

O servidor aceita **três tipos de token equivalentes** (qualquer um abre as mesmas rotas):

- uma **chave nomeada de integração** (`API_TOKENS` no servidor do rePET): é a que lhe foi entregue
  para este trabalho, exclusiva desta integração e revogável sozinha;
- o **token próprio da API de live** (`LIVE_API_KEY`); ou
- o **token que já usamos para integrar com o Lara** (`REPLAY_API_TOKEN`, o token Sanctum, enviado
  inteiro, com o `id|` na frente, por exemplo `7|abc...`).

Use a chave que lhe foi entregue. Se a aplicação já tiver o token do Lara e preferir reaproveitá-lo
em vez de guardar um segredo a mais, também funciona: o comportamento da API é idêntico.

Falhas de autenticação:

| Situação | Código | Corpo |
|---|---|---|
| Token errado ou vazio | `401` (header `WWW-Authenticate: Bearer`) | `{"detail": "Token inválido."}` |
| Nenhuma credencial enviada | `401` | `{"detail": "Chave inválida ou sessão ausente."}` |
| Servidor sem nenhum token configurado | `503` | `{"detail": "Nenhum token configurado (LIVE_API_KEY / REPLAY_API_TOKEN / API_TOKENS)."}` |

Um `401` ou `503` aqui é problema de configuração, não de uso: mostre uma mensagem clara de
"integração indisponível" ao operador e registre o evento (sem o token).

## Como conferir o contrato por conta própria

Além deste texto, a API publica o contrato legível por máquina (OpenAPI, com os corpos de requisição
e os esquemas): `GET $BASE/openapi.json`, e a página interativa em `$BASE/docs` (botão *Authorize*:
cole o token). Em caso de divergência entre este texto e a API em execução, vale a API: avise-me.
Para checar que o token funciona, sem efeito colateral nenhum:

```bash
curl -s -o /dev/null -w "%{http_code}\n" $BASE/status -H "Authorization: Bearer $REPET_API_TOKEN"   # 200 = ok, 401 = token errado
```

## Vocabulário

- **quadra_id**: identificador de uma quadra, por exemplo `loc1-quadra1`. Vem do `GET /status`: monte a
  lista de quadras a partir dele, **nunca fixe ids no código**.
- **Lado `a` / `b`**: as duas duplas. Em todas as rotas é literalmente `a` ou `b` (aceita maiúscula).
- **Dois modos de transmissão** (excludentes na mesma quadra):
  - **Com placar** (`/broadcast`): o servidor reencoda o vídeo e desenha o placar por cima. É o modo
    que este painel controla por inteiro.
  - **Direta** (`/live`): a câmera empurra o vídeo ao YouTube, sem placar. O painel só liga, desliga e
    mostra o estado.

## Contrato da API

`BASE=http://SERVIDOR:8000`. Todos os exemplos usam `-H "Authorization: Bearer $REPET_API_TOKEN"`.
Todas as respostas são JSON. Em erro, o corpo é `{"detail": "mensagem em português"}`; **a única
exceção** é a validação automática (corpo malformado, `?delta=abc`), que devolve `422` com
`detail` como **lista** de objetos (`[{"type","loc","msg","input"}]`). Trate os dois formatos.

### 1. Estado geral: `GET /status` (a rota que o painel consulta em loop)

Uma chamada devolve todas as quadras, com os dois modos. **Não consulta câmera na hora** (responde
rápido); o estado da live direta vem de uma verificação em segundo plano a cada ~15 s. É a rota do
polling do painel: consulte a cada **2 s** enquanto a página estiver aberta e visível, e **pare quando
a aba ficar oculta**. Não use `GET /live/{quadra_id}` nem `GET /broadcast` em loop.

```bash
curl -s $BASE/status -H "Authorization: Bearer $REPET_API_TOKEN"
```

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
      "placar": {
        "quadra_id": "loc1-quadra1",
        "no_ar": true,
        "nomes": { "a": "ANA E BIA", "b": "CRIS E DUDA" },
        "placar": {
          "a": { "pontos": 2, "sets": 1 },
          "b": { "pontos": 1, "sets": 0 }
        },
        "volume": 100,
        "no_ar_ha_segundos": 312,
        "encerra_em_segundos": 6888,
        "reinicios": 0,
        "travamentos": 0,
        "transmitindo": true,
        "audio_da_camera": true
      }
    }
  ],
  "transmissoes_com_placar": { "no_ar": 1, "limite": 2 },
  "verificacao_na_subida": { "direta": [], "placar": ["loc1-quadra1"], "conflitos": [] }
}
```

Campos por quadra:

| Campo | Significado |
|---|---|
| `modo` | `placar`, `direta`, `conflito` (os dois ligados: situação anormal), `nenhum` (fora do ar) ou `desconhecido` (a câmera não respondeu e não há transmissão com placar) |
| `tem_stream_key` | Se `false`, a quadra não pode transmitir (as rotas de ligar dão `422`): desabilite os botões e explique |
| `chave_compartilhada_com` | Outras quadras que usam a **mesma** stream key do YouTube (um canal só) |
| `chave_em_uso_por` | Qual quadra de mesma key está no ar agora (`null` = nenhuma). Enquanto houver uma no ar, **ligar outra dá `409`** |
| `direta.no_ar` | `true`/`false`, ou `null` se a câmera não respondeu (veja `direta.erro`) |
| `placar` | O **objeto de estado** abaixo |
| `transmissoes_com_placar` | `no_ar` = quantas ligadas; `limite` = máximo simultâneo (2) |

**Objeto de estado** (devolvido por quase todas as rotas de `/broadcast`):

| Campo | Significado |
|---|---|
| `no_ar` | A transmissão foi pedida e está ativa |
| `transmitindo` | `true` só se o vídeo está **de fato saindo agora**. `no_ar: true` com `transmitindo: false` = "conectando/tentando" (câmera ou YouTube fora, reinício). Mostre isso como aviso amarelo, não como erro |
| `nomes.a`, `nomes.b` | Nomes das duplas como aparecem no vídeo (padrão `DUPLA A` / `DUPLA B`) |
| `placar.a.pontos`, `.sets` (e `b`) | Inteiros, nunca negativos |
| `volume` | % do volume da câmera: 100 = original, 0 = mudo, máximo 400 |
| `no_ar_ha_segundos` | Tempo no ar; `null` fora do ar |
| `encerra_em_segundos` | Contagem regressiva até o desligamento automático (prazo máximo de 2 h por padrão); `null` fora do ar. **Mostre um aviso quando restar pouco** (ex.: menos de 10 min) |
| `reinicios`, `travamentos` | Contadores de recuperação automática (informativos) |
| `audio_da_camera` | `false` = câmera sem áudio, a live sai muda (volume não tem efeito); `null` = não sondado |

### 2. Ligar a transmissão com placar: `POST /broadcast/{quadra_id}/start`

Corpo JSON **opcional**; todos os campos são opcionais:

| Campo | Tipo | Padrão | Descrição |
|---|---|---|---|
| `nome_a`, `nome_b` | string | `DUPLA A` / `DUPLA B` | Até 14 caracteres, viram MAIÚSCULAS; só ficam letras, números, espaço e `. & ' -` |
| `reset` | bool | `true` | `true` = jogo novo (placar 0x0 e volume 100%). `false` = mantém placar e volume (religar o mesmo jogo) |
| `volume` | int | 100 | % do volume da câmera (0 a 400) |

```bash
# jogo novo, com nomes
curl -s -X POST $BASE/broadcast/loc1-quadra1/start \
  -H "Authorization: Bearer $REPET_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"nome_a": "Ana e Bia", "nome_b": "Cris e Duda"}'

# religar o mesmo jogo, sem zerar nada
curl -s -X POST $BASE/broadcast/loc1-quadra1/start \
  -H "Authorization: Bearer $REPET_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"reset": false}'
```

Resposta `200`: o objeto de estado com um campo `status` na frente (`"iniciada"`, ou `"ja_no_ar"` se
já estava ligada; nesse caso nada é alterado, nem o corpo é aplicado).

```json
{ "status": "iniciada", "quadra_id": "loc1-quadra1", "no_ar": true,
  "nomes": {"a": "ANA E BIA", "b": "CRIS E DUDA"},
  "placar": {"a": {"pontos": 0, "sets": 0}, "b": {"pontos": 0, "sets": 0}},
  "volume": 100, "no_ar_ha_segundos": 0, "encerra_em_segundos": 7200,
  "reinicios": 0, "travamentos": 0, "transmitindo": false, "audio_da_camera": true }
```

**Esta chamada demora** (a API sonda a câmera, por vezes ~10 s): use timeout de **30 s**, desabilite o
botão enquanto espera, e **não repita automaticamente**. `transmitindo` costuma vir `false` nos
primeiros segundos; o polling do `/status` mostra quando passa a `true`.

### 3. Desligar: `POST /broadcast/{quadra_id}/stop` (ou `DELETE /broadcast/{quadra_id}`)

Sem corpo. Resposta: o objeto de estado com `status` `"encerrada"` ou `"nao_estava_no_ar"`. O placar e
os nomes ficam guardados. **Peça confirmação ao operador** antes de encerrar.

```bash
curl -s -X POST $BASE/broadcast/loc1-quadra1/stop -H "Authorization: Bearer $REPET_API_TOKEN"
```

### 4. Placar

Todas devolvem o objeto de estado já atualizado, sem corpo de requisição, a menos que indicado.

```bash
# +1 ponto para o lado a (ou b)
curl -s -X POST $BASE/broadcast/loc1-quadra1/ponto/a -H "Authorization: Bearer $REPET_API_TOKEN"
# desfaz um ponto (delta=-1); nunca fica abaixo de 0
curl -s -X POST "$BASE/broadcast/loc1-quadra1/ponto/a?delta=-1" -H "Authorization: Bearer $REPET_API_TOKEN"

# fecha o set: +1 set para o lado e ZERA OS PONTOS DOS DOIS LADOS
curl -s -X POST $BASE/broadcast/loc1-quadra1/set/a -H "Authorization: Bearer $REPET_API_TOKEN"
# corrige um set dado por engano: tira 1 set e NÃO mexe nos pontos
curl -s -X POST "$BASE/broadcast/loc1-quadra1/set/a?delta=-1" -H "Authorization: Bearer $REPET_API_TOKEN"

# correção manual: grava exatamente o que enviar; campos omitidos não mudam; {} não faz nada
curl -s -X PUT $BASE/broadcast/loc1-quadra1/placar \
  -H "Authorization: Bearer $REPET_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"pontos_a": 40, "pontos_b": 15, "sets_a": 1}'

# zera pontos e sets dos dois lados (nomes e volume ficam)
curl -s -X POST $BASE/broadcast/loc1-quadra1/placar/zerar -H "Authorization: Bearer $REPET_API_TOKEN"
```

**Peça confirmação** em *fechar set* (apaga os pontos do set) e em *zerar placar*. O placar aparece no
vídeo em menos de 1 s.

### 5. Nomes: `PUT /broadcast/{quadra_id}/nomes`

Corpo com `nome_a` e/ou `nome_b` (o que não vier não muda). Texto vazio volta ao nome padrão.

```bash
curl -s -X PUT $BASE/broadcast/loc1-quadra1/nomes \
  -H "Authorization: Bearer $REPET_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"nome_a": "Ana e Bia", "nome_b": "Cris e Duda"}'
```

Limite visual de 14 caracteres no campo; a resposta traz o nome já normalizado (maiúsculas, caracteres
inválidos removidos): **exiba o que a API devolveu**, não o que o usuário digitou.

### 6. Volume

```bash
# sobe/desce em passos (padrão 10 pontos percentuais; ?passo=20 muda o passo)
curl -s -X POST $BASE/broadcast/loc1-quadra1/volume/up   -H "Authorization: Bearer $REPET_API_TOKEN"
curl -s -X POST "$BASE/broadcast/loc1-quadra1/volume/down?passo=20" -H "Authorization: Bearer $REPET_API_TOKEN"

# define o valor (0 a 400; fora disso a API ajusta, sem erro). Corpo: {"nivel": <int>} obrigatório
curl -s -X PUT $BASE/broadcast/loc1-quadra1/volume \
  -H "Authorization: Bearer $REPET_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"nivel": 150}'
```

Vale na hora, sem derrubar a transmissão. Se `audio_da_camera` for `false`, desabilite os controles de
volume (não têm efeito). Há um limitador de pico, mas o microfone da câmera já chega perto do máximo em jogo: subir muito
satura o som. Prefira passos curtos e deixe claro que 100% é o volume original.

### 7. Estado de uma quadra ou de todas (uso pontual, não para polling)

```bash
curl -s $BASE/broadcast/loc1-quadra1 -H "Authorization: Bearer $REPET_API_TOKEN"   # objeto de estado
curl -s $BASE/broadcast               -H "Authorization: Bearer $REPET_API_TOKEN"   # {"no_ar": [...], "limite": 2, "transmissoes": {...}}
```

### 8. Live direta (sem placar): `/live`

Sem corpo em nenhuma. A câmera é quem transmite.

```bash
curl -s -X POST $BASE/live/loc1-quadra1       -H "Authorization: Bearer $REPET_API_TOKEN"
# {"quadra_id":"loc1-quadra1","status":"iniciada","max_seconds":7200.0}   (ou "ja_no_ar")

curl -s -X POST $BASE/live/loc1-quadra1/stop  -H "Authorization: Bearer $REPET_API_TOKEN"
# {"quadra_id":"loc1-quadra1","status":"encerrada"}   (ou "nao_estava_no_ar"); também existe DELETE /live/{quadra_id}

curl -s $BASE/live/loc1-quadra1               -H "Authorization: Bearer $REPET_API_TOKEN"
# {"quadra_id":"loc1-quadra1","no_ar":true}   (consulta a câmera na hora, pode levar ~10 s: não use em loop)
```

Use timeout de 30 s. Para o estado da live direta no painel, leia `direta.no_ar` do `GET /status`.
Atenção: `no_ar: true` significa só que a câmera foi **configurada** para transmitir; a confirmação de que o
YouTube recebeu a imagem só existe no YouTube Studio.

### Erros que o painel precisa tratar

| Código | Quando | Exemplo de `detail` | O que fazer na UI |
|---|---|---|---|
| `401` / `503` | Token errado ou ausente; nada configurado | `Token inválido.` | "Integração indisponível", avise o administrador; não insista |
| `404` | `quadra_id` desconhecido | `quadra_id 'x' não está em .../cameras.json.` | Recarregue a lista a partir do `/status` |
| `409` | Limite de 2 transmissões simultâneas | `limite de 2 transmissões simultâneas atingido.` | "Já há 2 quadras transmitindo; encerre uma" |
| `409` | A quadra está em live direta (ao ligar com placar) ou o inverso | `'loc1-quadra1' está em live direta; encerre-a antes (POST /live/loc1-quadra1/stop).` | Explique e ofereça encerrar a outra modalidade (com confirmação) |
| `409` | Outra quadra com a mesma stream key já está no ar | `A stream key de 'loc1-quadra1' é a mesma de 'loc1-quadra2', que já está no ar; encerre 'loc1-quadra2' antes.` | Nomeie a quadra que está usando a key |
| `422` | Quadra sem `youtube_stream_key` | `a quadra não tem youtube_stream_key em cameras.json.` | Deixe a quadra desabilitada (use `tem_stream_key`) |
| `422` | `lado` diferente de `a`/`b`; `direcao` diferente de `up`/`down` | `lado deve ser 'a' ou 'b'.` | Bug do cliente: não deve ocorrer |
| `422` | Corpo inválido (lista em `detail`) | `[{"type":"missing","loc":["body","nivel"],...}]` | Bug do cliente: registre |
| `502` | A câmera não respondeu ou recusou | `câmera recusou GET /ISAPI/Custom/RTMP (HTTP 400).` | "Câmera sem resposta"; permita tentar de novo |
| `503` | O vídeo estava reiniciando e não aceitou o comando de volume | `o ffmpeg não está lendo comandos agora (reiniciando?); tente de novo.` | Peça para tentar de novo em alguns segundos |

Mostre ao operador a mensagem do `detail` quando for string (ela já está em português e é clara);
trate `detail` lista como erro técnico.

## O que o painel deve fazer (requisitos de interface)

- Um **cartão por quadra**, montado a partir do `GET /status`, com nome da quadra, esporte e um selo
  do modo (`placar`, `direta`, `nenhum`, `conflito`, `desconhecido`).
- **Fora do ar:** campos de nome das duas duplas, botão *Iniciar transmissão com placar* e botão
  *Iniciar live direta*.
- **Com placar:** placar grande e legível; por lado: **+1 ponto**, **−1 ponto**, **fechar set** (com
  confirmação), **−1 set**; volume − / +; edição de nomes; *zerar placar* (com confirmação);
  *Encerrar* (com confirmação); tempo no ar e contagem regressiva do prazo (`encerra_em_segundos`).
- **Direta:** estado e botão *Encerrar* (com confirmação).
- **Avisos visíveis:** quadra sem stream key; limite de 2 transmissões atingido; `no_ar` sem
  `transmitindo` ("conectando..."); câmera sem áudio (`audio_da_camera: false`); câmera sem resposta
  (`modo: desconhecido` ou `direta.erro`); `modo: conflito`; prazo prestes a acabar.
- **Conflito (`modo: conflito`):** a API não desliga nada sozinha. Mostre um alerta forte e deixe o
  operador escolher qual modalidade encerrar.
- **Atualização:** polling do `/status` a cada 2 s (só com a aba visível). Depois de qualquer ação, use a
  resposta (que já traz o estado novo) para atualizar o cartão **imediatamente**, sem esperar o
  próximo ciclo.
- **Cliques repetidos:** desabilite o botão enquanto a chamada está em andamento. Para `ponto` e `set`,
  que **não são idempotentes** (cada chamada soma), nunca reenvie automaticamente após timeout ou erro
  de rede: consulte o estado e deixe o operador decidir.
- **Tolerância a indisponibilidade:** pelo agendamento atual o servidor do rePET reinicia todo dia às
  06:00 e a API fica fora do ar durante o reinício (as transmissões com placar são relançadas sozinhas
  depois, e a API também pode ser reiniciada em manutenção). Se o `/status` falhar, mantenha o
  último estado na tela marcado como "desatualizado" e continue tentando, sem tela de erro agressiva.
- Acessível em celular (o operador usa o painel na quadra), botões grandes, textos em português.

## Regras de segurança operacional (leia antes de testar)

- **Ligar uma transmissão de verdade publica vídeo ao vivo no YouTube.** `start` (com placar) e
  `POST /live` mexem em câmeras e canais reais. **Não os execute para testar sem a autorização
  explícita de um humano**, e **nunca teste duas transmissões ao mesmo tempo**: algumas quadras
  compartilham a mesma stream key de propósito, e ligar uma segunda recebe `409`.
- As rotas de **placar, nomes e volume funcionam com a transmissão desligada** (só gravam o valor) e
  `GET /status` é somente leitura: use-as para desenvolver e testar à vontade.
- Em testes automatizados da nossa aplicação, **simule** a API do rePET (mock do cliente HTTP) em vez de
  chamá-la. Cubra no mínimo: token enviado no header; mapeamento dos erros acima para mensagens da UI;
  `detail` como string e como lista; timeout; polling parando com a aba oculta.
- **Fora de escopo:** `POST /replay/{quadra_id}` (acionado pelo botão físico da quadra) e as rotas de
  replays não fazem parte deste painel. Não as chame.

## Entregáveis

1. Cliente HTTP do rePET na camada de servidor (token por env, timeouts, tradução de erros).
2. Rotas/controllers da nossa aplicação que expõem ao front as ações acima, protegidas pela
   autenticação de usuário existente.
3. A tela do painel, com os requisitos de interface.
4. Testes (com a API simulada) e a documentação curta de configuração: variáveis
   `REPET_API_URL` e `REPET_API_TOKEN`, e qual dos dois tokens aceitos foi usado.
5. No fim, um resumo do que foi feito, do que não foi e das decisões que dependem de mim.

Comece explorando o projeto e me diga o plano em poucas linhas antes de implementar.
