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

**2. Passe de julgamento** — o Claude **entende a aula** e só depois corta.
Ele lê a transcrição inteira, procura no livro do Yala
(`material/guia-taro-de-yala.md`) o verbete da carta de que o vídeo trata,
escreve o que entendeu em `work/<video>/entendimento.md` — e é com isso na
cabeça que acrescenta o que nenhuma regra pega: interrupções, raciocínio
refeito com outras palavras, tentativa abandonada antes da frase que
vingou, bocejo que o transcritor leu como palavra. Sai em
`work/<video>/anotacoes.json`, que sobrevive a re-análises: é uma vez por
vídeo, para sempre.

Essa ordem — entender, depois cortar — não é formalidade. O que mais
escapava não tinha marca nenhuma do lado: um bocejo inteiro atravessou o 7
de espadas porque foi transcrito como uma palavra que não existe
("despeçoso") no meio de uma frase normal. Só desconfia disso quem sabe do
que o vídeo fala.

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
cabe em ~1,3 mil tokens, contra 32 mil do `stream.json`. O arquivo abre com
uma lista `## PENDENCIAS` numerada — cada item é uma coisa que o programa
sabe que não sabe resolver (um som que ninguém leu, uma leitura de baixa
confiança, uma frase que talvez seja tentativa abandonada). São poucas, 0 a
12 por vídeo, e o passe de julgamento tem de responder por todas.

**Responder é escrever.** A decisão vai em
`work/<video>/respostas.json` — `{"em": 223.35, "resposta": "fica",
"nota": "..."}`, com `em` copiado da linha `Pn`. Pendência cujo trecho sai
no corte já está respondida pelo próprio corte; a linha escrita existe
para o outro caso, o "olhei e deixei ficar", que não deixa marca em lugar
nenhum. Escrever `"sai"` aí **registra** a decisão e não corta nada — quem
corta é a anotação —, e se o trecho continuar tocando o `conferir` diz
`PENDENCIA NAO OBEDECIDA`. **O que sobrar sem resposta ele lista como
`PENDENCIA SEM RESPOSTA`, e o `audio`/`render` avisam antes de rodar** — dar o passe por
encerrado com uma pergunta em aberto dá; fazer isso em silêncio, não.

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

Abre uma página em duas metades. Em cima, o texto do vídeo inteiro, ipsis
litteris, e **riscado** tudo o que não vai sair no .mov — inclusive o que as
anotações do passe de julgamento tiraram. Embaixo, a **faixa de áudio**: a
mesma coisa dita em tempo, com a onda, os blocos de palavra e a régua nos
dois relógios (o da fonte e o do `.m4a` que você ouve).

As duas metades são a mesma coisa, e é esse o ponto: **selecionar uma
palavra acende o trecho dela na onda; arrastar na onda acende as palavras;
ocultar de um lado risca do outro.**

Quatro marcas, e cada uma responde uma pergunta diferente:

| marca | o que quer dizer |
|---|---|
| letra riscada em **cinza** | sai do vídeo, e quem mandou foi uma regra do programa |
| letra riscada em **azul** | sai do vídeo, e quem mandou foi você, no passe de julgamento — o motivo que você escreveu aparece ao pousar o mouse |
| tracinho **cinza contínuo** | silêncio ocultado ali; a largura cresce com o tanto |
| tracinho **pontilhado marrom** | **você falou e o Whisper não leu.** Aparece mesmo quando nada foi cortado — se estiver assim, aquela fala continua no vídeo sem uma letra na tela. Ao pousar o mouse: quanto é, quanto sobra, e o que o Whisper chegou a ouvir antes de o programa recusar a leitura |

No pontilhado marrom vale o **botão direito → escrever o que se ouve**:
onde há fala e não há texto, você digita o que ouviu. Quando a recusa foi
por dúvida do Whisper, a caixa já abre com o palpite dele para você
confirmar ou corrigir. Isso não muda um milissegundo do corte — é texto,
para a legenda que uma etapa futura vai gerar.

Clique numa palavra e ela é selecionada; arraste para pegar um trecho.
`backspace` oculta a seleção — e **oculta mesmo**: a palavra some do áudio,
inclusive as curtas ("de", "que", "e"), que antes o programa avisava não ter
conseguido tirar. `backspace` de novo restaura. As setas andam pelo texto,
`shift`+setas estende.

Na faixa de áudio, três gestos e nenhum botão:

| você quer | faça |
|---|---|
| tirar um trecho | **arraste sobre a região limpa** (a que ainda toca) |
| devolver um trecho | **arraste sobre a região hachurada** (a que já saiu) |
| mexer numa emenda | **arraste a linha vertical** onde uma encontra a outra |

A regra do começo vale para o arrasto inteiro: começou no que toca, tudo o
que ele cobriu sai; começou no que já saiu, tudo o que ele cobriu volta.
Arrastar de volta por cima desfaz exato. A roda do mouse dá zoom, arrastar a
régua anda pelo vídeo, e clicar num bloco de palavra seleciona aquela
palavra — igual a clicar nela no texto.

Nada disso é calculado no navegador: cada gesto vai ao servidor e volta com
o corte já feito pela mesma função que o `render` usa. Enquanto o dedo anda,
a tela mostra um fantasma; quando a resposta chega, quem manda é ela.

**Botão direito no texto**: tocar a seleção, ou tocar dali em diante — o
corte, com `espaço` pausando — e **tocar o original (sem o corte)**, que é
a tecla `o` e a única coisa da tela que ignora a montagem: toca a seleção
com 0,35 s de folga dos dois lados, para você ouvir o que a lâmina levou e
como ficou a emenda. Numa palavra só
(nunca num trecho de várias), também **corrigir transcrição** — o Whisper
erra o texto às vezes, e essa correção é só ortografia: não muda quando a
palavra começa/termina nem se ela sai do vídeo, e serve para a legenda que
uma etapa futura vai gerar. Clique, apague/reescreva, `enter` grava (ou
clique fora); `esc` desiste. **Botão direito fora do texto**: exportar
áudio, exportar vídeo, conferir.

No **canto superior direito** ficam a caixa "ir para" — digite `1:29`, o
tempo que você anotou ouvindo o `.m4a`, e a tela vai até a palavra — e um
botão com a conta do que merece ouvido (`⚠ 37`). Ele abre o painel: o que
você mandou e não saiu, o que o `conferir` acharia neste corte agora, o que
a geometria não deu conta e o programa completou no tempo exato, e os botões
de exportar. Fica escondido de propósito; a **conta** não, e fica vermelha
quando há pedido desobedecido ou arquivo desatualizado.

O que você faz aqui vai para `work/<video>/montagem.json` (o corte) e
`work/<video>/correcoes.json` (o texto), sozinho, a cada gesto — não há
botão de salvar. O `render` lê o primeiro, então o corte que sai é o que
você ouviu. `ctrl+z` desfaz os dois.

**O painel do canto responde três coisas.** *O que ouvir* é a mesma lista
do `conferir`, calculada pela mesma função, agora sem sair da tela e
atualizada a cada gesto: pendência sem resposta, anotação que não saiu,
fala marcada para sair que continua tocando, ilha curta, palavra raspada,
repetição que o próprio corte criou. Cada linha corre no relógio da
**prévia** — a ordem em que você vai ouvir — e leva você até a palavra com
um clique. *Ir para* faz o caminho inverso: você digita `1:29`, o tempo que
anotou ouvindo o `.m4a`, e a tela vai até a palavra que soa ali. E se o
`.m4a` do disco tiver deixado de ser este corte — basta um gesto seu para
isso —, o painel avisa em vermelho, porque a partir daí os tempos que você
anotou apontam para outro lugar.

As setas `←` `→` andam palavra a palavra e **param também nos pontilhados
marrons**, que é onde você escreve o que ouviu.

O botão **Abrir…**, no canto do painel, troca de vídeo sem passar pela
linha de comando — só lista o que já rodou `run`/`analyze` (tem
`stream.json` e `audio.wav` em `work/`), então nunca oferece um vídeo pela
metade.

**3b. Ouvir sem abrir o editor** — duas formas, nenhuma renderiza vídeo:

```bash
venv\Scripts\python.exe tarocut.py audio "C:\Users\marip\Videos\IMG_0247.MOV"
```

Gera um `.m4a` só com o áudio cortado, já normalizado. **~50 s para um vídeo
de 4 min, contra alguns minutos do render de vídeo** (varia com a máquina e
o que mais está rodando nela — o `render` usa a GPU Intel por padrão), e
3 MB em vez de 286 MB. Com `-vn` o ffmpeg nem toca no vídeo, então o `audio`
não sente o gargalo de decodificar 4K60 HEVC 10 bits que o `render` sente.
Para julgar corte, gaguejada e volume, ouvir basta.

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

**3c. Mandar o que você ouviu de volta pro programa** — em vez de traduzir
os tempos na mão, anote enquanto ouve, com o relógio do player, e cole no
`/anotar`:

```
[00:15] aqui há uma gaguejada "mas mas mas..."
[01:29] essa frase é uma tentativa malsucedida, a correta aparece em [01:45]
```

```bash
/anotar "7 de espadas_previa.m4a"
```

Numa janela nova do Claude Code: ele confirma que o `.m4a` ainda bate com o
corte de agora, pede as anotações, traduz cada tempo de volta para a fonte,
põe cada correção na camada certa (anotação, `montagem.json`,
`correcoes.json`) e gera o áudio novo. E faz uma segunda pergunta, que é
metade do motivo de ele existir: **alguma dessas coisas o programa podia
ter pego sozinho?** Quando podia, ele propõe a mudança em português de
gente e espera você aprovar. Veja `.claude/commands/anotar.md`.

Ajuste no editor (ou no `anotacoes.json`, ou no `transcript.md`), rode de
novo, repita até gostar. Só então renderize.

Três linhas do `conferir` valem uma palavra à parte, porque o conserto
delas não é anotação: `ARRASTO SOBREVIVE` é o rabo de uma palavra
arrastada que o corte não levou (a poda era curta demais para virar
emenda), e o lugar de apará-lo é o editor, arrastando na onda.
`PENDENCIA SEM RESPOSTA` não é defeito de corte nenhum — é uma pergunta
do `revisao.txt` esperando decisão. E `PALAVRA ENTERRADA` é o alinhador
errando, não o corte: pelo relógio do Whisper — a única segunda opinião
que existe aqui — uma palavra que era para ficar está dentro do buraco,
longe da emenda. Ouça e conserte a borda no editor; mexer na anotação não
resolve, porque a anotação está ancorada num tempo que a palavra não
ocupa.

**4. As cartas e a legenda** — no editor, o botão **cartas** parte a tela ao
meio. À esquerda, o texto que **sobrou** do corte, uma linha por legenda,
com o tempo do vídeo pronto e um ▶ para ouvir só aquele trecho. À direita,
o roteiro das cartas.

O texto da esquerda é editável: é dele que sai o `.srt` que acompanha o
vídeo. Ele já vem escrito com o que se ouve; corrija o que quiser, aperte
enter, e a correção fica azul (a mesma cor que a folha usa para "isto foi
uma pessoa"). Apagar tudo devolve o texto automático. Os tempos não se
editam — eles são o corte.

À direita, marque **este vídeo é sobre uma carta** e escolha a carta: ela
entra à esquerda aos 3 s e fica até 10 s antes do fim (os últimos 10 s são
só você falando). Abaixo, uma carta por linha:

```
[1:29] Rainha de Copas; direita; 12s
```

O nome da carta tem autocomplete com as 78 do baralho — as mesmas de que
sai a imagem, então **não dá para errar o nome**. `esquerda`/`direita` também.
O lado e o tempo são opcionais: sem tempo, a carta fica até aqueles 10 s
finais.

- **carta à direita** = a Mariana vai para o centro, com uma carta de cada
  lado. O enquadramento sai do rastreio de rosto, e o rodapé diz o zoom que
  ele escolheu e quanto do tempo o rosto passa atrás de uma carta.
- **carta do mesmo lado com outra em cena** = empilha sobre ela, e desce
  sozinha quando a de baixo sai.

**Foto ou gráfico que não é carta do baralho** entra pela pasta `midias/`
(mesmo autocomplete, mesma garantia — só entra o que estiver lá dentro).
Duas formas de linha:

```
[2:10] grafico.png; 12s
[3:02] mapa-astral.jpg; full; 8s
```

Sem `full`, a mídia entra na mesma pilha da direita, do mesmo tamanho de
uma carta — mas nunca aceita `esquerda`/`direita`, ela só vai para a
direita. Com `full`, ela ocupa o centro da tela quase inteira (`Xs` é
obrigatório aqui) e **toda carta ou mídia que estiver em cena some com um
fade de 2s antes dela aparecer, e volta com outro fade de 2s depois que
ela desaparece** — é um recurso raro, para quando você precisa mostrar uma
imagem que não é carta.

**Clicar num tempo da esquerda escreve a linha na direita**, já com o
colchete, numa quebra nova — daí é só digitar o nome da carta. Os dois
lados falam o mesmo relógio (o do vídeo pronto), então não há tradução no
meio.

Linha que o programa não entende sai em vermelho, na linha, e o export
recusa até você consertar — meio roteiro renderizado calado seria pior. E
cada carta aceita aparece no rodapé com **o que se ouve naquele instante**,
entre aspas: se você mexer no corte depois de escrever o roteiro, é aí que
o escorregão aparece.

Dois botões exportam, e os dois escrevem o `.srt` junto (desmarque `.srt
junto` se não quiser):

- **prévia lo-res** — 360p/15fps com o **áudio final**. Minutos, e é ela que
  se olha para aprovar a dança das cartas.
- **vídeo final** — resolução do original, o mesmo áudio, as cartas
  bakeadas. Sai ao lado do arquivo de origem, como `<vídeo>_final.mov`.

Pela linha de comando é o mesmo:

```bash
venv\Scripts\python.exe tarocut.py exportar "C:\Users\marip\Videos\IMG_0247.MOV" --previa
```

**5. Export sem carta nenhuma** — se o vídeo não leva carta:


```bash
venv\Scripts\python.exe tarocut.py render "C:\Users\marip\Videos\IMG_0247.MOV"
```

Sai um `.mov` cortado, com o volume corrigido, pronto pro CapCut. (O
`exportar` faz a mesma coisa quando o roteiro de cartas esta vazio, e
ainda escreve o `.srt`.)

Os passos 2, 3 e 4 são opcionais: sem eles, o render usa as propostas
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

As cartas e a legenda têm blocos próprios, e nenhum deles é medição contra
vídeo editado à mão — são o que você pediu, e o comentário de cada um diz
isso:

```toml
[cartas]
principal_entra = 3.0    # quando a carta principal entra
rabo_final      = 10.0   # os últimos N segundos são sem carta
carta_altura    = 0.85   # fração da altura do quadro; aperta a faixa
                         # livre do meio, e o enquadramento responde sozinho
[cartas.midia]
max_largura     = 0.98   # o quanto uma imagem 'full' pode cobrir do quadro
fade_cartas     = 2.0    # quanto tempo as cartas levam para sumir/voltar
                         # em volta de uma imagem 'full'
[legenda]
max_linha       = 42     # largura de uma linha do .srt
max_segundos    = 6.0    # um cartão não fica mais que isto na tela
```

Se o render final com cartas ficar lento (é o `overlay` em 4K que custa),
o botão é `[render].perfil = "1080p30"`: ~4x mais rápido, e ainda deixa a
carta mais nítida, porque em 1080p ela é reduzida em vez de ampliada.

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
