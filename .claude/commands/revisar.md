---
description: Passe de julgamento de um vídeo do tarocut - entende a aula, lê a transcrição, escreve anotacoes.json e confere que o corte obedeceu
argument-hint: <caminho do vídeo ou nome curto, ex. IMG_0247>
allowed-tools: Bash(venv/Scripts/python.exe:*), Bash(grep:*), Read, Write, Edit
---

Faça o passe de julgamento do vídeo: **$ARGUMENTS**

Você é a única parte deste programa que lê o que ela DISSE. Todo o resto
decide por regra e por energia de áudio. O que você acrescenta é o que
nenhuma heurística pega: interrupção, raciocínio refeito com outras
palavras, trecho em que ela se perde, som que não é fala nenhuma e passou
por palavra.

**O que "pronto" quer dizer aqui.** O produto deste passe é um áudio que o
Rafael possa ouvir do começo ao fim sem tropeçar, e um `transcript.md` em
que o que sobrou para o editor de milissegundo seja *só* milissegundo. Um
bocejo que sobrevive, uma tentativa abandonada que fica junto da boa, uma
frase que não fecha: cada um desses é trabalho manual que este passe devia
ter poupado. É por isso que a fase 1 existe.

## A regra que manda em todas as outras

**Compreensão vem antes de corte.** Este passe não é uma caça a marcadores.
Marcador aponta para onde *o programa* desconfia; ele não sabe do que a
aula trata, e portanto não desconfia justamente do que só faz sentido para
quem sabe. No 7 de espadas um bocejo inteiro (quem ouviu descreveu como "ai
que horror", bocejado) foi transcrito como a palavra inexistente
"despeçoso", ficou no meio de "das pessoas [despeçoso] envolvidas na
questão" e atravessou um passe de julgamento inteiro — não havia marca
nenhuma do lado dele, e não precisava haver: o texto sozinho já não fazia
sentido para quem soubesse do que aquele vídeo falava.

## A economia deste comando

Este comando existe para rodar numa janela limpa. Cada resposta relê a
conversa inteira, então **leia poucos arquivos, e nenhum grande**:

- `work/<video>/revisao.txt` — a transcrição compactada. É a sua fonte.
- `work/<video>/conferir.txt` — o resultado da geometria, na fase 4.
- **um trecho** de `material/guia-taro-de-yala.md` — só a seção da carta de
  que a aula trata (ver fase 1). Nunca o arquivo inteiro (67 mil caracteres).
- `work/<video>/words.json` — só janelas dele, com `-c`, para ancorar borda
  (ver fase 3). Nunca inteiro.

Não abra `stream.json` (32 mil tokens contra ~1,3 mil do `revisao.txt`),
não abra `transcript.md`, não abra `tarocut.py`. Se sentir falta de algo
que só o código explica, isso é sinal de que você está tentando consertar
um bug — veja a fase 5.

Rode tudo da pasta do projeto, com o Python do venv. O nome curto
(`IMG_0247`) funciona tanto quanto o caminho completo.

---

## Fase 1 — entender a aula ANTES de julgar (obrigatória)

```
venv\Scripts\python.exe tarocut.py revisar $ARGUMENTS
```

### 1a. Leia o `revisao.txt` inteiro, uma vez, sem anotar nada

Do início ao fim. Não pare em marcador, não abra `words.json`, não escreva
anotação. Você está aqui só para saber do que a aula trata.

O cabeçalho explica as marcas. As que mais importam:

- `## PENDENCIAS` — a lista numerada do que o programa **sabe que não sabe
  resolver**. Cada `Pn` precisa de uma resposta sua, e a resposta vai em
  **dois** lugares: `work/<video>/respostas.json` (fase 3) e o relatório
  (fase 5). Elas aparecem também ancoradas no texto como `[P7]`, para você
  ver cada uma no contexto em que caiu. São poucas — 0 a 12 por vídeo — e
  o cabeçalho do bloco diz quantas ainda estão sem resposta.
- `[rec: ...]` — **texto reconstruído**: não saiu da transcrição do vídeo.
  O Whisper não leu nada ali e o programa re-transcreveu o pedaço isolado.
  É a leitura de menor confiança do arquivo, e é onde bocejo, resmungo e eco
  de microfone se disfarçam de palavra. Quando a pendência disser
  `<== X nao existe no material do curso`, a leitura é quase certamente
  invenção — mas a ausência desse aviso **não** é atestado de que a leitura
  presta: "Disse percebe" tem duas palavras reais e não quer dizer nada.
- `[-N.Ns]` / `[-N.Ns VOZ]` — já marcado para corte; o `VOZ` avisa que há
  som alto lá dentro. Confira que não é frase inteira prestes a sumir.
- `palavra~` — o tempo dessa palavra **não é confiável**; o alinhador
  recusou o próprio resultado. Ela pode estar segundos longe de onde
  aparece. **Nunca ancore borda de corte numa palavra com `~`.** Se o
  trecho que você quer cortar começa ou termina numa delas, ancore na
  palavra limpa mais próxima (fase 3).

### 1b. Descubra de que carta a aula trata e leia o que o livro diz dela

Quase todo vídeo deste curso é sobre uma carta (o nome do arquivo costuma
dizer: `7 de espadas`, `as de espadas`). Ache a seção dela no guia:

```
grep -n "^7 de Espadas" material/guia-taro-de-yala.md
```

e leia essa seção com `Read` (`offset` na linha achada, `limit` de 6 a 20
linhas). O guia é organizado assim: `ARCANOS MAIORES` com uma seção por
carta terminando em `PALAVRAS-CHAVE:`, e `ARCANOS MENORES` divididos em
`COPAS`, `PAUS`, `ESPADAS`, `OUROS`, cada carta num parágrafo que começa
com o nome dela e um `:`.

Se o vídeo enumera várias cartas ou fala de um tipo (as rainhas, os
arcanos menores), leia o cabeçalho do naipe e as duas ou três cartas mais
citadas — não o livro todo.

**Por que isto não é enfeite.** O verbete do 7 de Espadas diz "astúcia,
tato, diplomacia e esperteza" e "uma mulher Guna negocia com o homem
branco". Com essas duas linhas na cabeça, "pessoa cuna", "kunayala" e
"Samblas, Panamá" se confirmam como fala boa na hora, e "ai que horror"
lido como "despeçoso" fica órfão no parágrafo. Sem elas, as duas coisas
parecem igualmente estranhas — e o revisor passa batido nas duas, ou pior,
corta a boa.

### 1c. Escreva `work/<video>/entendimento.md`

Curto — 15 a 30 linhas. Sem enfeite, é ferramenta de trabalho:

```markdown
# <video> — o que esta aula diz

**Carta:** 7 de Espadas
**O que o guia diz:** astúcia, tato, diplomacia, esperteza; desconforto de
falsidade; planos irreais. Imagem: mulher Guna negociando com o homem branco.

**Tese da aula:** <uma frase: o que ela quer que a pessoa entenda>

**Roteiro:**
1. 0-20s — abre com o nome da carta e o tema (astúcia/esperteza)
2. 21-63s — quem é a pessoa da imagem: povo Guna, Kuna Yala, San Blas
3. ...

**O que esta aula NÃO é sobre:** <2 ou 3 assuntos vizinhos que ela não
toca — serve de filtro: frase que cai fora disso é suspeita>
```

Este arquivo é para você, agora, e para quem reler o `anotacoes.json` daqui
a seis meses. **Escrevê-lo é o que separa ter lido de ter entendido** — a
versão antiga deste comando pedia o resumo "de cabeça, não precisa
escrever", e foi assim que a fase 2 virou caça a marcador isolado.

---

## Fase 2 — julgar

Duas varreduras. **As duas, sempre**, e nesta ordem.

### 2a. Varredura das pendências (fechada)

Percorra `P1`, `P2`, ... `Pn` na ordem. Para cada uma, três coisas:

1. Ache a `[Pn]` no texto corrido e leia a frase inteira em volta.
2. Pergunte, com o `entendimento.md` na cabeça: **isso faz sentido nesta
   aula?** Não "é uma palavra em português?", mas "é uma coisa que ela
   diria aqui, agora, sobre esta carta?".
3. Decida: **sai**, **fica**, ou **vale ouvido**. Registre a decisão — a
   fase 3 pede uma linha no `respostas.json` e a fase 5, uma no relatório.

Pode agrupar o que se decide junto (`{"em": [223.35, 270.45], "resposta":
"fica"}`, e "P2 e P3: som alto que é ela respirando, ficam" no relatório).
O que não pode é sobrar pendência sem resposta — e agora **isso não é só
uma regra deste arquivo**: o `conferir` lista cada uma como `PENDENCIA SEM
RESPOSTA` e o `audio`/`render` avisam antes de gastar minutos. Terminar sem
responder dá para fazer; terminar sem responder **em silêncio**, não.

Como decidir cada tipo:

- **texto reconstruído** — o teste é de sentido, não de gramática. Se não
  encaixa no assunto, **o que sai é ele**, não a palavra vizinha real. Se
  for sua única evidência de alguma coisa (uma interrupção, por exemplo),
  escreva o `motivo` com ressalva em vez de afirmar: no IMG_0247_bruto um
  "Mãe! Mais do que..." recuperado virou "alguém grita Mãe! interrompendo"
  no `motivo`, e o áudio real era a Mariana fazendo um "oooh" de voz
  fofinha.
- **som alto sem texto** (`[??]`) — o microfone ouviu e ninguém leu.
  Mantido de propósito para você decidir. Sai ou fica.
- **retomada?** — o programa achou a mesma dupla de palavras repetida
  pouco depois e **não cortou nada**, porque a esta distância a repetição
  tanto pode ser tentativa abandonada quanto anáfora de propósito. As duas
  têm a MESMA forma; o que as separa é o sentido, e é por isso que a
  pergunta chega até você. Tentativa: "pessoas mesmo, que aparecem" …
  "pessoas mesmo, com as quais a gente se encontra no caminho" — a segunda
  substitui a primeira, corte a primeira inteira. Anáfora: "na Carta da
  Rainha, por exemplo … na Carta do Rei é o Rio" — as duas dizem coisas
  diferentes, fica tudo. Na dúvida, releia a frase inteira em volta do
  `[Pn]`: se as duas versões cabem juntas no mesmo parágrafo sem soar
  repetitivo, é anáfora.
- **fala rarefeita** — muito tempo e pouca palavra. Quase sempre parada
  longa, retomada, ou ela se perdendo.
- **texto nao explica o tempo** — a única pendência sobre trecho que **já
  está cortado**, e por isso a única em que "não fazer nada" não é uma
  posição neutra: se você não responder, saem N segundos que ninguém
  ouviu. A regra cortou com toda a razão aparente — o texto dizia
  "éé...", ou repetia a palavra vizinha — mas o tempo não cabe no texto.
  É assim que fala real some: onde o Whisper não consegue ler, ele
  escreve um cacoete curto e plausível no lugar do que foi dito. **Não dá
  para responder pelo texto**, porque o texto é justamente o que está sob
  suspeita: ouça o trecho (fase 3, `--em`) antes de deixá-lo sair. Se for
  mesmo hesitação, responda `sai`; se houver fala ali, o conserto é um
  `restauro` em `montagem.json` cobrindo o trecho, e depois anotações
  cortando só os pedaços que são de fato silêncio. Medido nos 7 fluxos de
  `work/`: 2 disparos, os dois no mesmo corte de 27,5s do "as de
  espadas", em que 17,9s eram fala.

**Três categorias saíram desta lista, e é bom saber por quê** — se você
viu alguma num `revisao.txt` antigo, ela não sumiu por descuido:

- `fala nao transcrita` (`X.Xs colados em 'palavra'`) apontava a palavra em
  que o áudio estava grudado, mas o programa já separou esse áudio num
  token próprio antes de escrever o arquivo. Medido nos 10 fluxos: nos 106
  casos o vizinho ou já está cortado, ou já é um `[rec: ...]`/`[??]` na
  lista. Era a mesma pergunta, do lado errado.
- `cauda arrastada` era pergunta de geometria feita a quem lê texto. Virou
  medida: o `conferir` diz `ARRASTO SOBREVIVE` quando a poda não coube no
  corte (14 casos nos 10 fluxos, contra 76 perguntas). Continua valendo a
  regra de onde consertar: **se o que você quer aparar cabe dentro de uma
  única palavra, isso não é anotação** — vai para `montagem.json`, fase 3.
- `disputa` este arquivo mesmo mandava não responder ("achado para o
  programador"). Agora ela aparece onde tem consequência: como nota numa
  linha `ILHA CURTA` do `conferir`, e como faixa suspeita no cabeçalho do
  `revisao.txt`.

### 2b. Varredura corrida (aberta) — é aqui que mora o que você veio buscar

A varredura 2a só cobre o que o programa conseguiu suspeitar. **A maioria
dos defeitos que chegaram ao Rafael não tinha marca nenhuma.** Releia o
texto do começo ao fim, sentença por sentença, **inclusive o que já parece
"a versão boa"**, e em cada **pausa** (`[-N.Ns]`, `[pausa N.Ns]`, `[rec:]`)
faça as três perguntas:

**(i) A frase antes da pausa fecha?** Se ela termina em preposição,
conjunção, artigo ou relativo — "…de", "…que", "…para uma", "…e" — ou se
para no meio de uma ideia, é candidata a tentativa abandonada. Também
conta a frase que fecha gramaticalmente mas não diz nada ("não saber se
tem mesmo uma boa" — adjetivo sem substantivo).

**(ii) O que vem depois refaz o que veio antes?** Este é o caso que o
Rafael relatou como o mais frequente: *ela repete a mesma linha de
pensamento uma ou mais vezes até chegar na frase certa, e as tentativas
estão vindo junto*. Repetição **literal** o programa já pega sozinho; a
que é sua é a refeita **com outras palavras**, que não casa por comparação
de texto:

> "Eles têm uma relação autônoma," … "eles são um povo autônomo em relação
> ao governo do Panamá"

> "ainda que não seja uma categoria, realmente" … "ainda que não haja uma
> categoria de gênero"

> "na vida do lente" … "na vida da pessoa que está" … "na vida de quem
> está abrindo o jogo"

**A boa é a última.** Descarte as anteriores inteiras — o começo abandonado
*e* a bagunça até a retomada. Deixar a primeira "porque também está certa"
é o que faz a mesma ideia aparecer duas vezes seguidas no áudio.

**Achar uma retomada não fecha a questão para o texto em volta.** No
IMG_0247_bruto a retomada de 78,5-158,5s foi encontrada, anotada e
obedecida, e mesmo assim sobrou uma retomada MENOR no início da versão
mantida ("Em Yala, todas… [pausa] em Yala, todas as rainhas…") e outra no
meio ("personagens políticos ou da cultura, enfim, personagens políticos,
artistas, enfim."). Nenhuma tinha `[??]`, `~` ou marca especial.
Proximidade de um defeito já achado não é evidência de que o resto está
limpo — se alguma coisa, é sinal de que ela estava incomodada com aquele
trecho e mexeu nele mais de uma vez.

**(iii) Tudo nesta frase pertence a esta aula?** Compare com o
`entendimento.md` e com as palavras-chave do guia. Um "ai que horror"
bocejado no meio da explicação de uma carta não tem nada a ver com astúcia,
isolamento ou território. Não precisa de marca do lado para ser suspeito;
precisa só de alguém que sabe do que a aula fala.

Procure ainda, na mesma passada:

- **Interrupção** — alguém entra, ela fala com outra pessoa, cachorro,
  celular. Sai inteiro.
- **Ela tateando antes de falar "para valer"** — "deixa eu ver se…", "tipo
  assim…", frase que não fecha gramaticalmente, fala baixinha. O texto
  sozinho pode ler normal; o que denuncia é o padrão de frase incompleta ou
  deveras hesitante.
- **Fim e começo** — sobra de gravação: ela ajeitando a câmera, olhando
  para os lados, saindo de quadro.

Na dúvida sobre CONTEÚDO, **deixe ficar**: cacoete que sobra se resolve com
uma anotação depois; frase apagada não se resolve, porque ninguém fica
sabendo que existiu. Na dúvida sobre uma TENTATIVA que você já identificou
como tentativa, **corte inteira**: meia tentativa é pior que nenhuma, e é
exatamente o farelo que sobra para o editor limpar à mão.

---

## Fase 3 — escrever

São dois arquivos, e eles respondem perguntas diferentes: o
`anotacoes.json` diz **o que sai**, e o `respostas.json` diz **o que você
decidiu sobre cada pendência** — inclusive as que ficam, que não deixam
marca em lugar nenhum.

### `work/<video>/respostas.json`

```json
[{"em": [223.35, 270.45], "resposta": "fica", "nota": "ela respirando fundo entre as frases"},
 {"em": 464.09, "resposta": "sai", "nota": "tentativa abandonada; a boa e a segunda"}]
```

- `em` é o **tempo** impresso na linha `Pn` do `revisao.txt`, copiado como
  está (duas casas). Uma lista quando a mesma decisão vale para várias.
- `resposta` é `sai`, `fica` ou `ouvido` — qualquer outra palavra e o
  programa recusa o arquivo em vez de ignorar a linha.
- **`sai` registra a decisão; não corta nada.** Quem corta é a anotação
  (ou o `transcript.md`, ou o editor). Se você escrever `sai` e o trecho
  continuar tocando, o `conferir` diz `PENDENCIA NAO OBEDECIDA` — irmã da
  `ANOTACAO NAO OBEDECIDA`, e pelo mesmo motivo.
- `nota` é para uma pessoa ler daqui a seis meses, como o `motivo` da
  anotação.

É o **tempo**, e não o número `n`, porque número caduca: uma pendência do
meio da lista que sai renumera todas as de baixo, e a resposta passaria a
valer para outra pergunta. Mesma razão pela qual `montagem.json` guarda o
`s`/`e` de cada palavra.

Duas coisas dispensam a linha escrita, e as duas o programa reconhece
sozinho: pendência cujo trecho **sai no corte** (a resposta é o corte) e
trecho que você **restaurou no editor** (você clicou naquele token). O que
não dá para adivinhar é "olhei e deixei ficar" — e é exatamente essa a
linha que este arquivo existe para guardar.

`respostas.json` é do mesmo tipo do `anotacoes.json`: não se regenera, não
se sobrescreve, e uma resposta já escrita não se apaga. Acrescente as suas
ao fim.

### `work/<video>/anotacoes.json`

```json
[{"ini": 228.1, "fim": 231.4, "motivo": "interrupcao domestica"}]
```

O `motivo` é para uma pessoa ler daqui a seis meses: diga o que ela abandona
e o que sobra. Sem acento (o resto do projeto é assim).

**Onde pôr a borda — nunca no chute.** O programa encolhe a anotação para
dentro até cair em fronteira de palavra. Se você mirar no meio de uma
palavra, **essa palavra sobrevive** — foi assim que um `reis,` ficou
pendurado em "as rainhas estão acima dos reis". `revisao.txt` só dá o
tempo do **começo de cada linha**, arredondado, de exibição — não é tempo
de palavra. **Não calcule tempo de palavra por palavras/segundo, mesmo
"só para uma estimativa".** Foi exatamente isso que decidiu a borda em
todas as 5 anotações do primeiro passe do IMG_0247_bruto, e deu errado em
2 delas sem que `conferir` acusasse nada (`ANOTACAO NAO OBEDECIDA` só
existe quando sobra fala real — não quando a borda engoliu uma palavra
*a mais*, tipo "figuras historicas," ali do lado): uma comeu "típica do
Brasil." inteira, outra deixou 25 milissegundos de margem num limite —
por sorte não deu errado, mas era pura sorte.

**`anotacoes.json` não corta PARTE de uma palavra — só serve para cortar
palavra(s) inteira(s) ou o silêncio entre elas.** `encaixa_anotacoes`
encolhe qualquer borda que caia no meio de uma palavra até a fronteira
dela; se `ini` e `fim` caem **dentro da mesma palavra** (por exemplo,
tentando tirar só o rabo arrastado de uma `cauda arrastada`), as duas
bordas encolhem para o mesmo ponto, a anotação vira largura zero e
**desaparece em silêncio** — nem erro, nem aviso, ela só some do arquivo
lido. Medido no 7 de espadas: duas anotações escritas para aparar cauda
arrastada sumiram assim, e só apareceram porque `conferir` tinha ido de
8 trechos aplicados para 10 escritos. Se o corte que você quer começa e
termina dentro da mesma palavra, isso não é anotação — é ajuste de
milissegundo, e o lugar certo é `work/<video>/montagem.json` (`cortes`,
uma lista de `[ini, fim]` em segundos; `tokens` e `restauros` podem ficar
vazios). Essa camada entra depois de tudo, sem encaixar em fronteira de
palavra e sem mínimo — é a mesma camada que o editor usa para os botões
de mais e menos.

Abra **`work/<video>/words.json`** — a proibição da fase 1 não cobre esse
arquivo, desde que você leia janelas dele e não o arquivo todo. Cada
palavra tem `s`/`e` (início/fim, em segundos, precisão de milissegundo)
medidos de verdade, não estimados:

```
venv\Scripts\python.exe -c "import json; ws=json.load(open('work/IMG_0247/words.json',encoding='utf-8'))['words']; [print(w['s'],w['e'],w['w']) for w in ws if 55<w['s']<75]"
```

Então:

- ache as duas palavras **limpas** (sem `align_rejeitado` — é o `~` do
  `revisao.txt`) mais próximas dos dois lados do corte, e ancore no
  **centro** do intervalo entre elas (não na borda: sobra margem para
  erro de medição de qualquer lado);
- se a palavra vizinha ao corte tiver `align_rejeitado`, não confie no
  `s`/`e` dela — pule para a palavra limpa seguinte, mesmo que isso
  troque o que sai e o que fica (por exemplo, uma palavra presa entre
  duas rejeitadas pode sair junto sem problema, contanto que a frase que
  sobra ainda feche sozinha);
- para tirar uma palavra, a anotação tem que **conter** a palavra inteira,
  não encostar nela;
- `[-N.Ns]` no `revisao.txt` ainda serve para saber ONDE olhar em
  `words.json` (é silêncio já cortado, então a região certa para procurar
  a palavra limpa mais próxima) — só não basta sozinho para decidir o
  número.

**`anotacoes.json` é sagrado.** Se o arquivo já existir:

- copie antes de escrever, uma vez só nesta sessão, para
  `anotacoes.json.bak-<AAAAMMDD-HHMM>`;
- **nunca apague uma anotação existente nem reescreva o `motivo` dela**;
- acrescente as suas ao fim;
- mover `ini`/`fim` de uma anotação antiga é permitido **só** quando a fase
  4 provar que ela não foi obedecida — e aí diga no relatório o que moveu e
  por quê.

---

## Fase 4 — fechar o laço (não pule)

Escrever a anotação não é o mesmo que ela ter valido. Já houve quatro
caminhos diferentes pelos quais uma anotação foi lida, aplicada e sumiu no
cálculo do corte, sem uma linha de aviso. Você não tem como saber se foi
obedecida a menos que pergunte:

**Antes de perguntar, se a anotação cobre fala de verdade transcrita**
(interrupção, retomada — qualquer coisa em que o Whisper acertou o texto),
rode `analyze --force` primeiro. `aplica_anotacoes` — a função que marca as
palavras dentro da anotação como cortadas — só roda dentro do `analyze`;
`conferir`/`audio`/`render` leem o `anotacoes.json` do disco e injetam o
trecho como corte bruto, mas isso não desprotege a palavra individual, e a
rede de segurança `_respeita_palavras` devolve pra tela qualquer palavra que
ainda esteja com `cut=False` no `stream.json`. Medido no IMG_0246_bruto: uma
anotação de 47,6s cobrindo "Que foi, Rafudo? [...] Tadinho. [...] Sim."
sobrou 2,72s — exatamente a soma das três palavras/frases reais dentro do
trecho — até rodar `analyze --force` e `draft --force` (o `analyze --force`
troca o `stream.json`, e sem o `draft` depois dele o `conferir` recusa por
`transcript.md` desatualizado). Anotação só de silêncio/hesitação sem
palavra real dentro não precisa disso.

**Este laço APAGA o que você tiver editado no `transcript.md`, e apaga em
silêncio.** O `analyze` não lê aquele arquivo: `reconcilia` é chamado uma
vez só, dentro do `_base` (`tarocut.py:2686`), que é o que `conferir`,
`audio`, `render` e o editor usam para montar o corte — `cmd_analyze` não o
chama. E o `cmd_draft` não tem cache nenhum: reescreve o `transcript.md` a
partir do `stream.json` toda vez, com `--force` ou sem. Então quem apagou
texto, ou trocou `⟦ ⟧` por `⟨ ⟩`, antes do laço perde a edição — o arquivo
volta com as propostas automáticas, e a decisão não sai do corte com um
aviso, sai sem nenhum.

Por isso: **edite o `transcript.md` DEPOIS do laço, nunca antes.** Se já
tinha editado, guarde o que mudou, refaça a edição no arquivo regenerado e
confirme que ela está lá antes de rodar o `conferir` — o cabeçalho novo
traz o selo de outro `stream`, e a marca antiga não sobrevive a ele.
MEDIDO no "5 de espadas": `espadas...` da primeira frase saía por
`alongamento` (a carta perdia o nome na abertura), foi resgatada com
`⟨espadas...⟩`, e o `draft --force` da fase 4 devolveu o arquivo com
`⟦espadas...⟧`. Só a segunda aplicação chegou ao corte.

```
venv\Scripts\python.exe tarocut.py analyze $ARGUMENTS --force   # so se houver fala real na anotacao
venv\Scripts\python.exe tarocut.py draft $ARGUMENTS --force     # idem; reescreve o transcript.md do zero (releia o aviso acima)
venv\Scripts\python.exe tarocut.py conferir $ARGUMENTS
```

**O critério de terminar é binário: zero linhas `ANOTACAO NAO OBEDECIDA`.**
Enquanto houver uma, você não terminou.

Se ele abortar dizendo que o `transcript.md` está desatualizado, rode
`tarocut.py draft $ARGUMENTS` e tente de novo — vale o mesmo aviso: esse
`draft` reescreve o arquivo do zero, então reaplique nele o que você tiver
editado à mão.

Para cada `ANOTACAO NAO OBEDECIDA`, a linha diz `sobra Xs de Ys`. Diagnostique:

| o que você vê | causa provável | o que fazer |
|---|---|---|
| sobra ~0,1–0,2s, nas pontas | borda caiu no meio de uma palavra e encolheu | alargue a anotação até conter a palavra inteira |
| sobra é a soma exata de palavras/frases reais dentro do trecho | você ainda não rodou `analyze --force` (ver acima) | rode `analyze --force` + `draft --force`, tente de novo |
| sobra um pedaço grande no meio, e isso já não resolveu | há palavra mantida dentro do trecho por outro motivo | alargue, ou parta a anotação em duas em volta dela |
| a região tem `~` no `revisao.txt` | o alinhador embaralhou os tempos ali | abra `words.json`, ache a palavra limpa mais próxima (fase 3) — não reancore só "no `[-N.Ns]` vizinho" de olho, isso ainda é chute |
| sobra e nada disso explica | provavelmente não é sua culpa | pare e reporte (fase 5) |

**`ANOTACAO NAO OBEDECIDA` não é o único jeito de errar a borda — só é o
único que este comando acusa sozinho.** Cortar uma palavra a mais (que
devia ficar) não sobra em lugar nenhum, então zero `ANOTACAO NAO OBEDECIDA`
não prova que a borda está certa. Depois de bater esse critério, releia
o trecho ao redor de cada borda em `revisao.txt` (ou, melhor, confirme os
tempos exatos em `words.json` como na fase 3) para garantir que não sumiu
palavra por engano — foi assim que "figuras historicas," saiu junto sem
querer no IMG_0247_bruto, sem nenhuma linha de erro.

Ajuste, rode `conferir` de novo. **No máximo 3 rodadas.** Se na terceira
ainda sobrar, pare e reporte o que sobrou — laço infinito é pior que achado
honesto.

**As outras linhas do `conferir` você TRIA todas**, não só as que caem
dentro de um trecho que você anotou. `revisar` e `conferir` existem
justamente para nada passar batido; "não é minha responsabilidade" foi como
uma ilha curta de 0,36s real (uma palavra solta entre duas emendas, no meio
de uma disputa de recuperação automática) escapou do IMG_0246 e só apareceu
porque alguém ouviu o `audio` por fora do processo. Para cada linha, decida
e registre na fase 5:

- **`PENDENCIA NAO OBEDECIDA`** — você respondeu `sai` e o trecho
  continua no corte. Ou faltou a anotação, ou ela não pegou o trecho.
- **`PENDENCIA SEM RESPOSTA`** — uma pendência que você não respondeu.
  Ou é esquecimento, ou o tempo que você copiou para o `respostas.json`
  saiu errado (nesse caso aparece também uma linha `# nota: a resposta em
  [...] nao casa com pendencia nenhuma`). O primeiro número da coluna
  `fonte` é o `em` que faltava.
- **`ARRASTO SOBREVIVE`** — a poda da cauda arrastada não coube no corte
  (cauda menor que 0,34s é descartada pelo `minimo`) e o rabo continua
  audível. Conserto: `montagem.json`/`cortes`, não anotação — está dentro
  de uma palavra só.
- **`PALAVRA ENTERRADA`** — pelo relógio do **Whisper**, uma palavra que
  era para ficar está segundos dentro do buraco, longe da emenda. Quem
  errou ali é o alinhador, não o corte, e por isso **mover a borda da
  anotação não resolve**: ela está ancorada num tempo que a palavra não
  ocupa. É a mesma família do `~`, medida contra o corte pronto — o
  tempo da coluna `fonte` é o do Whisper, e é nele que se ouve. Foi um
  destes que o Rafael ouviu como um "ou" estranho no meio do áudio do
  "as de espadas" (a disputa "lâmina"/"mental,"). Conserto: ouvir e
  fechar o vazamento em `montagem.json`/`cortes`. Aparece em todos os
  vídeos (2 a 7 por vídeo, medido nos 7 fluxos): **não** é para tratar
  cada linha como defeito, é para ouvir as que caem perto de trecho que
  você anotou.
- **`REPETICAO AUDIVEL`** — a mesma palavra duas vezes seguidas no corte.
  Metade destes casos **o corte criou**: no IMG_0248 a anotação de
  343,7-350,4s ("três tentativas da mesma oração") encosta o "que" de
  antes no "que" de depois. Isso é *seu*, e o conserto costuma ser mover
  uma borda de anotação para engolir uma das duas ocorrências.
- **desenho, não defeito** — a maioria. `FALA CORTADA SOBREVIVE` de
  0,1–0,4s é quase sempre o `minimo`/`ilha_minima` decidindo que a emenda
  não vale o pulo na tela; confirme o tamanho e siga.
- **vale ouvido** — sobra grande, ou uma `ILHA CURTA` com uma palavra
  isolada entre dois cortes (sinal de `disputa`). Rode
  `tarocut.py audio $ARGUMENTS` e escute esse ponto antes de decidir.
- **defeito confirmado** — vai para "achados para o programador" na fase
  5, nunca para o `motivo` de uma anotação (isso não é julgamento de
  conteúdo).

Se precisar traduzir um tempo da prévia em áudio para o tempo da fonte:
`tarocut.py conferir $ARGUMENTS --em 3:05`.

---

## Fase 5 — relatório

Termine com cinco blocos curtos:

1. **A aula, em uma frase** — copiada do `entendimento.md`. Serve para o
   Rafael conferir de relance se você entendeu o vídeo certo; se não
   entendeu, todo o resto do passe está suspeito.
2. **Pendências** — uma linha por `Pn` (agrupadas quando a decisão é a
   mesma): saiu, ficou, ou vale ouvido. **Nenhuma pode faltar**, e o
   `respostas.json` tem de dizer o mesmo que este bloco — diga aqui
   quantas o `conferir` ainda cobra (o normal é zero).
3. **O que anotei** — uma linha por anotação: tempo, o que sai, por quê.
4. **O que o `conferir` diz** — confirme "zero ANOTACAO NAO OBEDECIDA", ou
   liste o que sobrou e por que você parou. Some a triagem das outras
   linhas.
5. **Achados para o programador** — se alguma coisa parecer defeito de
   código, escreva aqui, **nunca dentro do campo `motivo`**.

Essa última regra existe porque já aconteceu: duas das 19 anotações do
IMG_0247 são diagnóstico de código escrito em prosa dentro do `motivo`
("PARCIAL - so tira o 'e' de 49.8-50.6...", "O corte mecanico entrava em
55.43..."). O arquivo passou a mentir sobre o motivo do corte, e o achado
não chegou a quem podia consertar. Julgamento de conteúdo vai no arquivo;
defeito de programa vai no relatório.

---

## Corrigindo com base em áudio já ouvido

Cenário diferente do resto deste arquivo: o Rafael já rodou `audio` ou
`previa`, já ouviu o `_previa.m4a` e volta com uma lista de tempos e
defeitos ("em tal ponto sobrou um bocejo", "aqui repete uma palavra").
Ele não está pedindo um passe de julgamento do zero — está apontando
onde um passe (automático ou já feito) errou. Algumas diferenças
importantes em relação às fases 1-5:

- **Os tempos que ele dá são da PRÉVIA, não da fonte.** São relógios
  diferentes assim que existe um corte antes do ponto citado — 1 minuto
  de prévia pode ser 2 ou 3 minutos de fonte. **Sempre** rode
  `tarocut.py conferir $ARGUMENTS --em <tempo>` antes de tocar em
  qualquer arquivo. Calcular isso de cabeça, ou assumir que os relógios
  batem, é a maneira mais rápida de anotar o trecho errado.
- **Se já existir `entendimento.md`, leia-o em vez de refazer a fase 1.**
  Se não existir, faça a 1b (o verbete da carta no guia) mesmo assim: é
  barato e é o que decide se um trecho suspeito é fala boa ou ruído.
- **A proibição de abrir `stream.json` e `words.json` inteiros da fase 1
  não se aplica aqui do mesmo jeito.** Aquela regra existe para não
  gastar 32 mil tokens julgando um vídeo do zero; aqui você já sabe
  exatamente quantos pontos verificar (tipicamente menos de dez) e
  exatamente onde cada um cai, então abrir uma janela de `words.json`
  ou `stream.json` em volta de UM tempo específico é barato e é a
  ferramenta certa — foi assim que se descobriu, no 7 de espadas, que
  um "bocejo" era na verdade um token `hesitacao` de baixa confiança
  mantido por engano, e que um "no-no" repetido era na fonte três
  ocorrências, não duas.
- **Quando o texto não resolve a dúvida, um espectrograma da fonte
  resolve mais barato que pedir para alguém ouvir.** Nem toda dúvida
  desta lista aparece no texto (um trecho arrastado, uma repetição que
  o Whisper juntou em uma palavra só). Gerar uma imagem da região com
  `ffmpeg` já responde "isso é um som sustentado (vogal arrastada) ou
  dois pulsos curtos (palavra repetida)?" sem precisar decodificar
  áudio numa ferramenta de tocar som:
  ```
  ffmpeg -y -ss <inicio-2s> -t <alguns-segundos> -i work/<video>/audio.wav \
    -update 1 -lavfi "showspectrumpic=s=1400x400:legend=1" saida.png
  ```
  Leia a imagem gerada. Um pulso sustentado com bandas horizontais
  estáveis por 0,3-0,8s é vogal arrastada (o "eeee"/"iiii" de uma cauda
  ou de um resmungo); pulsos curtos e repetidos com silêncio entre eles
  são palavras/sílabas distintas, não uma só esticada. Isso não
  substitui ouvir quando a dúvida é sobre O QUE foi dito — só quando a
  dúvida é sobre a FORMA do som (quanto dura, é uma coisa ou são duas).
- **O procedimento de escrita continua o mesmo da fase 3-4** (as
  pendências que sobrarem em aberto continuam pedindo linha no
  `respostas.json`): faça backup de `anotacoes.json` uma vez nesta sessão
  (`anotacoes.json.bak-AAAAMMDD-HHMM`), nunca apague ou reescreva
  anotação existente, ancore em `words.json` como sempre — e lembre da
  regra da fase 3: se o corte cai dentro de uma única palavra (aparar
  uma cauda arrastada, por exemplo), vai para `montagem.json`/`cortes`,
  não para `anotacoes.json`. Feche o laço com `analyze --force` (só se
  houver fala real na anotação) + `draft --force` + `conferir` do mesmo
  jeito — incluindo o aviso da fase 4 de que o `draft` reescreve o
  `transcript.md` do zero, então reaplique nele o que estiver editado à
  mão —, e trie as linhas restantes do `conferir` do mesmo jeito da fase
  4 — inclusive as que não têm nada a ver com o que o Rafael relatou,
  porque ele só ouviu o que sobrevive no corte de hoje, não os pontos que
  já eram suspeitos antes.
- **Se um tempo relatado não bater com nada suspeito na região**, isso
  não vira anotação no chute. Diga no relatório que não achou o defeito
  descrito naquele ponto e o que checou, para o Rafael confirmar o tempo
  ou ouvir de novo — errar a anotação por "algo tinha que estar errado
  ali" é pior que reportar honestamente que não achou.
