# Padrões de edição do curso — medidos

Base: os três vídeos já editados que estavam em `Videos/Curso editado`
(`Introdução 0 - Que negócio é esse de tarô`, `Ouros - 3`, `Ouros - 9`),
13,9 min no total. Medido em 31/08/2026 com ffmpeg + análise de energia,
detecção de emenda quadro a quadro e leitura de frames.

**Limite desta medição:** não temos os brutos *destes* três vídeos. Dá para
medir o que **sobreviveu** à edição, não o que ela cortou. Para o gabarito
completo falta um par bruto+editado da mesma aula.

---

## 1. Formato

| | bruto (IMG_02xx) | editado (saída do CapCut) |
|---|---|---|
| resolução | 3840×2160 | 1920×1080 |
| fps | 59,94 | 60 |
| codec | HEVC 10-bit, HLG (`arib-std-b67`, BT.2020) | idem |
| bitrate | ~61 Mbps | ~18 Mbps |
| áudio | — | AAC 44,1 kHz estéreo |

O CapCut reduz 4K → 1080p **no final**. É isso que dá a ela margem de
enquadrar: um punch-in de 1,5× num 4K entregue em 1080p ainda é nativo.

> **Para o tarocut:** `perfil = "igual"` está certo e não deve mudar.
> Entregar 1080p pré-cortado destruiria a margem de zoom que ela usa.

---

## 2. Áudio: os vídeos publicados NÃO são normalizados

| vídeo | LUFS integrado | pico real | LRA |
|---|---|---|---|
| Introdução 0 | −30,0 | −7,5 dBFS | 6,7 LU |
| Ouros - 3 | −32,3 | −12,5 dBFS | 5,5 LU |
| Ouros - 9 | −31,9 | −12,1 dBFS | 5,5 LU |

Referência de plataforma de curso/streaming: −16 a −14 LUFS. As aulas dela
estão **15 dB abaixo** disso, e consistentes entre si — ou seja, é o nível
cru da câmera passando direto pelo CapCut.

Não há música de fundo em nenhum dos três (espectrograma nas pausas mostra
só piso de sala; nada tonal, nada com pulso). O piso de ruído varia por
gravação: −70 dBFS nos "Ouros", −58 dBFS na "Introdução".

> **Decisão pendente:** nosso `lufs_alvo = -16.0` faz as aulas novas saírem
> ~15 dB mais altas que as já publicadas. Ou a gente aceita a quebra (e
> considera re-renderizar as antigas), ou baixa o alvo. Recomendo manter
> −16 e corrigir o acervo depois: −31 LUFS é baixo demais para aula.

---

## 3. Pausas — o gabarito de corte

Silêncio medido por energia (12 dB acima do piso), pausas internas apenas.

| | Introdução 0 | Ouros - 3 | Ouros - 9 | brutos |
|---|---|---|---|---|
| voz | 70,0% | 81,6% | 83,3% | — |
| tempo em pausa | 29,7% | 17,6% | 16,2% | — |
| p90 da pausa | 0,32s | 0,69s | 0,52s | — |
| p95 da pausa | 0,50s | 0,83s | 0,71s | — |
| maior pausa | 1,84s | 2,26s | 1,32s | 5,5–11,5s |
| pausas > 0,6s | 6,1% do tempo | 8,3% | 5,4% | 25–33% |
| pausas > 1,0s | 5 | 4 | 4 | — |

Dois fatos que mudam a régua:

1. **Ela não zera pausa longa.** Cada vídeo mantém 4 a 5 pausas acima de
   1s, e uma acima de 1,8s. São respiros retóricos, em fim de raciocínio.
   Nosso `pausa_intocavel = 0.75` encurta todas elas para 0,45s.
2. **O piso dela é ~0,45s.** O silêncio que sobra em volta de uma emenda
   tem mediana 0,28–0,45s. É exatamente o `pausa_alvo` que já está no
   config — calibração confirmada por um caminho independente.

> **Para o tarocut:** manter `pausa_alvo = 0.45`. Acrescentar exceção para
> pausa que vem **depois de fim de frase**: até ~1,2s pode ficar. Hoje o
> programa achata o respiro que ela deliberadamente mantém.

---

## 4. Ritmo de corte — ela corta MUITO menos do que a gente

Emendas detectadas por descontinuidade de quadro na região dela (a faixa
da legenda e a da carta foram mascaradas, senão a legenda vira "corte").

| | Introdução 0 | Ouros - 3 | Ouros - 9 |
|---|---|---|---|
| emendas | 40 | 9 | 34 |
| por minuto | **6,6** | **4,7** | **5,8** |
| mediana entre emendas | 8,1s | 12,4s | 8,1s |
| maior trecho sem emenda | 24,7s | 21,3s | 28,2s |

Onde a emenda cai, no áudio:

| | em silêncio absoluto (±0,08s) | em cima de fala |
|---|---|---|
| Introdução 0 | 19/40 | 4/40 |
| Ouros - 3 | 6/9 | 0/9 |
| Ouros - 9 | 17/33 | 2/33 |

**Nenhuma emenda no meio de palavra.** As poucas que caem sobre fala são
mudanças de enquadramento (item 6), não remoção.

> **Este é o achado mais importante.** Nos brutos, pausa > 0,6s ocupa
> 25–33% do tempo. Se o tarocut encurtar toda pausa acima de 0,75s, ele
> emenda algo como 20–40 vezes por minuto — 4 a 8× o ritmo dela. O corte
> fica tecnicamente correto e **visivelmente** diferente do curso.
>
> Proposta: **orçamento de emendas**. Ordenar os candidatos por ganho
> (segundos economizados, disfluência removida) e aplicar até bater um
> teto — algo como 8/min — ou até a fatia de pausa cair para ~17%, que é
> onde os vídeos dela vivem. O resto fica.

---

## 5. O que sobrevive na fala

Transcrição do vídeo **já editado** (Whisper small, pt), `Ouros - 3`:

- **149 palavras/min** — ritmo de aula, não de podcast.
- **zero** hesitações puras ("ah", "eh", "hum") no texto.
- **zero** repetições imediatas de palavra.
- marcadores de discurso sobrevivem, em dose baixa: `então` 1,6/min,
  `assim` 1,1/min, `a gente` 0,5/min. Ela não limpa a fala até virar
  locução — o jeito de falar dela fica.
- **3,1 s por minuto de voz sem texto sobrevivem**: trechos com energia de
  voz e nenhuma palavra transcrita, o maior deles com 1,04s. Parte é
  hesitação que ela deixou passar de propósito, parte é palavra que o
  Whisper comeu.

Ressalva: o Whisper apaga disfluência por projeto (ver
`memory/whisper-apaga-disfluencias`), então "zero hesitação no texto" é
evidência fraca sozinha. O número que vale é o de voz-sem-texto acima,
que vem da energia do áudio e não do texto.

> **Para o tarocut:** 3,1 s/min de voz-sem-texto é o **teto** que ela
> tolera, não um erro a zerar. O programa não precisa caçar toda hesitação;
> precisa caçar as longas.


---

## 6. Padrões visuais

Tudo isto é trabalho de CapCut, não do tarocut — mas define o que o corte
que a gente entrega precisa aguentar.

**Cenário.** Sempre o mesmo: parede branca com faixa amarela, espada-de-são-
jorge à esquerda, mesa redonda de madeira com o baralho e o pano. Lapela na
alça. Ela sentada, ligeiramente à direita do centro.

**Dois enquadramentos.** Um base, aberto, e um punch-in de **1,3× a 1,5×**
(medido por correlação em escala). O fechado dura de 5 a 16s e serve
ênfase. Ele entra e sai **no meio da fala** — é reframe, não remoção. São
2 a 4 por vídeo.

**Overlay da carta.** Arte da carta na faixa esquerda, ~55% da altura, com
etiqueta serifada embaixo (`3 de Ouros`, `9 de Ouros`, `6 de Espadas`).

- Em aula de carta (`Ouros - 3`): na tela **82% do tempo**. Entra animando
  em ~2,0–2,4s; sai ~1,8s antes do fim.
- **Some durante o punch-in** e volta quando o enquadramento abre. Em
  `Ouros - 3` isso é exato: sem carta de 29,0s a 45,5s, que é precisamente
  o trecho fechado.
- Na `Introdução 0` (aula sem carta fixa) o overlay só aparece quando ela
  cita uma carta específica — `6 de Espadas` por volta de 3:25 a 3:45.

**Legendas.** Automáticas, estilo CapCut: caixa alta, fonte condensada
branca com contorno preto, 1–2 linhas, centro-baixo. A palavra corrente
ganha caixa **roxa**. A troca de destaque acontece a cada **0,45–0,60s**
(70–116 por minuto) — é destaque palavra a palavra, não legenda por frase.

**Abre e fecha frio.** Sem vinheta, sem card de fim, sem música.
`Ouros - 9` abre com 5s dela mexendo no baralho, em silêncio; todos fecham
com 1–2s de sobra depois da última palavra, já sem legenda e sem carta.

> **Para o tarocut:** dois desdobramentos possíveis.
> 1. **Não cortar os respiros de abertura e fechamento.** O silêncio inicial
>    dela é conteúdo — hoje entraria na régua de pausa longa e sumiria.
> 2. **Marcar emenda "cara".** Onde a posição da cabeça muda muito entre o
>    fim e o começo do corte, a emenda salta. Dá para medir isso no render
>    (diferença de quadro através da emenda) e sair uma lista: *nestes
>    pontos, vale um punch-in no CapCut para esconder a emenda* — que é
>    exatamente o que ela já faz à mão.

---

## 7. O que muda no programa

Por ordem de impacto:

1. **Orçamento de emendas** (item 4). Hoje o corte é "toda pausa acima do
   limiar"; o gosto dela é "as piores pausas, umas 6 por minuto". Sem isso,
   o resultado é correto e não parece com o curso.
2. **Respiro retórico** (item 3). Não achatar pausa que vem depois de fim
   de frase até ~1,2s.
3. **Abertura e fechamento intocáveis** (item 6). Os primeiros ~5s e os
   últimos ~2s não entram na régua de pausa.
4. **Decidir o LUFS** (item 2).
5. **Lista de emendas caras** (item 6), como saída extra do render.
