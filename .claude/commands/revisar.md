---
description: Passe de julgamento de um vídeo do tarocut - lê a transcrição, escreve anotacoes.json e confere que o corte obedeceu
argument-hint: <caminho do vídeo ou nome curto, ex. IMG_0247>
allowed-tools: Bash(venv/Scripts/python.exe:*), Read, Write, Edit
---

Faça o passe de julgamento do vídeo: **$ARGUMENTS**

Você é a única parte deste programa que lê o que ela DISSE. Todo o resto
decide por regra e por energia de áudio. O que você acrescenta é o que
nenhuma heurística pega: interrupção, raciocínio refeito com outras
palavras, trecho em que ela se perde.

## A economia deste comando

Este comando existe para rodar numa janela limpa. Cada resposta relê a
conversa inteira, então **leia só dois arquivos**: `work/<video>/revisao.txt`
e `work/<video>/conferir.txt`. Nada mais.

Não abra `stream.json` (32 mil tokens contra 1,3 mil do `revisao.txt`), não
abra `transcript.md`, não abra `tarocut.py`. Se sentir falta de algo que só
o código explica, isso é sinal de que você está tentando consertar um bug —
veja a fase 5.

Essa proibição é sobre **julgar** conteúdo, não sobre **cortar** com
precisão: `work/<video>/words.json` fica de fora dela. Veja a fase 3 —
colocar borda de anotação sem abrir esse arquivo foi a causa raiz de um
passe inteiro saído errado no IMG_0247_bruto.

Rode tudo da pasta do projeto, com o Python do venv. O nome curto
(`IMG_0247`) funciona tanto quanto o caminho completo.

## Fase 1 — gerar e ler

```
venv\Scripts\python.exe tarocut.py revisar $ARGUMENTS
```

Leia o `work/<video>/revisao.txt` que sair. O cabeçalho explica as marcas;
as três que mais importam para você:

- `[?? N.Ns]` — som alto que ninguém conseguiu ler, **mantido de propósito**
  para você decidir. Toda ocorrência precisa de uma resposta sua: sai ou
  fica.
- `[!Ns/N palavras]` — o que sobra ali é fala rarefeita. Quase sempre é
  parada longa, retomada ou ela se perdendo. Olhe cada um.
- `[-N.Ns VOZ]` — vai sair, mas tem som alto dentro. Confira que não é
  frase inteira prestes a sumir.

E uma regra dura:

- `palavra~` — o tempo dessa palavra **não é confiável**; o alinhador
  recusou o próprio resultado. Ela pode estar segundos longe de onde
  aparece. **Nunca ancore borda de corte numa palavra com `~`.** Se o
  trecho que você quer cortar começa ou termina numa delas, ancore no
  `[-N.Ns]` mais próximo e deixe a fase 4 ajustar.

**Antes de marcar qualquer coisa, leia o `revisao.txt` inteiro uma vez, do
início ao fim, sem anotar nada — só para entender do que a aula trata.**
Julgamento de conteúdo (interrupção, retomada, ela se perdendo) só é
confiável com o assunto na cabeça; sem isso a fase 2 vira caça a marcador
isolado, e é assim que um trecho comum passa batido só porque não tinha
`[??]` nem `~` do lado. Não precisa escrever esse resumo em lugar nenhum —
é para você, não para o relatório.

## Fase 2 — julgar

**Depois do resumo, releia sentença por sentença, sem pular nada — inclusive
o que já parece "a versão boa".** Achar uma retomada grande não fecha a
questão para o texto ao redor: no IMG_0247_bruto a retomada de 78,5-158,5s
foi encontrada, anotada e obedecida, e mesmo assim sobrou uma retomada
MENOR e mais curta bem no início da versão mantida ("Em Yala, todas...
[pausa] em Yala, todas as rainhas...") e outra no meio ("personagens
políticos ou da cultura, enfim, personagens políticos, artistas, enfim.")
- nenhuma delas tinha `[??]`, `~` ou qualquer marca especial; só apareciam
lendo a frase inteira com atenção. **Proximidade de um defeito já achado
não é evidência de que o resto está limpo** - se qualquer coisa, é sinal
de que ela estava incomodada com aquele trecho e pode ter mexido nele mais
de uma vez.

Procure, nesta ordem:

1. **Interrupção** — alguém entra, ela fala com outra pessoa, cachorro,
   celular. Sai inteiro. **Cuidado com o texto reconstruído** (token
   `hesitacao` recuperado por `resolve_suspeitos`/`marca_ecos`, sem tempo de
   Whisper original): ele veio de uma re-transcrição isolada e curta, e o
   próprio programa às vezes marca a leitura como incerta (`why` diz "nao e
   fala" ou parecido). Não vire esse texto uma cena na sua cabeça sem
   ressalva - no IMG_0247_bruto um "Mãe! Mais do que..." recuperado virou
   "alguém grita Mãe! interrompendo" no `motivo`, e o áudio real (ouvido
   depois) era a Mariana fazendo um "oooh" de voz fofinha, nada a ver com
   interrupção de terceiro. Se o texto reconstruído for sua única evidência
   e a leitura for de baixa confiança, escreva o `motivo` com ressalva
   ("provavelmente X, mas texto recuperado com baixa confiança") em vez de
   afirmar, e considere marcar para "vale ouvido" na fase 4 em vez de só
   confiar no texto.
2. **Raciocínio refeito com outras palavras** — ela diz uma coisa, para, e
   diz de novo trocando as palavras. Repetição literal o programa já pega
   sozinho; esta não casa, e por isso é sua.
3. **Ela se perde** — começa uma frase que não vai a lugar nenhum e retoma
   depois. Descarte a tentativa, guarde a boa. Inclui falar baixinho,
   tateando o raciocínio antes de falar "para valer" ("deixa eu ver se...",
   "tipo assim...", frase que não fecha gramaticalmente) - o texto sozinho
   pode não denunciar isso (lê normal), é o padrão de frase incompleta ou
   deveras hesitante que denuncia.
4. **Fim e começo** — sobra de gravação: ela ajeitando a câmera, olhando
   para os lados, saindo de quadro.
5. Os `[?? N.Ns]` e `[!Ns/N palavras]` que ainda não estiverem cobertos.
6. **`[disputa N.Ns]`** — não é julgamento de conteúdo, é aviso de
   geometria: duas recuperações automáticas (duração impossível +
   retomada colapsada) brigaram pelo mesmo áudio ali. Quase sempre o texto
   ao redor está certo (é uma frase real, só malalinhada) — mas o corte
   pode sair fragmentado (uma palavra solta entre duas emendas). Não dá
   pra saber pelo texto; rode `tarocut.py audio $ARGUMENTS` e ouça esse
   ponto específico antes de aceitar. Se soar errado, isso é achado para
   o programador (fase 5), **não** anotação — anotação é para o que você
   decide que não deveria estar no vídeo, não para consertar um defeito
   de corte.

Na dúvida, **deixe ficar**. Cacoete que sobra se resolve com uma anotação
depois; frase apagada não se resolve, porque ninguém fica sabendo que
existiu.

## Fase 3 — escrever

`work/<video>/anotacoes.json`:

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

Abra **`work/<video>/words.json`** — a proibição da fase 1 não cobre esse
arquivo. Cada palavra tem `s`/`e` (início/fim, em segundos, precisão de
milissegundo) medidos de verdade, não estimados. Rode algo como:

```
venv\Scripts\python.exe -c "import json; ws=json.load(open('work/IMG_0247/words.json',encoding='utf-8'))['words']; [print(w['s'],w['e'],w['w']) for w in ws if 55<w['s']<75]"
```

para listar as palavras num intervalo e achar as que cercam o corte. Then:

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
  a palavra limpa mais próxima) — só não basta mais sozinho para decidir
  o número.

**`anotacoes.json` é sagrado.** Se o arquivo já existir:

- copie antes de escrever, uma vez só nesta sessão, para
  `anotacoes.json.bak-<AAAAMMDD-HHMM>`;
- **nunca apague uma anotação existente nem reescreva o `motivo` dela**;
- acrescente as suas ao fim;
- mover `ini`/`fim` de uma anotação antiga é permitido **só** quando a fase
  4 provar que ela não foi obedecida — e aí diga no relatório o que moveu e
  por quê.

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
trecho — até rodar `analyze --force` (que reconcilia com o `transcript.md`
antes de você rodar `draft --force` de novo, então tenha isso a mão) e
`draft --force` (o `analyze --force` troca o `stream.json`, e sem isso o
`conferir` recusa por `transcript.md` desatualizado). Anotação só de
silêncio/hesitação sem palavra real dentro não precisa disso.

```
venv\Scripts\python.exe tarocut.py analyze $ARGUMENTS --force   # so se houver fala real na anotacao
venv\Scripts\python.exe tarocut.py draft $ARGUMENTS --force     # idem, senao conferir recusa por transcript.md desatualizado
venv\Scripts\python.exe tarocut.py conferir $ARGUMENTS
```

**O critério de terminar é binário: zero linhas `ANOTACAO NAO OBEDECIDA`.**
Enquanto houver uma, você não terminou.

Se ele abortar dizendo que o `transcript.md` está desatualizado, rode
`tarocut.py draft $ARGUMENTS` e tente de novo.

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

**As outras linhas do `conferir` (`ILHA CURTA`, `FALA CORTADA SOBREVIVE`,
`PALAVRA RASPADA`) você TRIA todas, não só as que caem dentro de um trecho
que você anotou.** `revisar` e `conferir` existem justamente para nada
passar batido; "não é minha responsabilidade" foi como uma ilha curta de
0,36s real (uma palavra solta entre duas emendas, no meio de uma disputa de
recuperação automática — ver `[disputa]` na fase 2) escapou do IMG_0246 e
só apareceu porque alguém ouviu o `audio` por fora do processo. Para cada
linha, decida e registre na fase 5:

- **desenho, não defeito** — a maioria. `FALA CORTADA SOBREVIVE` de
  0,1–0,4s é quase sempre o `minimo`/`ilha_minima` decidindo que a emenda
  não vale o pulo na tela (ver `config.toml`); confirme o tamanho e siga.
- **vale ouvido** — sobra grande, ou uma `ILHA CURTA` com uma palavra
  isolada entre dois cortes (sinal de `[disputa]`, ver fase 2). Rode
  `tarocut.py audio $ARGUMENTS` e escute esse ponto antes de decidir.
- **defeito confirmado** — vai para "achados para o programador" na fase
  5, nunca para o `motivo` de uma anotação (isso não é julgamento de
  conteúdo).

Se precisar traduzir um tempo da prévia em áudio para o tempo da fonte:
`tarocut.py conferir $ARGUMENTS --em 3:05`.

## Fase 5 — relatório

Termine com três blocos curtos:

1. **O que anotei** — uma linha por anotação: tempo, o que sai, por quê.
2. **O que o `conferir` diz** — confirme "zero ANOTACAO NAO OBEDECIDA", ou
   liste o que sobrou e por quê você parou.
3. **Achados para o programador** — se alguma coisa parecer defeito de
   código, escreva aqui, **nunca dentro do campo `motivo`**.

Essa última regra existe porque já aconteceu: duas das 19 anotações do
IMG_0247 são diagnóstico de código escrito em prosa dentro do `motivo`
("PARCIAL - so tira o 'e' de 49.8-50.6...", "O corte mecanico entrava em
55.43..."). O arquivo passou a mentir sobre o motivo do corte, e o achado
não chegou a quem podia consertar. Julgamento de conteúdo vai no arquivo;
defeito de programa vai no relatório.
