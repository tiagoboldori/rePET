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

**Estado atual (2026-10-16):** nove faixas do catálogo NCS (NoCopyrightSounds,
<https://ncs.io>), todas com 2–4 min, bem mais longas que qualquer clipe (~35s):
DJ PUMA – ALL BEFORE; Egzod, Lost Sky, Alaina Cross – Fight In Me; gabriawll,
Ada Petcu – Desire; Zack Griffin – Feel It; Silver Bullet – NOBODY ELSE; Syn
Cole, KAZHI – Waiting; jonty – fired up; e duas mais rock (gêneros Dance-Rock/
Electronic Rock do NCS): Laeko – DON'T WANNA KNOW; SlidV, Clarx – Severed Rose. Substituíram os dois dingles antigos.

As faixas **não são versionadas** (regra `/assets/music/*` no `.gitignore`):
cada ambiente baixa as suas em ncs.io (botão "Download" da página de cada
faixa). A política do NCS exige crédito de artista/música na descrição do
conteúdo publicado e restringe uso por marcas/serviços promovendo o próprio
negócio — ver <https://ncs.io/usage-policy>.
