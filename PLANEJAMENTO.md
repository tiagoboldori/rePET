# Planejamento de Execução — Ciclo de Duas Semanas Úteis

**Projeto:** rePET — sistema de replay de vídeo para quadras esportivas
**Versão:** 2 — revisada em 17/09/2026 para incorporar a sobreposição de logo
**Documento de referência:** `PLANO_DE_ACAO.md` (requisitos e priorização MoSCoW)

Este planejamento estabelece o cronograma do ciclo e serve como instrumento
de acompanhamento do ritmo de execução. A seção 5 concentra os indicadores a
serem atualizados ao longo das duas semanas.

---

## 1. Estrutura

| Frente | Natureza | Início | Conclusão |
|---|---|---|---|
| **A — Desenvolvimento** (motor e API) | Software | 17/09/2026 | 30/09/2026 (10 dias úteis) |
| **B — Eletrônica** (perfboard, firmware, rede) | Bancada | D+1 após entrega dos componentes | D+8 |

A Frente B não possui data de calendário porque depende da entrega dos
componentes, ainda não adquiridos. Seu cronograma é expresso em dias
relativos ao recebimento.

As frentes são tecnicamente independentes: a API pode ser concluída e
validada por requisição HTTP sem botão físico, e o firmware depende apenas
do endpoint `POST /replay/{quadra_id}`, já implementado. Caso os componentes
cheguem durante a janela de 17/09 a 30/09, recomenda-se concluir o bloco
*Must have* antes de iniciar a bancada.

---

## 2. Estimativa: Análise de Pontos de Função

A APF dimensiona o software pela funcionalidade oferecida ao usuário, e não
pelo volume de código. Classifica cada item em cinco tipos — arquivos
internos (ALI), arquivos externos lidos (AIE), entradas (EE), saídas com
processamento (SE) e consultas simples (CE) —, atribui complexidade a cada
um e converte o total em horas por um índice de produtividade. A vantagem
prática é permitir estimativa antes de escrever código, de forma
independente da linguagem.

O método é aplicado somente à Frente A. Montagem de placa e configuração de
rede não são funcionalidade de software e foram estimadas por decomposição
de tarefas.

| Tipo | Itens | PF |
|---|---|---|
| ALI — arquivos internos | Replay, Quadra, Local, Esporte, Logo, Configuração | 42 |
| AIE — arquivos externos | Buffer de segmentos em tmpfs | 5 |
| EE — entradas | Acionamento, remoção de replay, três operações de câmera, configuração, três operações de logo, reconciliação, locais, reprocessamento | 45 |
| SE — saídas | Entrega de mídia, saúde da câmera, página pública, retenção, aplicação da logo, logo efetiva | 31 |
| CE — consultas | Replay individual, listagem por quadra, listagem entre quadras, câmeras, configuração, saúde, logos, locais | 29 |
| | **Não ajustados** | **152** |
| | Fator de ajuste (0,65 + 0,01 × 40) | 1,05 |
| | **Ajustados** | **≈ 160** |

**Distribuição**

| Faixa | PF | Esforço | Situação |
|---|---|---|---|
| Must have | 79 | 48 h | Comprometido |
| Should have (aplicação da logo) | 7 | 9 h | Comprometido, com função de amortecedor |
| Deslocado para o ciclo seguinte | 32 | ≈ 21 h | Fora da janela — ver seção 8 do Plano de Ação |
| Could have | 27 | ≈ 18 h | Fora do ciclo |
| Já implementado | 7 | — | Linha de base |

---

## 3. Capacidade

| Parâmetro | Valor |
|---|---|
| Dias úteis (17/09 a 30/09, sem feriado) | 10 |
| Dedicação considerada | 6 h/dia |
| Capacidade total | 60 h |
| Esforço alocado (Must, Should e fechamento) | 59,5 h |
| Reserva | 0,5 h |
| Produtividade média resultante | 0,69 h/PF |

A reserva é praticamente nula, e isso é deliberado. O amortecedor do ciclo
não são horas de folga, e sim o pacote PT-15 — a aplicação da logo —,
classificado como *Should have* justamente para poder ser deslocado sem
comprometer o MVP. Concluído o bloco *Must* ao final do oitavo dia, os dois
dias restantes são integralmente dedicados a PT-15; havendo desvio
acumulado, PT-15 escorrega para o ciclo seguinte sem qualquer perda do
trabalho de modelagem, que já estará entregue.

Observa-se que o índice de 0,69 h/PF é mais favorável que o adotado na
versão anterior deste planejamento, de 0,78. A razão é metodológica e não
otimismo: a criação de cinco tabelas pequenas e correlatas em um único
pacote acumula muitos pontos de função para um esforço de implementação
compartilhado. Em contrapartida, PT-15 apresenta 1,29 h/PF, pois
processamento de vídeo tem produtividade por ponto bem pior que operações
de cadastro.

---

## 4. Frente A — Cronograma e pacotes

| Dia | Data | Pacote de trabalho | PF | Horas |
|---|---|---|---|---|
| D1 | qui, 17/09 | PT-01 Camada de persistência (SQLite, sessão, índices) | 12 | 6 |
| D2 | sex, 18/09 | PT-10 Modelo Local, Esporte e Quadra, com migração do registro em arquivo (6 h de 8) | — | 6 |
| D3 | seg, 21/09 | PT-10 conclusão (2 h) + PT-02 Registro do replay no banco (4 h) | 27 | 6 |
| D4 | ter, 22/09 | PT-03 Reconciliação (3 h) + PT-04 Metadados do replay (1,5 h) + PT-05 Entrega da mídia, início (1,5 h) | 7 | 6 |
| D5 | qua, 23/09 | PT-05 conclusão (3,5 h) + PT-06 Listagem paginada por quadra (2,5 h de 3,5) | 5 | 6 |
| D6 | qui, 24/09 | PT-06 conclusão (1 h) + PT-07 Remoção de replay (2,5 h) + PT-08 Autenticação (2,5 h) | 7 | 6 |
| D7 | sex, 25/09 | PT-14 Logos: entidade, escopo, envio, listagem, remoção e regra de resolução (6 h de 8) | — | 6 |
| D8 | seg, 28/09 | PT-14 conclusão (2 h) + PT-09 Suíte de testes (4 h) — **marco M-1** | 21 | 6 |
| D9 | ter, 29/09 | PT-15 Aplicação assíncrona da logo: fila, worker, estado, fallback (6 h de 9) | — | 6 |
| D10 | qua, 30/09 | PT-15 conclusão (3 h) + documentação e fechamento (2,5 h) — **marco M-2** | 7 | 5,5 |
| | | **Total** | **86** | **59,5** |

O dia D1 é parcial, por iniciar na data de emissão deste documento. Como não
há reserva de horas, essa perda deve ser recuperada ao longo da primeira
semana ou absorvida por PT-15.

**Pontos de atenção.** PT-05 é o pacote com maior risco de subestimação: o
suporte a requisições parciais envolve cabeçalhos `Range`, respostas 206 e
tratamento de faixas inválidas, e deve ser validado com cliente real. PT-14
concentra a regra de precedência da logo, que é lógica de falha silenciosa e
exige teste em todos os quatro níveis da hierarquia. PT-08 e PT-09 não geram
pontos de função — autenticação é característica geral já contemplada no
fator de ajuste, e teste é atividade de verificação —, mas recebem alocação
própria por serem condição de aceitação.

### Marcos

| Marco | Data | Condição |
|---|---|---|
| **M-1** — Bloco *Must* concluído | 28/09 (fim de D8) | MVP funcional com cadastro e resolução de logo operantes, critérios de aceitação verificados |
| **M-2** — Bloco *Should* concluído | 30/09 (fim de D10) | Logo efetivamente aplicada ao vídeo, de forma assíncrona |

M-1 é o ponto de decisão do ciclo. Havendo atraso acumulado, a orientação é
deslocar PT-15 para o ciclo seguinte, e em nenhuma hipótese comprimir os
testes do bloco *Must*.

---

## 5. Acompanhamento do ritmo

A tabela abaixo é a linha de base do ciclo. Ao final de cada dia, preencher
as colunas de realizado e comparar com o previsto acumulado.

| Dia | PF previsto (acum.) | Horas previstas (acum.) | PF realizado | Horas realizadas | Desvio |
|---|---|---|---|---|---|
| D1 — 17/09 | 12 | 6 | | | |
| D2 — 18/09 | 12 | 12 | | | |
| D3 — 21/09 | 39 | 18 | | | |
| D4 — 22/09 | 46 | 24 | | | |
| D5 — 23/09 | 51 | 30 | | | |
| D6 — 24/09 | 58 | 36 | | | |
| D7 — 25/09 | 58 | 42 | | | |
| **D8 — 28/09 (M-1)** | **79** | **48** | | | |
| D9 — 29/09 | 79 | 54 | | | |
| **D10 — 30/09 (M-2)** | **86** | **59,5** | | | |

Os pontos de função são creditados na conclusão do pacote, e não
proporcionalmente ao andamento. Por isso D2, D7 e D9 não apresentam ganho
previsto: são dias em que um pacote atravessa a virada. Nesses dias, a
coluna de horas é o indicador confiável.

### Indicadores

| Indicador | Cálculo | Referência |
|---|---|---|
| **Aderência ao cronograma** | PF realizado ÷ PF previsto acumulado | ≥ 0,90 aceitável; < 0,80 exige ação |
| **Produtividade efetiva** | Horas realizadas ÷ PF realizado | Linha de base: 0,69 h/PF |
| **Ritmo necessário** | PF restante ÷ dias úteis restantes | Linha de base: 8,6 PF/dia |
| **Horas restantes para o Must** | 48 h menos horas realizadas em PT-01 a PT-14 | Deve chegar a zero em D8 |

### Regras de resposta a desvio

1. **Aderência entre 0,80 e 0,90:** manter escopo e recuperar comprimindo a
   margem de PT-15, que dispõe de dois dias para nove horas de trabalho.
2. **Aderência abaixo de 0,80 em qualquer dia até D5:** revisar a estimativa
   dos pacotes seguintes antes de prosseguir, pois o desvio provavelmente
   indica erro de dimensionamento e não apenas atraso.
3. **Aderência abaixo de 0,80 em M-1:** deslocar PT-15 integralmente para o
   ciclo seguinte e destinar D9 e D10 à conclusão do bloco *Must*.
4. **Produtividade efetiva acima de 1,00 h/PF de forma sustentada:**
   recalibrar o índice e replanejar o restante do ciclo, em vez de
   pressionar o cronograma.

O bloco *Must* não é objeto de redução de escopo em nenhuma hipótese. Sendo
inviável concluí-lo até 30/09, a resposta correta é estender o ciclo, não
reduzir seu conteúdo.

---

## 6. Frente B — Eletrônica

### Pré-requisitos

Componentes ainda não adquiridos: devkit ESP32 (placa `esp32dev`), módulo
Ethernet W5500 com interface SPI, perfboard, barras de pinos, conectores,
resistores de 10 kΩ para os pinos sem *pull-up* interno, cabo, botões, fonte
de 5 V sem negociação de carga rápida e conversor LM2596.

O devkit ESP32 possui conversor USB-serial integrado, o que dispensa o
adaptador USB-serial TTL externo e o aterramento manual de GPIO0 que o
WT32-ETH01 exigiria na primeira gravação.

### Pacotes

| ID | Pacote | Horas | Dia relativo |
|---|---|---|---|
| EL-01 | Inventário, conferência dos componentes, calibração do LM2596 sob carga | 4 | D+1 |
| EL-02 | Ensaio em protoboard: ESP32 com W5500 por SPI, firmware mínimo com um botão, validação contra `/health` | 6 | D+1 e D+2 |
| EL-03 | Firmware completo: oito botões, tratamento de repique, tempo limite de 15 s, identificador por pino, resposta a 429 | 6 | D+2 e D+3 |
| EL-04 | Montagem em perfboard: soldagem, *pull-ups* externos, terra comum, conectores, alimentação | 10 | D+3 a D+5 |
| EL-05 | Rede: IP fixo por `manual_ip`, reserva de DHCP, porta de switch, rota até o backend | 4 | D+5 |
| EL-06 | Ensaio de estabilidade de 24 h e validação em campo: latência, reconexão, acionamentos repetidos | 6 | D+6 e D+7 |
| EL-07 | Acondicionamento, instalação piloto, unidade de reposição, documentação do esquema | 6 | D+7 e D+8 |
| | **Total** | **42** | ≈ 8 dias de bancada |

### Pontos de atenção

EL-04 concentra o maior esforço e é irreversível na prática: erro de
soldagem em perfboard custa mais para corrigir do que para refazer. Não deve
ser iniciado antes de o firmware de EL-03 estar validado em protoboard.

EL-05 depende de quem administra a rede do local e é o principal candidato a
atraso externo. Recomenda-se iniciar a articulação da liberação de rota em
D+1, em paralelo à bancada, para que a dependência não se revele apenas em
D+5. A experiência do projeto de controle de cancela indica que mDNS não
resolve de forma confiável na rede dos locais, de modo que o endereçamento
fixo é requisito e não preferência.

---

## 7. Premissas

1. Dedicação de 6 horas produtivas por dia útil, sem trabalho em fins de
   semana.
2. Manutenção do escopo do `PLANO_DE_ACAO.md`, na opção A da seção 8 daquele
   documento. Novo requisito durante o ciclo implica remoção de item de
   prioridade equivalente.
3. Disponibilidade de ambiente com pelo menos uma câmera RTSP acessível para
   validação.
4. Definição da ordem de precedência entre esporte e local até o início de
   PT-14, em 25/09, para não gerar retrabalho de dados.
5. As estimativas da Frente B pressupõem entrega completa dos componentes;
   entrega parcial altera a sequência dos pacotes e motiva revisão.
