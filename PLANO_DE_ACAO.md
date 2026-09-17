# Plano de Ação — Motor de Replay e API de Gerenciamento

**Projeto:** rePET — sistema de replay de vídeo para quadras esportivas
**Documento:** Plano de ação e priorização de requisitos (MoSCoW)
**Versão:** 2 — revisada em 17/09/2026 para incorporar a sobreposição de logo
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
este projeto compreende **exclusivamente o motor e a API**: captura contínua
e corte do clipe retroativo, API pública de consumo dos replays e API de
gerenciamento composta apenas por endpoints JSON.

**Não faz parte deste projeto** qualquer interface web de administração.
Qualquer painel que venha a consumir a API de gerenciamento é
responsabilidade de outra equipe. Essa fronteira vale também para as
funcionalidades de logo: este projeto entrega o endpoint de cadastro e o
mecanismo de aplicação, nunca a tela de upload.

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

**Observação relevante para esta versão do plano:** a introdução do worker
assíncrono de aplicação de logo (seção 6) cria um segundo processo com
escrita no banco. Trata-se de escrita restrita a atualização de estado de
registros existentes, em fila serializada com concorrência unitária, o que
permanece dentro do que o SQLite suporta com segurança em modo WAL. Ainda
assim, esse é hoje o gatilho de migração mais próximo de se concretizar, e
deve ser reavaliado caso o worker venha a ser paralelizado.

---

## 5. Modelo de dados

O modelo abaixo torna explícita a estrutura de local, quadra e esporte, que
até aqui existia de forma implícita — o local era apenas um prefixo textual
no identificador da quadra, e o esporte não existia em lugar algum.

| Entidade | Campos principais | Observações |
|---|---|---|
| **Local** | identificador, nome, observações | Passa a ser entidade própria, e não prefixo do identificador da quadra. Habilita agrupamento e filtro no gerenciamento. |
| **Esporte** | identificador, nome | Tabela de domínio (futsal, society, beach tennis, vôlei de areia e outros). Necessária para o escopo de logo por esporte. |
| **Quadra** | identificador global, local, esporte, nome, URL RTSP, situação | Mantém o identificador global já em uso, que continua evitando rota aninhada nas páginas públicas. A URL RTSP nunca é exposta em respostas da API. |
| **Logo** | identificador, nome, arquivo, escopo, alvo do escopo, posição, margem, opacidade, situação | Escopo assume um de quatro valores: global, local, esporte ou quadra. O alvo identifica a instância correspondente, exceto no escopo global. |
| **Replay** | identificador, quadra, arquivo bruto, arquivo marcado, logo aplicada, estado de processamento, data, duração, tamanho | O estado assume: bruto, processando, marcado ou falha. O registro da logo aplicada torna o resultado auditável. |

### 5.1 Hierarquia de resolução da logo

A logo efetiva de uma quadra é determinada por precedência, da mais
específica para a mais genérica:

**quadra → esporte → local → global**

Vence a primeira logo ativa encontrada. A ordem se justifica porque o
esporte é atributo da quadra, sendo portanto mais específico que o local,
que agrupa quadras de esportes distintos. A resolução é determinística e
cada replay registra qual logo foi efetivamente aplicada.

Registra-se um ponto que merece confirmação: se a prioridade comercial for
a marca do patrocinador do local, e não a do esporte, a ordem entre esses
dois níveis deve ser invertida. A inversão custa a alteração de uma
constante, mas precisa ser decidida antes da implementação para não gerar
retrabalho de dados.

---

## 6. Estratégia de aplicação da logo

A logo precisa estar queimada no arquivo, conforme decidido, para valer
também em qualquer download e não apenas na visualização pelo site. Isso
implica decodificar e recodificar o vídeo, limitação inerente a codecs
preditivos e não do ffmpeg em particular, com custo de CPU estimado entre
1000% e 1200% no cenário sem aceleração de hardware.

A solução adotada separa o que é caro do que não é:

1. **O corte permanece em `-c copy`** e responde ao acionamento do botão em
   menos de um segundo, preservando com folga o tempo limite de 15 segundos
   configurado no firmware.
2. **O replay é registrado no estado bruto e enfileirado.**
3. **Um worker único, com concorrência unitária, aplica a logo** por
   sobreposição via ffmpeg, com preset `ultrafast`, gerando o arquivo
   marcado e atualizando o estado do registro.
4. **A entrega prefere o arquivo marcado** quando ele existe, recorrendo ao
   bruto enquanto o processamento não concluiu.

Essa arquitetura produz três efeitos relevantes. O custo do reencode sai do
caminho da requisição, de modo que a latência do botão não é afetada. A fila
serializada impede picos concorrentes de CPU, cenário que ocorreria se dois
botões de locais diferentes fossem pressionados ao mesmo tempo. E a
aceleração por hardware deixa de ser pré-requisito, passando a ser
otimização que altera apenas a linha de comando do worker — o que remove a
dependência em relação à definição do servidor central, ainda pendente.

O arquivo bruto é preservado, sujeito à política de retenção, para permitir
reprocessamento quando uma logo for substituída.

**Alternativa complementar, não excludente.** A função *Picture Overlay*
nativa das câmeras HiLook sobrepõe a imagem no próprio processamento da
câmera, com custo zero de servidor. Atende apenas escopo fixo por câmera,
está limitada a arquivo BMP de 24 bits com 128 por 128 pixels, e não há
confirmação de que a sobreposição permaneça no stream RTSP em vez de apenas
na visualização. Permanece como opção para quem aceitar logo fixa por
quadra, sem variação por esporte ou campanha.

---

## 7. Requisitos funcionais priorizados

### 7.1 Must have — núcleo do MVP

| ID | Requisito | Justificativa |
|---|---|---|
| **M1** | Camada de persistência em SQLite, com tabelas e índices por quadra e data. | A única fonte de verdade sobre os clipes é hoje a listagem de diretório. Consulta por identificador, filtro por período e paginação eficiente dependem de índice adequado. |
| **M2** | Modelagem de **Local**, **Esporte** e **Quadra** como entidades, com migração do registro atual em arquivo. | Pré-requisito da hierarquia de logo e do agrupamento por local. Enquanto o local é apenas prefixo textual, não há como filtrar nem aplicar escopo de forma confiável. |
| **M3** | Registro automático do replay no banco no momento do corte. | Sem isso a tabela nasce vazia e o banco não reflete o disco. |
| **M4** | Rotina de reconciliação idempotente entre disco e banco. | Já existem clipes anteriores à introdução do banco. Serve também como recuperação, caso o banco seja perdido. |
| **M5** | `GET /api/replays/{replay_id}` — metadados do replay, incluindo estado de processamento e logo aplicada. | Permite consultar um clipe pelo identificador, sem depender do nome do arquivo. |
| **M6** | `GET /api/replays/{replay_id}/media` — entrega da mídia, com suporte a requisições parciais, tipo de conteúdo correto e cabeçalhos de cache, preferindo o arquivo marcado quando disponível. | É a entrega do vídeo propriamente dita. Sem suporte a `Range`, o player não navega na linha de tempo de forma confiável e o download é sempre integral. |
| **M7** | `GET /api/quadras/{quadra_id}/replays` — listagem paginada, mais recente primeiro, com total de itens. | Consulta de uso mais frequente, tanto no consumo público quanto no gerenciamento. |
| **M8** | `DELETE /api/replays/{replay_id}` — remove registro e arquivos. | O conteúdo é público e sem controle de acesso na visualização. É a única medida de moderação disponível. |
| **M9** | **Cadastro de logos:** `POST /api/logos` (envio com escopo), `GET /api/logos` (listagem com filtro por escopo), `PATCH /api/logos/{id}` e `DELETE /api/logos/{id}`. | Conjunto sem qualquer custo de processamento de vídeo. Entrega à equipe de frontend a superfície necessária para começar, independentemente de quando a aplicação no vídeo estiver pronta. |
| **M10** | **Regra de resolução da logo efetiva**, conforme a hierarquia da seção 5.1, aplicada no momento do corte e registrada no replay. | É o que dá sentido ao escopo. Sem a regra, o cadastro de logos por local ou esporte não produz efeito. |
| **M11** | Autenticação HTTP Basic em toda a superfície de gerenciamento, preservando como públicos apenas os endpoints de consumo. | Requisito de segurança elementar. Endpoints de escrita e moderação não podem ficar abertos. |
| **M12** | Cobertura de testes automatizados dos itens acima, incluindo os casos de erro existentes e a resolução de logo em cada nível da hierarquia. | O projeto já opera com validação de ponta a ponta. A hierarquia de precedência é exatamente o tipo de lógica que falha de forma silenciosa sem teste. |

### 7.2 Should have — previsto para o ciclo

| ID | Requisito | Justificativa |
|---|---|---|
| **S1** | **Aplicação assíncrona da logo no clipe**, conforme a seção 6: enfileiramento, worker serializado, geração do arquivo marcado, atualização de estado, tratamento de falha e preferência de entrega pelo arquivo marcado. | É a materialização da logo no vídeo. Classificado como *Should*, e não *Must*, por uma razão de método e não de importância: é o único pacote do ciclo cuja remoção não inviabiliza o MVP, e portanto é o amortecedor natural do cronograma. O cadastro e a resolução, que são *Must*, garantem que nada do trabalho de modelagem se perca caso a aplicação escorregue para o ciclo seguinte. |

### 7.3 Could have

| ID | Requisito | Observação |
|---|---|---|
| **C1** | `GET /api/quadras/{id}/logo-efetiva` — exposição explícita da resolução. | Útil para depuração e para o frontend exibir qual logo será aplicada. A lógica já existe em M10; o endpoint apenas a expõe. |
| **C2** | `POST /api/replays/{id}/reprocessar` — reaplicação da logo após substituição. | Depende de S1 estar concluído. O arquivo bruto preservado já viabiliza a operação. |
| **C3** | `GET /api/config` e `PATCH /api/config` — parâmetros operacionais em tempo de execução. | Exige introduzir estado mutável: os parâmetros hoje são variáveis de ambiente fixadas na inicialização. |
| **C4** | Rotina de retenção dos clipes finais, com período configurável. | Bloqueado por decisão de negócio: a política de retenção permanece indefinida. A implementação é simples; o número não existe. |
| **C5** | Unit systemd para a API, cron de sistema e montagem real de tmpfs. | Higiene de implantação, hoje suprida pelo `start.sh`. |

### 7.4 Won't have neste ciclo

| Item | Situação |
|---|---|
| Gerenciamento de câmeras via API (criar, alterar, remover) | **Deslocado para o ciclo seguinte** em razão da promoção da logo. Ver seção 8. |
| Endpoint de saúde e defasagem por câmera | **Deslocado para o ciclo seguinte.** Ver seção 8. |
| Listagem de replays entre quadras, com filtros | **Deslocado para o ciclo seguinte.** |
| Gerenciamento de locais e esportes via API | **Deslocado para o ciclo seguinte.** As entidades são criadas em M2; apenas a manutenção via API fica adiada, permanecendo por carga inicial. |
| Qualquer interface web de administração | Fora do escopo do projeto por definição. |
| Migração para PostgreSQL | Adiada conforme a seção 4, por gatilho e não por calendário. |
| Aceleração de vídeo em hardware | Depende do servidor central, ainda não definido. Deixou de ser pré-requisito com a arquitetura da seção 6. |
| Trilha de áudio nos clipes | Tecnicamente barata, sem prioridade de produto no momento. |
| Contas de cliente, reivindicação de replay, integração com reserva | Rejeitado em definitivo em decisão anterior. |
| Detecção por inteligência artificial e NVR completo | Descartado em decisão anterior. |

---

## 8. Consequência da promoção da logo

A inclusão do cadastro e da aplicação de logo acrescenta aproximadamente 17
horas ao ciclo, que já estava integralmente alocado. Como o bloco *Must* não
admite redução, três pacotes anteriormente previstos como *Should* foram
deslocados para o ciclo seguinte: gerenciamento de câmeras, saúde por
câmera e listagem entre quadras.

O deslocamento tem um custo operacional que merece registro explícito. O
endpoint de saúde por câmera existia para tornar proativa a detecção de
câmera travada, hoje descoberta somente quando alguém pressiona o botão e
não recebe o replay. O incidente de defasagem de aproximadamente dez
minutos, ocorrido em 17/09, é precedente concreto. Enquanto esse endpoint
não existir, a verificação permanece manual.

Há duas opções, e a escolha é do responsável pelo projeto:

| Opção | Efeito |
|---|---|
| **A — recomendada.** Manter a janela de dez dias úteis e deslocar os três pacotes. | Logo entregue no ciclo. Monitoramento de câmeras permanece manual por mais um ciclo. |
| **B.** Estender o ciclo em aproximadamente três dias úteis, até 05/10. | Logo entregue e monitoramento preservado, ao custo de atraso no encerramento do ciclo. |

O planejamento em `PLANEJAMENTO.md` está construído sobre a opção A.

---

## 9. Requisitos não funcionais

| ID | Requisito |
|---|---|
| RNF1 | A entrega da mídia deve suportar requisições parciais, condição para navegação na linha de tempo e retomada de download. |
| RNF2 | As listagens devem ser paginadas por padrão, com limite máximo de itens por página. |
| RNF3 | O tempo de resposta do acionamento deve permanecer compatível com o limite de 15 segundos do firmware. Nem a gravação no banco nem a aplicação da logo podem ocorrer no caminho da requisição. |
| RNF4 | A aplicação da logo deve operar com concorrência unitária, evitando picos simultâneos de CPU. |
| RNF5 | O arquivo bruto deve ser preservado enquanto a política de retenção permitir, viabilizando reprocessamento. |
| RNF6 | O disco permanece como fonte de verdade final. O banco é índice de acesso e deve ser reconstruível a partir do conteúdo dos diretórios. |
| RNF7 | O código de acesso a dados deve permanecer independente do banco utilizado. |
| RNF8 | Nenhuma credencial de câmera pode ser exposta em respostas da API nem versionada no repositório. |
| RNF9 | A falha na aplicação da logo não pode impedir a entrega do replay. O estado de falha deve ser registrado e o arquivo bruto permanecer disponível. |

---

## 10. Critérios de aceitação

**Bloco Must**

1. Um acionamento gera o clipe em disco e a linha correspondente no banco,
   com a logo efetiva já resolvida e registrada.
2. A resolução é verificada nos quatro níveis: logo cadastrada por quadra
   prevalece sobre a de esporte, que prevalece sobre a de local, que
   prevalece sobre a global; e a ausência de qualquer uma delas não gera erro.
3. A reconciliação, executada sobre diretório com clipes preexistentes,
   insere apenas registros ausentes e é idempotente.
4. `GET /api/replays/{id}/media` entrega o vídeo, responde corretamente a
   requisição com cabeçalho `Range` e permite navegação no player.
5. A listagem por quadra pagina corretamente, inclusive nos casos de página
   vazia e última página parcial.
6. A remoção elimina registro e arquivos, e o item deixa de ser listado.
7. Todos os endpoints de gerenciamento respondem 401 sem credencial válida.
8. A suíte de testes é executada integralmente sem falhas.

**Bloco Should**

1. Após o acionamento, o replay é entregue imediatamente no estado bruto, e
   o arquivo marcado passa a ser preferido na entrega tão logo o
   processamento conclua.
2. Dois acionamentos simultâneos em quadras distintas não geram execução
   concorrente de reencode.
3. Falha de processamento registra o estado correspondente sem impedir o
   acesso ao arquivo bruto.

---

## 11. Riscos e dependências

| Risco ou dependência | Impacto | Tratamento |
|---|---|---|
| Custo de CPU do reencode em servidor sem aceleração de hardware | Fila acumulada em horário de pico | Fila serializada e preset `ultrafast`; aceleração por hardware como otimização posterior |
| Câmera nova entregando HEVC em vez de H.264 | Clipe com problema de reprodução | Verificação com `ffprobe` no RTSP durante o cadastro |
| Ordem de precedência entre esporte e local divergir da intenção comercial | Logo errada aplicada de forma sistemática | Confirmar a ordem antes da implementação de M10 |
| Monitoramento de câmeras adiado | Câmera travada descoberta somente no acionamento | Verificação manual periódica até o ciclo seguinte |
| Banda do *backbone* da intranet | Perda de segmentos | Pendente de confirmação junto à infraestrutura |
| Política de retenção indefinida | Acervo cresce sem limite, e o arquivo bruto duplica o consumo | C4 permanece bloqueado até a definição do prazo |
| Ponto único de falha no servidor central | Indisponibilidade simultânea nos quatro locais | Risco aceito na decisão de arquitetura |

---

## 12. Decisões pendentes que condicionam este plano

1. Ordem de precedência entre esporte e local na resolução da logo
   (condiciona M10).
2. Se um replay ainda sem logo pode ser exibido publicamente durante o
   processamento, ou se deve permanecer oculto até estar marcado.
3. Quais escopos de logo existem de fato no negócio, além dos quatro
   previstos — em particular, se haverá logo por campanha ou por período.
4. Política de retenção dos replays públicos e do arquivo bruto (bloqueia C4).
5. Hardware do servidor central, que deixa de bloquear a logo mas define o
   tempo de fila.
6. Escolha entre as opções A e B da seção 8.
