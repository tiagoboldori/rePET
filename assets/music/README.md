# Música de fundo dos clipes

Qualquer arquivo `.mp3`, `.m4a`, `.aac`, `.wav`, `.ogg` ou `.opus` colocado
nesta pasta é candidato a trilha de fundo dos clipes enviados ao Lara (ver
`integrations/audio.py` e README, seção "Integração com o Lara").

- **Pasta vazia = sem música** (no-op silencioso).
- **Um único arquivo = uso fixo** (esse arquivo é sempre escolhido).
- **Mais de um arquivo = sorteio aleatório** por clipe, a cada envio — já
  funciona hoje sem precisar mexer em código, é só adicionar mais faixas
  aqui.

A faixa sorteada é cortada **a partir do início dela** (0s até a duração
do clipe — ex.: clipe de 30s usa exatamente os primeiros 30s da faixa),
com loop só como fallback se a faixa for mais curta que o clipe, mais
fade in/out e volume fixo (`MUSIC_VOLUME`, `MUSIC_FADE_SECONDS` em
`.env`/`api/config.py`). Ver `integrations/audio.py`.

**Estado atual (2026-09-22):** duas faixas reais em uso — `clubix
dingle.mp3` (162s) e `rePET dingle.mp3` (228s), ambas bem mais longas que
qualquer clipe (~35s), então o sorteio hoje sempre pega os primeiros ~35s
de uma das duas.

Por padrão, arquivos de áudio novos **não são versionados** aqui (ver
regra em `.gitignore`, `/assets/music/*`) — cada ambiente adiciona os
seus localmente, evitando inflar um repositório público com mídia
grande/com licenciamento a confirmar. As duas faixas acima são uma
exceção deliberada: foram adicionadas direto pela interface web do
GitHub (que não respeita `.gitignore` local) a pedido do responsável, e
por isso ficam versionadas normalmente a partir de agora — só arquivos
*novos* adicionados localmente continuam exigindo `git add -f` para
entrar no repo.
