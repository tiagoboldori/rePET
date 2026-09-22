# Música de fundo dos clipes

Qualquer arquivo `.mp3`, `.m4a`, `.aac`, `.wav`, `.ogg` ou `.opus` colocado
nesta pasta é candidato a trilha de fundo dos clipes enviados ao Lara (ver
`integrations/audio.py` e README, seção "Integração com o Lara").

- **Pasta vazia = sem música** (comportamento atual, no-op silencioso).
- **Um único arquivo = uso fixo** (esse arquivo é sempre escolhido).
- **Mais de um arquivo = sorteio aleatório** por clipe, a cada envio — já
  funciona hoje sem precisar mexer em código, é só adicionar mais faixas
  aqui.

A faixa é cortada (com loop se for mais curta que o clipe) pra exatamente
a duração do clipe, com fade in/out e volume fixo (`MUSIC_VOLUME`,
`MUSIC_FADE_SECONDS` em `.env`/`api/config.py`).

Os arquivos de áudio em si **não são versionados** (ver `.gitignore`) —
por licenciamento/tamanho, cada ambiente adiciona os seus localmente.
