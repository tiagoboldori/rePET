# Plano de Ação — Motor de Replay e Cliente do Lara

**Projeto:** rePET — sistema de replay de vídeo para quadras esportivas
**Documento:** Plano de ação e priorização de requisitos (MoSCoW)
**Versão:** 3 — revisada em 17/09/2026 (mesmo dia da v2) para substituir o
pacote de cadastro e aplicação de logo local pela integração com o **Lara**,
sistema de gestão do clube, que passa a ser a fonte de verdade da
configuração operacional (orientação de vídeo, duração do clipe e
logomarca) e o repositório de entrega do clipe ao sócio.
**Documento complementar:** `PLANEJAMENTO.md` (cronograma e estimativas)

---

## 1. Objetivo do documento

Este plano consolida tudo o que a API do projeto deve expor, organizando os
requisitos por prioridade real de entrega. Cada item possui prioridade
atribuída, justificativa e critério objetivo de aceitação, de modo que a
decisão sobre o conteúdo do MVP esteja tomada antes do início do
desenvolvimento, e não no meio dele.

A priorização segue o método MoSCoW:

| Faixa | Significado prático neste projeto |
|---|---|
| **Must have** | Sem isso o MVP não se sustenta. Conjunto mínimo que precisa estar pronto e testado ao final do ciclo. Não é objeto de redução de escopo. |
| **Should have** | Importante e previsto para o ciclo, mas cuja ausência não impede o sistema de operar. É o amortecedor do cronograma. |
| **Could have** | Desejável, sem compromisso de entrega neste ciclo. |
| **Won't have (this cycle)** | Fora deste ciclo. Registrado para evitar retorno à pauta por esquecimento, com indicação de quando é esperado. |

---

## 2. Escopo

Conforme definido em 17/09/2026 e registrado no `README.md` do repositório,
este projeto compreende **exclusivamente o motor e a API**: captura
contínua e corte do clipe retroativo, API pública de consumo dos replays,
API de gerenciamento composta apenas por endpoints JSON e — a partir desta
revisão — o **cliente do Lara**: o componente que consulta a configuração
publicada pelo Lara, aplica mecanicamente o que ela determina e envia a ele
o clipe gerado.

**Não faz parte deste projeto** qualquer interface web de administração,
nem o cadastro ou a resolução de logomarca. Essa fronteira, que antes desta
revisão previa um cadastro de logo neste próprio repositório, muda de
direção: o Lara decide orientação, duração do clipe e logomarca; este
projeto só **executa** o que chega resolvido (baixa o overlay já composto e
o queima no clipe), nunca decide o conteúdo dessas configurações.

---

## 3. Premissas herdadas de decisões já fechadas

1. Arquitetura integralmente centralizada e permanente: um único backend
   atende os quatro locais.
2. Nenhum equipamento de processamento nos locais. A captura é feita por
   *pull* RTSP direto, através da intranet privada existente.
3. O botão físico chama a API diretamente, sem Home Assistant intermediando.
4. Os replays são públicos por quadra, sem conta de usuário e sem vínculo
   com sistema de reserva.
5. A superfície de gerenciamento é autenticada; HTTP Basic é aceitável no MVP.
6. O corte opera em `-c copy` e depende de as câmeras entregarem H.264
   nativo, configuração externa ao software.
7. **(Nova, 17/09/2026)** A configuração operacional (orientação, duração
   do clipe, logomarca) e a entrega definitiva do clipe ao sócio passam a
   ser responsabilidade do Lara. O motor consulta essa configuração via API
   e nunca a decide localmente.
8. **(Nova, 17/09/2026)** O caminho de resposta ao acionamento do botão
   nunca depende de uma chamada de rede síncrona ao Lara. A configuração
   usada em qualquer corte é sempre a última copiada para o cache local —
   o mesmo princípio de RNF3 (o botão não espera rede), agora estendido do
   banco/logo para a consulta ao Lara.

---

## 4. Nota técnica: adoção de SQLite nesta etapa

O contexto original definiu PostgreSQL, justificado pela concorrência de
escrita decorrente de "quatro locais escrevendo no mesmo backend pela rede".
Essa premissa deixou de existir com a correção da topologia: os locais não
escrevem nada, apenas disponibilizam streams RTSP, e o único processo que
escreve no banco é a própria API central.

Adota-se **SQLite nesta etapa**, com as seguintes condicionantes: acesso via
SQLModel/SQLAlchemy, mantendo o código agnóstico quanto ao banco; nenhum
recurso específico de SQLite; e migração para Postgres caso ocorra qualquer
um destes gatilhos — mais de um processo passar a escrever no banco,
necessidade de acesso concorrente por outro serviço, ou volume de replays
que torne as consultas paginadas sensíveis a desempenho.

**Observação relevante para esta versão do plano:** o worker assíncrono que
antes aplicava logo (seção 6 da v2) é substituído por um worker de
sincronização com o Lara, com três responsabilidades: puxar configuração
periodicamente, enviar o clipe (com fila e retentativa) e emitir
*heartbeat*. Continua sendo um segundo processo com escrita no banco,
restrita a atualização de estado de registros existentes, em fila
serializada — dentro do que o SQLite suporta com segurança em modo WAL.
Esse é o gatilho de migração mais próximo de se concretizar, e deve ser
reavaliado caso esse worker venha a ser paralelizado.

---

## 5. Modelo de dados

| Entidade | Campos principais | Observações |
|---|---|---|
| **Local** | identificador, nome, observações | Inalterado. |
| **Esporte** | identificador, nome | Inalterado. Deixa de ser pré-requisito de hierarquia de logo (que não existe mais aqui); continua útil para agrupamento local e para o campo enviado no cadastro cruzado com o Lara, se ele vier a precisar. |
| **Quadra** | identificador global, local, esporte, nome, URL RTSP, situação, **orientação**, **duração do clipe em segundos**, **caminho local do overlay estático**, **caminho local do overlay animado**, **hash de configuração** | Os cinco últimos campos são espelho local da configuração publicada pelo Lara (`GET /cameras`), escritos apenas pelo worker de sincronização — nunca editados diretamente nem por outra rota. A URL RTSP e a credencial nunca são expostas em resposta de API nem enviadas ao Lara (RNF8/RNF10). |
| **Replay** | identificador, quadra, arquivo bruto, arquivo com overlay aplicado, **estado de envio ao Lara**, **identificador do vídeo no Lara (uuid)**, **data/hora do envio confirmado**, data, duração, tamanho | Substitui os campos de logo aplicada. O estado de envio assume: pendente, enviado, falha. |

A entidade **Logo** deixa de existir neste sistema. Cadastro, escopo e
hierarquia de resolução (antes `quadra → esporte → local → global`) passam
a ser responsabilidade do Lara — o motor nunca mais precisa saber que esse
conceito de hierarquia existe, só recebe um arquivo de overlay já pronto
por câmera.

---

## 6. Integração com o Lara

O Lara expõe uma API própria (`<host do Lara>/api/replay`), autenticada por
token pessoal Sanctum (`Authorization: Bearer <REPLAY_API_TOKEN>`, gerado
pelo time do Lara e guardado apenas em variável de ambiente deste sistema).
O contrato completo, com todos os campos e exemplos de corpo, é o
`docs/replay-api.md` do repositório do Lara — este documento resume só o
que orienta as decisões de escopo daqui.

**1) Pull de configuração (barato, cacheado).** A cada 1-5 minutos, por
câmera: `GET /cameras/{external_id}` (ou `GET /cameras` para todas). A
resposta traz um `config_hash`. O worker de sincronização compara com o
valor salvo em `Quadra.config_hash` e só reprocessa quando ele mudar:
baixa o(s) arquivo(s) de overlay novo(s) para disco local e atualiza
`orientation`, `clip_seconds` e os caminhos de overlay na linha da
`Quadra`. Enquanto o hash não muda, não há nenhuma chamada de rede
adicional nem trabalho de disco. `external_id` é o mesmo identificador de
quadra já usado neste sistema (`loc1-quadra1` etc.) — precisa estar
previamente cadastrado no Lara; um `external_id` desconhecido lá devolve
404, tratado como risco operacional (seção 11), não como bug.

**2) O overlay é só aplicado, nunca decidido.** Vem como `overlay.png_url`
(sempre presente quando há layout) e, opcionalmente, `overlay.animated_url`
(WebM VP9 com alfa, para looping, presente só quando o Lara tem ffmpeg e o
layout usa GIF animado). É um arquivo único, do tamanho cheio do frame,
para aplicar em `(0,0)` — o motor nunca posiciona nada, só escala para a
resolução real do clipe quando ela difere de `overlay.width`/`overlay.height`
(mesma proporção, sem distorção). Preferir `animated_url` quando presente;
`png_url` é o caso comum.

**3) Aplicação mecânica no clipe.** Se a câmera tiver overlay configurado,
o clipe é reencodado uma vez (filtro `overlay` do ffmpeg) antes do envio —
único ponto em que o corte volta a pagar custo de CPU depois da otimização
`-c copy` (ver riscos, seção 11). Se não houver overlay (`overlay: null`),
o clipe segue para envio sem reencode algum.

**3.1) Música de fundo — decisão LOCAL, fora do contrato do Lara.** Depois
da orientação/overlay e antes do envio, o clipe pode ganhar uma trilha de
fundo sorteada localmente (`assets/music/`, ver README) — o Lara não tem
campo de áudio nesse contrato, então isso nunca é lido de `GET /cameras`.

**4) Envio do clipe.** `POST /cameras/{external_id}/videos`, multipart,
sempre com `external_id` do **clipe** (não confundir com o `external_id`
da câmera na URL — é o identificador que este sistema já usa para o
arquivo, `Replay.id`). É o que torna o envio idempotente: reenviar o mesmo
id devolve 200 com `duplicated: true`. `recorded_at` é sempre o instante do
aperto do botão (`Replay.criado_em`), nunca o do envio — determina a busca
de locação e a expiração de 7 dias no Lara, mesmo que o clipe tenha ficado
tempo na fila local.

**5) Fila local com retentativa.** O clipe é gravado em disco antes de
qualquer tentativa de envio (já é o comportamento atual). Falha de rede,
5xx ou 429 → retentar com backoff. 422 com corpo → registrar a mensagem e
não retentar (erro de formato não se resolve tentando de novo). 404 →
registrar e retentar em ciclo mais espaçado (provável cadastro pendente no
Lara, não um erro transitório). Nenhuma falha de envio impede a
disponibilidade local do replay (RNF9).

**6) Heartbeat.** `POST /cameras/{external_id}/heartbeat`, sem corpo, no
mesmo intervalo do pull de configuração, por câmera. Falha não interrompe
nada, só é registrada em log — alimenta apenas o "último contato" do lado
do Lara.

**7) Diagnóstico.** Como pedido explicitamente pelo lado do Lara: um
comando que chama `/ping` e `/cameras` e mostra, por câmera, a configuração
em vigor e se o overlay já foi baixado — cobre o item S2 (seção 7.2).

---

## 7. Requisitos funcionais priorizados

### 7.1 Must have — núcleo do MVP

| ID | Requisito | Justificativa |
|---|---|---|
| **M1** | Camada de persistência em SQLite, com tabelas e índices por quadra e data. | A única fonte de verdade sobre os clipes é hoje a listagem de diretório. Consulta por identificador, filtro por período e paginação eficiente dependem de índice adequado. |
| **M2** | Modelagem de **Local**, **Esporte** e **Quadra** como entidades, com migração do registro atual em arquivo. | Pré-requisito para guardar o espelho local da configuração do Lara por quadra. |
| **M3** | Registro automático do replay no banco no momento do corte. | Sem isso a tabela nasce vazia e o banco não reflete o disco. |
| **M4** | Rotina de reconciliação idempotente entre disco e banco. | Já existem clipes anteriores à introdução do banco. Serve também como recuperação, caso o banco seja perdido. |
| **M5** | `GET /api/replays/{replay_id}` — metadados do replay, incluindo estado de envio ao Lara. | Permite consultar um clipe pelo identificador, sem depender do nome do arquivo. |
| **M6** | `GET /api/replays/{replay_id}/media` — entrega da mídia, com suporte a requisições parciais, tipo de conteúdo correto e cabeçalhos de cache. | É a entrega do vídeo propriamente dita. Sem suporte a `Range`, o player não navega na linha de tempo de forma confiável e o download é sempre integral. |
| **M7** | `GET /api/quadras/{quadra_id}/replays` — listagem paginada, mais recente primeiro, com total de itens. | Consulta de uso mais frequente, tanto no consumo público quanto no gerenciamento. |
| **M8** | `DELETE /api/replays/{replay_id}` — remove registro e arquivos. | O conteúdo é público e sem controle de acesso na visualização. É a única medida de moderação disponível. |
| **M9** | **Cliente do Lara — autenticação e pull de configuração:** token via variável de ambiente, `GET /ping` para diagnóstico, `GET /cameras` periódico com cache local por `config_hash`, atualizando `Quadra` (orientação, duração do clipe, caminhos de overlay). | Sem isso o motor não sabe qual orientação/duração usar nem tem o que aplicar — é a base de tudo que depende do Lara. |
| **M10** | **Envio do clipe ao Lara:** aplicação mecânica do overlay em cache (queima via ffmpeg, sem reencode quando não há overlay) e `POST /cameras/{external_id}/videos` com fila local, idempotência por `external_id` do clipe e retentativa com backoff. | É a entrega em si — sem isso, os clipes continuam morrendo localmente, único motivo de existir esta integração. |
| **M11** | Autenticação HTTP Basic em toda a superfície de gerenciamento, preservando como públicos apenas os endpoints de consumo. | Requisito de segurança elementar. Endpoints de escrita e moderação não podem ficar abertos. |
| **M12** | Cobertura de testes automatizados dos itens acima, incluindo os casos de erro do cliente do Lara (401/403/404/422/429), a idempotência do envio e o cache por `config_hash`. | O projeto já opera com validação de ponta a ponta. A idempotência e o cache são exatamente o tipo de lógica que falha de forma silenciosa sem teste. |

### 7.2 Should have — previsto para o ciclo

| ID | Requisito | Justificativa |
|---|---|---|
| **S1** | Heartbeat periódico por câmera (`POST /cameras/{external_id}/heartbeat`). | Alimenta o "último contato" do lado do Lara, permitindo descobrir câmera muda antes do sócio reclamar. Classificado como *Should* porque a própria falha do heartbeat é, por contrato, inofensiva — não bloqueia nem afeta a entrega do replay. |
| **S2** | Diagnóstico: comando ou tela que chama `/ping` e `/cameras` e mostra, por câmera, a configuração em vigor e se o overlay já foi baixado. | Entrega explicitamente pedida pelo lado do Lara. Não bloqueia a entrega do replay em si, mas é o que permite validar a integração sem depender de acionar o botão físico. |
| **S3** | Retenção local do arquivo bruto/com overlay, por período curto (proposto: 3 dias, a confirmar com o responsável), usada apenas pela página de teste interna `/quadra/{id}` — não é a entrega oficial ao sócio (essa é do Lara, com retenção de 7 dias). | Evita crescimento ilimitado de disco local sem depender da política de retenção do Lara. |

### 7.3 Could have

| ID | Requisito | Observação |
|---|---|---|
| **C1** | `GET /api/config` e `PATCH /api/config` — parâmetros operacionais em tempo de execução. | Exige introduzir estado mutável: os parâmetros hoje são variáveis de ambiente fixadas na inicialização. |
| **C2** | Unit systemd para a API, para o worker de sincronização com o Lara, cron de sistema e montagem real de tmpfs. | Higiene de implantação, hoje suprida pelo `start.sh`. |

*(Numeração C3/C4 da v2 — cadastro de logo e reprocessamento — eliminada:
deixou de ter objeto, ver seção 12.)*

### 7.4 Won't have neste ciclo

| Item | Situação |
|---|---|
| Cadastro de logomarca e resolução por hierarquia | **Passa a ser responsabilidade do Lara.** Fora do escopo deste projeto em definitivo — não é adiamento, é remoção permanente de escopo (ver seção 12). |
| Gerenciamento de câmeras via API (criar, alterar, remover) | **Promovido de volta ao ciclo em 17/09/2026 — Opção B (seção 8).** Agendado como PT-16, ver `PLANEJAMENTO.md` seção 4. Trata do registro local (RTSP/credencial) — independente do cadastro de `external_id` no Lara, que é do time de lá. |
| Endpoint de saúde e defasagem por câmera | **Promovido de volta ao ciclo — Opção B.** Agendado como PT-17. |
| Listagem de replays entre quadras, com filtros | **Promovido de volta ao ciclo — Opção B.** Agendado como PT-18. |
| Gerenciamento de locais e esportes via API | **Deslocado para o ciclo seguinte.** As entidades são criadas em M2; apenas a manutenção via API fica adiada. |
| Qualquer interface web de administração | Fora do escopo do projeto por definição. |
| Migração para PostgreSQL | Adiada conforme a seção 4, por gatilho e não por calendário. |
| Aceleração de vídeo em hardware | Depende do servidor central, ainda não definido. Volta a ser relevante com o reencode de overlay (seção 6, item 3) — reavaliar se o custo de CPU se mostrar alto na prática. |
| Trilha de áudio nos clipes | **Implementado em 22/09/2026**, fora do ciclo MoSCoW original — decisão local (não faz parte do contrato do Lara), ver `integrations/audio.py` e README seção "Integração com o Lara". |
| Contas de cliente, reivindicação de replay, integração com reserva | Rejeitado em definitivo em decisão anterior. |
| Detecção por inteligência artificial e NVR completo | Descartado em decisão anterior. |

---

## 8. Consequência da substituição do pacote de logo pela integração com o Lara

A v2 deste plano estimava ~17h adicionais para o cadastro e a aplicação de
logo (M9/M10/S1 daquela versão). Essa integração com o Lara os substitui
por um pacote menor: pull de configuração com cache (M9 novo), aplicação
mecânica do overlay e envio do clipe (M10 novo), heartbeat e diagnóstico
(S1/S2 novos). A estimativa detalhada de horas fica pendente da leitura do
`docs/replay-api.md` completo do Lara (ainda não disponível neste
repositório) — nesta revisão, mantém-se como referência de ordem de
grandeza a mesma alocação de horas da v2 para o bloco correspondente
(seção 2 do `PLANEJAMENTO.md`), a ajustar assim que o contrato for lido.

**Importante:** a extensão do ciclo até 05/10 (Opção B, escolhida em
17/09/2026) não tinha relação com a logo — foi motivada por três pacotes
independentes (PT-16, PT-17, PT-18: gerenciamento de câmeras, saúde por
câmera e listagem entre quadras). Essa escolha permanece válida e não é
afetada por esta revisão. O que pode abrir alguma folga adicional no
cronograma é o corte do trabalho de modelagem/hierarquia de logo (M9/M10/S1
da v2), que era mais caro em pontos de função do que o novo pacote de
cliente do Lara tende a ser — a confirmar na atualização do
`PLANEJAMENTO.md`.

---

## 9. Requisitos não funcionais

| ID | Requisito |
|---|---|
| RNF1 | A entrega da mídia deve suportar requisições parciais, condição para navegação na linha de tempo e retomada de download. |
| RNF2 | As listagens devem ser paginadas por padrão, com limite máximo de itens por página. |
| RNF3 | O tempo de resposta do acionamento deve permanecer compatível com o limite de 15 segundos do firmware. Nem a gravação no banco, nem a consulta ao Lara, nem a aplicação do overlay ou o envio do clipe podem ocorrer no caminho da requisição. |
| RNF4 | A fila de envio ao Lara deve operar com concorrência controlada e backoff, nunca bloqueando o caminho do acionamento nem gerando picos simultâneos de CPU (reencode de overlay) quando vários botões são pressionados ao mesmo tempo. |
| RNF5 | O arquivo bruto/com overlay local deve ser preservado por um período curto de retenção (S3, seção 7.2), usado apenas para a página de teste interna — não é a entrega oficial ao sócio, que é feita pelo Lara com retenção de 7 dias. |
| RNF6 | O disco permanece como fonte de verdade final. O banco é índice de acesso e deve ser reconstruível a partir do conteúdo dos diretórios. |
| RNF7 | O código de acesso a dados deve permanecer independente do banco utilizado. |
| RNF8 | Nenhuma credencial de câmera pode ser exposta em respostas da API nem versionada no repositório. |
| RNF9 | Falha ao consultar o Lara, ao aplicar overlay, ao enviar o clipe ou ao registrar heartbeat não pode impedir a geração nem a disponibilização local do replay. Deve ser registrada em log e retentada quando aplicável, sem bloquear o caminho do acionamento. |
| RNF10 | A URL RTSP e qualquer credencial de câmera nunca podem ser enviadas ao Lara — permanecem estritamente locais (mesma garantia de RNF8, explícita quanto ao novo destino externo). |
| RNF11 | O envio de clipe ao Lara deve ser idempotente por `external_id` do clipe, permitindo reenvio seguro em caso de falha de rede sem duplicar o vídeo entregue ao sócio. |

---

## 10. Critérios de aceitação

**Bloco Must**

1. Um acionamento gera o clipe em disco e a linha correspondente no banco,
   usando a orientação e a duração de clipe da última configuração
   sincronizada do Lara para aquela quadra (nunca uma consulta ao vivo).
2. Falha de rede ao consultar o Lara, aplicar overlay ou enviar o clipe não
   impede a geração nem a disponibilidade local do replay.
3. Reenviar o mesmo clipe (mesmo `external_id`) ao Lara não duplica o vídeo
   — verificado pela resposta com `duplicated: true` ou 200 em vez de 201.
4. A reconciliação, executada sobre diretório com clipes preexistentes,
   insere apenas registros ausentes e é idempotente.
5. `GET /api/replays/{id}/media` entrega o vídeo, responde corretamente a
   requisição com cabeçalho `Range` e permite navegação no player.
6. A listagem por quadra pagina corretamente, inclusive nos casos de página
   vazia e última página parcial.
7. A remoção elimina registro e arquivos, e o item deixa de ser listado.
8. Todos os endpoints de gerenciamento respondem 401 sem credencial válida.
9. A suíte de testes é executada integralmente sem falhas.

**Bloco Should**

1. O heartbeat roda no intervalo configurado e sua falha não aparece em
   lugar nenhum além do log.
2. O comando de diagnóstico mostra, para cada câmera, a configuração em
   vigor e se o overlay já está em cache local.
3. O arquivo local é removido após o período de retenção definido em S3,
   sem depender de o envio ao Lara ter sido confirmado antes disso.

---

## 11. Riscos e dependências

| Risco ou dependência | Impacto | Tratamento |
|---|---|---|
| Custo de CPU do reencode do overlay em servidor sem aceleração de hardware | Fila acumulada em horário de pico | Fila serializada; aceleração por hardware como otimização posterior (mesmo raciocínio da v2, agora aplicado ao overlay em vez da logo local) |
| Câmera nova entregando HEVC em vez de H.264 | Clipe com problema de reprodução | Verificação com `ffprobe` no RTSP durante o cadastro |
| `external_id` de câmera não cadastrado no Lara | Envio de clipe fica preso na fila (404 permanente) | Checklist de sincronização do cadastro de câmeras com o time do Lara antes de subir cada quadra nova |
| Indisponibilidade ou alta latência do Lara | Fila de envio cresce; heartbeat falha | Cache local de configuração (config_hash) garante que o corte continua funcionando; fila com backoff absorve a indisponibilidade. **Materializado em 2026-09-22:** `/api/replay/*` fora do ar no ambiente `192.168.100.48:8000` (rotas fora dessa base, ex. `/api/ping`, respondiam normal) — fila represou ~10-13 replays em `PENDENTE`, backoff absorveu sem travar o resto do sistema, conforme projetado. |
| Banda do *backbone* da intranet | Perda de segmentos | Pendente de confirmação junto à infraestrutura |
| Ponto único de falha no servidor central | Indisponibilidade simultânea nos quatro locais | Risco aceito na decisão de arquitetura |

---

## 12. Decisões que condicionavam este plano

1. ~~Ordem de precedência entre esporte e local na resolução da logo~~ —
   **sem objeto.** A resolução de logo não é mais responsabilidade deste
   projeto; a hierarquia e sua precedência passam a ser do Lara.
2. ~~Replay sem logo pode ser exibido publicamente durante o
   processamento?~~ — **sem objeto**, pelo mesmo motivo do item acima.
3. ~~Quais escopos de logo existem de fato no negócio?~~ — **sem objeto**,
   idem.
4. ~~Política de retenção dos replays públicos e do arquivo bruto~~ — a
   retenção de 14 dias definida em 17/09/2026 valia para a entrega
   diretamente por este sistema. Com o Lara assumindo a entrega ao sócio
   (retenção de 7 dias, dele), a retenção local passa a ser só de apoio ao
   teste interno — **proposta: 3 dias (S3, seção 7.2), a confirmar com o
   responsável.**
5. Hardware do servidor central — **sem previsão de mudança** ("até segunda
   ordem"). Volta a pesar no reencode de overlay (seção 6, item 3), mas
   isso não é pré-requisito — só afeta tempo de fila.
6. ~~Escolha entre as opções A e B da seção 8 (v2)~~ — **Opção B**,
   permanece válida (motivada por PT-16/17/18, não pela logo — ver seção 8
   desta versão).
7. **(Nova, 17/09/2026)** Substituição do pacote de logo local pela
   integração com o Lara — decidida em conversa com o responsável, que
   preferiu o escopo enxuto de "cliente do Lara" à modelagem própria de
   logo/hierarquia neste repositório.

Decisão ainda em aberto que **não bloqueia** o início do desenvolvimento:
o número exato de dias de retenção local (item 4 acima, proposto 3 dias).
