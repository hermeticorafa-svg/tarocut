# tarocut

Pré-editor dos vídeos do curso **Tarô Fora da Caixa**.

Corta pausas longas, cacoetes e tentativas repetidas de fala, e corrige o
volume. Roda inteiro na sua máquina: sem nuvem, sem API, sem créditos.
Depois de rodar uma vez, funciona até sem internet.

---

## A janela

Se não quiser digitar comando, dê **duplo clique em `tarocut.bat`**. Abre uma
janela com os vídeos da pasta, em que etapa cada um está, e um botão por
passo do fluxo abaixo — o log do que está rodando aparece embaixo, com barra
de progresso, e dá para cancelar no meio.

A janela não faz nada que o terminal não faça: ela chama os mesmos comandos.
O botão *Abrir transcript* abre o `.md` no seu editor de texto; o
*2. Conferir corte* roda a conferência de geometria do passo 2b; o
*3. Editor* abre a transcrição no navegador (passo 3) e fica no ar até você
fechar a aba — a janela continua usável enquanto isso.

Uma coisa fica de fora, porque não é dela: **o passe de julgamento** —
`/revisar` roda no Claude Code, numa janela nova (o porquê está no passo 2).
O botão *Copiar /revisar* põe o comando pronto na área de transferência, é só
colar lá.

As marcas na lista: `✎` transcript editado à mão, `★` anotações do Claude,
`✂` ajustado no editor, `♪` prévia em áudio já gerada, `▶` já renderizado.

---

## O fluxo

Atalho: `/preparar <video>` faz os passos 1, 2, 2b e 3b (áudio) de uma vez,
numa janela nova do Claude Code — sobra o `transcript.md` pronto pro seu
editor de texto. Veja `.claude/commands/preparar.md`. Os passos abaixo são
o mesmo fluxo, um a um, para quando quiser rodar (ou entender) cada um na
mão.

**1. Passada mecânica** — transcreve e propõe os cortes que dão para decidir
sozinho: pausas, alongamentos, gaguejadas, repetição literal.

```bash
cd C:\Users\marip\Videos\tarocut
venv\Scripts\python.exe tarocut.py run "C:\Users\marip\Videos\IMG_0247.MOV"
```

**2. Passe de julgamento** — o Claude lê a transcrição e acrescenta o que
nenhuma regra pega: interrupções, raciocínio refeito com outras palavras,
trechos em que ela se perde. Sai em `work/<video>/anotacoes.json`, que
sobrevive a re-análises: é uma vez por vídeo, para sempre.

**Rode numa janela nova do Claude Code, com o comando `/revisar`:**

```
/revisar C:\Users\marip\Videos\IMG_0247.MOV
```

Isso importa mais do que parece. Cada resposta do Claude relê a conversa
inteira, então o custo cresce com o tamanho dela, não com a dificuldade da
tarefa. O passe de julgamento precisa só da transcrição de um vídeo — feito
numa conversa longa, arrasta junto todo o histórico a cada resposta. Numa
janela limpa ele lê 1,3 mil tokens em vez de arrastar 170 mil.

O comando mora em `.claude/commands/revisar.md` — dá para ler e mudar o que
ele faz. Ele mesmo roda o `conferir` do passo 2b depois de escrever as
anotações e só termina quando nenhuma delas ficou para trás; o 2b abaixo é
a sua conferência, quando quiser olhar com os próprios olhos.

O `tarocut.py revisar` existe só para isso: escreve um `revisao.txt` que
colapsa o que já está cortado em marcadores de duração. Um vídeo de 10 min
cabe em ~1,3 mil tokens, contra 32 mil do `stream.json`.

**2b. Conferir** — logo depois do passe, sempre:

```bash
venv\Scripts\python.exe tarocut.py conferir "C:\Users\marip\Videos\IMG_0247.MOV"
```

São dois trabalhos diferentes, e por isso são dois comandos. O `/revisar`
decide **o que sai** — é julgamento de conteúdo, e só uma cabeça faz. O
`conferir` verifica **se saiu mesmo** — é geometria, e só a máquina vê. Um
não substitui o outro: já aconteceu de uma anotação certa ser anulada pelo
cálculo do corte e ninguém perceber até você ouvir.

Não gera nada: **olha o corte e reclama.** Em um segundo, lista só o que
merece o seu ouvido — trecho que você mandou cortar e continua tocando, fala
sobrevivendo dentro do trecho mantido, ilha de meio segundo entre dois
cortes, palavra raspada na borda. Cada linha vem com os dois tempos: o da
fonte, para anotar, e o da prévia, para achar de ouvido.

A regra é uma só: **nenhuma linha `ANOTACAO NAO OBEDECIDA`.** Se aparecer,
o que você mandou cortar não saiu — é bug, não gosto. O resto da lista é
sugestão de onde prestar atenção. Fica salva em `work/<video>/conferir.txt`,
na ordem em que você vai ouvir.

**3. Ajuste fino** — a transcrição no navegador, com o que sai riscado:

```bash
venv\Scripts\python.exe tarocut.py editor "C:\Users\marip\Videos\IMG_0247.MOV"
```

Abre uma página com o texto do vídeo inteiro, ipsis litteris, e **riscado**
tudo o que não vai sair no .mov — inclusive o que as anotações do passe de
julgamento tiraram. Não tem linha do tempo, não tem forma de onda: é o
texto.

Clique numa palavra e ela é selecionada; arraste para pegar um trecho.
Aparecem quatro botõezinhos, um par de cada lado: **o de menos esconde mais
um tanto daquele lado, o de mais devolve.** O tanto é a *unidade*, escolhida
no canto da tela entre 1 ms e 1 s (teclas `1` a `7`). O mais desfaz o menos
exato, então dá para tatear. `backspace` oculta a palavra inteira (e
restaura, se já estiver oculta); `ctrl+clique` põe o cursor entre duas
letras, para quando o corte tem de cair no meio de uma palavra.

**Botão direito no texto**: tocar a seleção, ou tocar dali em diante —
sempre o corte, nunca o original, com `espaço` pausando; numa palavra só
(nunca num trecho de várias), também **corrigir transcrição** — o Whisper
erra o texto às vezes, e essa correção é só ortografia: não muda quando a
palavra começa/termina nem se ela sai do vídeo, e serve para a legenda que
uma etapa futura vai gerar. Clique, apague/reescreva, `enter` grava (ou
clique fora); `esc` desiste. **Botão direito fora do texto**: exportar
áudio, exportar vídeo, conferir.

O que você faz aqui vai para `work/<video>/montagem.json` (o corte) e
`work/<video>/correcoes.json` (o texto), sozinho, a cada gesto — não há
botão de salvar. O `render` lê o primeiro, então o corte que sai é o que
você ouviu. `ctrl+z` desfaz os dois.

O botão **Abrir…**, no canto do painel, troca de vídeo sem passar pela
linha de comando — só lista o que já rodou `run`/`analyze` (tem
`stream.json` e `audio.wav` em `work/`), então nunca oferece um vídeo pela
metade.

**3b. Ouvir sem abrir o editor** — duas formas, nenhuma renderiza vídeo:

```bash
venv\Scripts\python.exe tarocut.py audio "C:\Users\marip\Videos\IMG_0247.MOV"
```

Gera um `.m4a` só com o áudio cortado, já normalizado. **~50 s para um vídeo
de 4 min, contra 5–6 min do render de vídeo**, e 3 MB em vez de 286 MB — o
gargalo do render é decodificar 4K60 HEVC 10 bits, e com `-vn` o ffmpeg nem
toca no vídeo. Para julgar corte, gaguejada e volume, ouvir basta.

```bash
venv\Scripts\python.exe tarocut.py previa "C:\Users\marip\Videos\IMG_0247.MOV"
```

Abre no mpv tocando direto do original, via EDL — instantâneo, e com imagem,
quando a dúvida for visual (pulo de cabeça na emenda).

Ouviu algo estranho e quer saber onde é na fonte? O `conferir` faz o caminho
de volta:

```bash
venv\Scripts\python.exe tarocut.py conferir "...IMG_0247.MOV" --em 3:05
```

Ajuste no editor (ou no `anotacoes.json`, ou no `transcript.md`), rode de
novo, repita até gostar. Só então renderize.

**4. Export**:

```bash
venv\Scripts\python.exe tarocut.py render "C:\Users\marip\Videos\IMG_0247.MOV"
```

Sai um `.mov` cortado, com o volume corrigido, pronto pro CapCut.

Os passos 2 e 3 são opcionais: sem eles, o render usa as propostas
automáticas.

---

## Como editar o transcript.md

O arquivo é a transcrição do vídeo com os cortes já propostos:

```
As rainhas elas fecham ou ⟦abrem...⟧ ⟦pausa 2.4s⟧ ⟦O que é isso, Juno?⟧
⟦pausa 6.4s⟧ As rainhas elas fecham ou abrem os naipes, dependendo de...
```

Três gestos, só:

| você quer | faça |
|---|---|
| cortar um trecho | **apague o texto** |
| cortar o que já veio marcado | **não faça nada** — `⟦ ⟧` já é corte |
| **manter** algo marcado | troque `⟦ ⟧` por `⟨ ⟩` |

Não mexa nas linhas `## [b001] 00:01:23 → 00:01:45`. São as âncoras de tempo.

O que aparece entre `⟦ ⟧` e não é palavra:

- `⟦pausa 4.3s⟧` — silêncio. Não é eliminado, é **encurtado** para 0,45s.
- `⟦hesitação · 0.8s⟧` — tem voz ali, mas o Whisper não transcreveu.
  Quase sempre é um "ééé" ou um "hmm".
- `⟦é... estra... · 2.3s⟧` — a hesitação foi recuperada em re-transcrição
  isolada, e é isso que ela disse.

No topo do arquivo há um **resumo** com quanto sai e por quê, e a lista das
**tentativas de fala** detectadas — para você já saber de antemão onde ela
começou a frase três vezes, sem descobrir isso editando.

---

## Os 34 vídeos

Transcrever é lento (~1/3 do tempo do vídeo). Rode em lote, de madrugada:

```bash
venv\Scripts\python.exe tarocut.py lote "C:\Users\marip\Videos" --ate draft
```

Isso deixa todos os `transcript.md` prontos. Você revisa quando quiser, e
renderiza um a um — ou tudo:

```bash
venv\Scripts\python.exe tarocut.py lote "C:\Users\marip\Videos" --ate render
```

Cada etapa tem cache: rodar de novo não retranscreve. Use `--force` para
refazer do zero.

---

## Ajustando o gosto

Tudo em `config.toml`. Os valores atuais foram **calibrados contra os vídeos
que você editou à mão** — medimos que você derruba a fatia de pausa longa de
~29% para ~7% e não deixa pausa passar de ~1,1s.

Se ficar agressivo demais, mexa aqui:

```toml
pausa_alvo      = 0.45   # pausa longa vira isto
pausa_intocavel = 0.75   # abaixo disto nem toca
```

O léxico fica em `lexicon/`:

- **`fillers.txt`** — cacoetes. Linha simples corta sempre; linha com `~`
  só corta quando a palavra está ilhada entre pausas. O `~` existe porque
  "é" é cacoete mas "e" é conjunção, e cortar a conjunção desmonta a frase.
- **`terms.txt`** — vocabulário do curso, para o Whisper acertar os nomes.
  Formato `Correto | erro1 | erro2`. Ainda incompleto: ele erra "naipes"
  (vira "snipes", "knipe"), "Tarô de Yala" (vira "tarot de ala") e os nomes
  das cartas. Alimentar isso com o livro do Yala melhora muito.

---

## Etapas, se precisar rodar separado

| comando | faz |
|---|---|
| `ingest` | lê o vídeo, extrai o áudio |
| `transcribe` | Whisper: o **texto** |
| `align` | alinhamento forçado: as **fronteiras** de palavra |
| `analyze` | cruza transcrição com energia do áudio, acha o que foi engolido, propõe cortes |
| `draft` | escreve o `transcript.md` |
| `revisar` | escreve o `revisao.txt` para o passe de julgamento |
| `conferir` | olha a geometria do corte e reclama, sem gerar nada |
| `audio` | corte só em áudio, para julgar de ouvido |
| `previa` | toca o corte no mpv, sem renderizar |
| `render` | aplica os cortes e normaliza o volume |
| `run` | ingest → draft |
| `lote` | tudo isso numa pasta inteira |

### Por que `align` é uma etapa separada

O Whisper acerta o texto e erra os tempos: medido neste material, **72% dos
fins de palavra caem depois do que ele diz** (mediana +27 ms, p90 +165 ms).
Cortar por esses tempos come o "s" final e parte palavra no meio — foi o que
produziu "momen-- meira versão" e um "lá de 20—" sem o ano.

O `align` passa um modelo wav2vec2 em português sobre o texto já conhecido e
descobre, quadro a quadro, onde cada letra foi pronunciada. Custa ~0,5x a
duração do vídeo e tem cache.

Um detalhe que importa: modelo CTC emite **pico**, não duração — a posição
sai exata e a palavra sai encolhida. Por isso, depois do alinhamento, cada
palavra reconquista por energia o que for contíguo e sonoro em volta do pico,
com teto. Sem esse teto a palavra engoliria o "ééé" seguinte; sem a expansão,
30s de fala real seriam confundidos com cacoete.

Útil: `render --dry-run` mostra quanto vai sair sem gastar tempo
renderizando.

---

## Por que é assim

O Whisper foi treinado para produzir texto limpo, e **apaga da transcrição
justamente os cacoetes que queremos cortar**. Medido: numa amostra de 3 min,
zero alongamentos no texto — mas 11,8s de voz com energia e nenhuma palavra
transcrita. Re-transcrevendo essas lacunas isoladas, apareceu `"É... Estra..."`,
`"é..."`, `"Zome..."`.

Por isso o `analyze` não confia só no texto: ele cruza a transcrição com a
energia do áudio e reinsere, como token sintético, aquilo que o Whisper
engoliu. É o que faz "apagar texto = apagar vídeo" funcionar de verdade.
