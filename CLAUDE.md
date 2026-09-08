# tarocut

Pré-editor dos vídeos do curso *Tarô Fora da Caixa*. Um usuário, uma máquina,
34 vídeos. Roda 100% local: sem nuvem, sem API, sem créditos — **isso é um
requisito, não um detalhe.** Não introduza dependência que precise de rede em
tempo de execução.

O `README.md` explica o fluxo para quem *usa*. Este arquivo é para quem *mexe
no código*.

## Rodar qualquer coisa

Sempre pelo Python do venv, nunca `python` solto:

```
venv\Scripts\python.exe tarocut.py <comando> "C:\Users\marip\Videos\IMG_0247.MOV"
```

Etapas, em ordem: `ingest → transcribe → align → analyze → draft →
editor → render`.
`run` faz ingest→draft; `lote` faz uma pasta inteira. Cada etapa tem cache;
`--force` refaz. `tarocut.bat` abre a janela (`gui.py`), que só chama esses
mesmos comandos.

## Mapa

| arquivo | o que é |
|---|---|
| `tarocut.py` | tudo: CLI, pipeline, marcação, geometria do corte, render |
| `alinhar.py` | wav2vec2 — fronteiras de palavra (o Whisper erra os tempos) |
| `gui.py` | janela Tk; só dispara comandos e mostra estado |
| `editor.py` | servidor do editor: a sessão, os gestos, o que a tela pergunta |
| `editor.html` | a tela: a transcrição, os quatro botões, o motor de áudio |
| `teste_editor.py` | as promessas do editor que quebram em silêncio |
| `teste_revisao.py` | as promessas do passe de julgamento que quebram em silêncio |
| `lexico.py` | gera `lexicon/terms.txt` a partir do livro do Yala |
| `.claude/commands/revisar.md` | o passe de julgamento: o que o Claude lê e o que ele tem de conferir depois |
| `.claude/commands/preparar.md` | run + revisar + conferir + audio num comando só, pronto pro editor de texto |
| `.claude/commands/anotar.md` | o passe do OUVIDO: absorve as anotações que o Rafael faz ouvindo o `.m4a`, e pergunta se alguma delas ensina algo ao programa |
| `config.toml` | **todos** os números do gosto de corte |
| `material/padroes-edicao.md` | medições contra os vídeos editados à mão |
| `material/licoes-do-ouvido.md` | o que o ouvido do Rafael pegou e o programa não, uma linha por achado do `/anotar`. Existe para a SEGUNDA ocorrência do mesmo defeito ser reconhecida como segunda — um caso não é medição |
| `correcoes-pendentes.md` | **histórico, não pendências.** O nome mente: as 8 correções de 01/09 estão todas feitas, e o arquivo virou o registro de por que cada uma foi feita |

Estado por vídeo em `work/<video>/`: `meta.json`, `audio.wav`, `words.json`,
`stream.json` (a fonte da verdade), `transcript.md`, `revisao.txt`,
`conferir.txt`, `entendimento.md` (o que o passe de julgamento entendeu da
aula), `ouvido.md` (o que ele anotou ouvindo o `.m4a`, colado literal, uma
seção por rodada), `anotacoes.json`, `respostas.json`, `montagem.json`,
`correcoes.json`.

As três camadas de decisão humana empilham nesta ordem, que é a ordem em
que foram tomadas: `transcript.md` (apagar texto) → `anotacoes.json` (o
passe de julgamento, trechos de tempo) → `montagem.json` (o editor). O
`_base` monta as três; a última vence, por ser a mais explícita.

`montagem.json` tem duas camadas, e a diferença entre elas é o arquivo
inteiro. `tokens` são decisões de **palavra** e passam por toda a geometria
de emenda — quem oculta uma palavra quer a palavra fora, não um número de
milissegundos. `cortes` e `restauros` são trechos de **tempo**, os botões
de mais e menos, e entram depois de tudo, sem snap e sem mínimo: quem pediu
1 ms está olhando a palavra e quer 1 ms.

**Não se chama `edicao.json`, e isso é de propósito.** Sobraram
`edicao.json` em `work/` do editor removido em 01/09 (`git show
cf6e07e:editor.py`), com outro formato e decisões de outra época. Passar a
ler aqueles arquivos seria reimpor decisão velha em silêncio.

## O que não quebrar

**`config.toml` é medição, não gosto.** Cada número tem um comentário dizendo
contra o quê foi calibrado e o que aconteceu quando estava errado. Não mexa
num valor para fazer um sintoma sumir. Meça primeiro, num vídeo real, e
**atualize o comentário com a nova medição** — o comentário é o valor.

E **todo número que decide corte mora lá**, não como valor padrão de
parâmetro. Cinco moravam: os quatro de `detecta_tentativas` (hoje
`[tentativa]`), a janela do `marca_ecos` e o limite de gaguejada (hoje
`[cacoete]`), o `volta_max` do `marca_instrucoes` e o `deriva_max` do
`alinhar.descarta_ruins`. Depois vieram os dois de `classifica_fala` —
quantas palavras cabem num cacoete recuperado e o que é palavra curta,
hoje `[hesitacao].recuperado_max_palavras` e `.palavra_curta_max` — e o
`minimo` de `protege_enumeracoes`, que era pior: um segundo lugar
decidindo o que `fortes_min` já decidia, invisível para quem mexesse no
config. Esse foi apagado, não mudado de lugar. Default de parâmetro é um
segundo lugar decidindo a mesma coisa, e foi assim que o piso de ruído
chegou a viver com `12.0` no braço dentro do `alinhar.py`. O que fica no
código é o que decide *forma de regra*, não taxa: `ENFASE`, `HALLU`,
`RE_NUM`, `MAO_MANTEM`.

**`anotacoes.json` é sagrado.** É o passe de julgamento: caro de produzir, uma
vez por vídeo, para sempre. Nunca regenere, sobrescreva ou apague. Ele
sobrevive a `run --force` de propósito. **`respostas.json` é da mesma
família** e vale a mesma regra: um diz o que sai, o outro diz o que a
pessoa decidiu sobre cada pendência — inclusive as que ficam, que não
deixam marca em lugar nenhum.

**Anotação que some em silêncio é o pior defeito que este programa tem.**
Já apareceu por quatro caminhos: o `_funde` juntando a anotação a um token
e o `_respeita_palavras` truncando o resultado; o filtro `minimo`
descartando anotação curta; o `confere_atualidade` aceitando `transcript.md`
sem selo, que fazia o `reconcilia` casar token errado; e a borda encolhida
pelo `encaixa_anotacoes` (só esse é proposital). Os três primeiros estão
fechados.

A pergunta agora mora em `anotacoes_desobedecidas`, uma vez só, e é feita em
dois lugares: o `conferir` lista, e o `_plano` avisa — ou seja, `render`,
`audio` e `previa` reclamam sozinhos, sem depender de alguém lembrar. Se
mexer na geometria, o teste é: **toda anotação tem que sobrar ~0 s.** O que
os quatro caminhos tinham em comum não era a causa; era ninguém perguntar.

**Corte que a regra faz por TEXTO se cobra pelo tempo, não pelo texto.**
Onde o Whisper não consegue ler, ele não devolve nada legível — devolve um
cacoete curto e plausível no lugar da fala. Aí a regra de texto
(`alongamento`, `retomada colapsada`, `tentativa`…) corta com toda a razão
aparente, e o trecho sai sem `[??]`, sem `[rec:]`, sem `VOZ` na marca e sem
pendência nenhuma: é o único caminho pelo qual fala real some sem que nada
pergunte. MEDIDO no "as de espadas": 27,5s (35,46-62,92s) num corte só, dos
quais o Rafael confirmou 9,6s de hesitação e 17,9s de fala, ouvindo a fonte.
Os três tokens do corte eram "mais...", "profunda, mais potente." e um "éé…"
de 200 caracteres — e nenhum era `pausa`.

A pergunta que fecha isso é a que `marca_arrastadas` já fazia para podar
cauda ("este tempo cabe no que está escrito?"), com os mesmos números dela
(`[arrastada].fator`), e ela falhou ali por DUAS razões independentes:
`marca_cacoetes` corre antes e já tinha cortado o token — e `marca_arrastadas`
pula token cortado —, e mesmo que olhasse, `letras_faladas` conta 200 letras
num "éé…" de uma vogal só, o que fazia 20,5s de um som parecerem 9,7
caracteres por segundo, ritmo de fala fluente. Por isso `letras_uma_vez`
existe e é SEPARADA de `letras_faladas`: aquela decide corte no `analyze`,
esta decide pergunta. Trocar a de baixo mudaria o corte de todo vídeo já
analisado, e isso é medição que ninguém fez.

MEDIDO nos 7 fluxos, sobre 216 cortes automáticos de ≥1s: oito tokens
cortados por texto duram mais que `fator` vezes o que o texto explica, e
eles vêm em dois montes sem nada entre eles — seis de 1,40 a 2,27s, todos
cacoete de verdade ("Eeeeee" sustentado, "É... isso."), e dois de 6,06 e
20,54s, que são o mesmo corte de 27,5s. `corte_por_texto_min` (3,0) mora
nesse vazio. Corte por texto legítimo bem mais longo existe (uma "retomada
colapsada" de 3,90s no IMG_0248) e não entra na lista, porque o texto dela
dá conta do tempo: o piso nunca decide sozinho.

E ela é a ÚNICA pendência sobre trecho que **já sai**, o que inverte a
conta de `pendencias_sem_resposta`: cortar não responde nada, porque o
corte já estava feito quando a pergunta nasceu. Responde-la é restaurar o
trecho ou escrever no `respostas.json` — e é `fica`, não `sai`, que vira
`PENDENCIA NAO OBEDECIDA` quando ninguém age. Sem essa inversão a pergunta
nasceria e sumiria da cobrança no mesmo instante.

**Pendência sem resposta é a mesma família de defeito, do outro lado.** O
`revisao.txt` lista, numera e ancora cada `Pn`, e até 03/09/2026 nada
verificava se alguma tinha ficado sem resposta — "responda todas" era
regra do prompt do `/revisar` e de mais ninguém. A pergunta agora mora em
`pendencias_sem_resposta`, uma vez só, e é feita nos mesmos dois lugares
que a da anotação desobedecida: o `conferir` lista (`PENDENCIA SEM
RESPOSTA`) e o `_plano` avisa. Responder é uma destas três coisas, e o
programa reconhece as três: **cortar** o trecho, **restaurar** o token no
editor, ou escrever a linha em `respostas.json`. A terceira existe porque
"olhei e deixei ficar" não deixa rastro nenhum — é indistinguível de
ninguém ter olhado.

**`respostas.json` registra decisão, não corta.** É de propósito — ele
não entra na pilha `transcript.md → anotacoes.json → montagem.json`, e
`_base` nem o lê. Mas responder `sai` e não cortar seria um caminho novo
para julgamento humano sumir em silêncio, então isso vira
`PENDENCIA NAO OBEDECIDA` no `conferir`, irmã da anotação desobedecida. A
cobrança só vale onde trecho e pergunta coincidem (`proprio`): numa
`retomada?` o trecho vai da primeira tentativa até a versão boa, e cortar
só a primeira — o que se pede — deixa a segunda tocando.

A resposta se guarda pelo **tempo** da pendência, nunca pelo número: uma
pendência do meio da lista que sai renumera todas as de baixo, e a resposta
passaria a valer para outra pergunta. Mesma regra do `s`/`e` do
`montagem.json`, e pela mesma razão.

**Tempo de prévia só vale contra o corte que gerou aquela prévia.** O
`/anotar` recebe anotações escritas ouvindo o `.m4a` (`[00:15]`, `[01:29]`)
e as traduz com `_mapa_previa`, a mesma função do `conferir --em`. Mas o
mapa é feito do corte de AGORA: se o corte mudou depois que o `.m4a` foi
gerado, a tradução erra tudo o que vem depois do ponto que mudou, e erra em
silêncio — é a família do selo do `transcript.md`, por um caminho novo. Por
isso a fase 0 do `/anotar` compara a duração do `.m4a` com o `mantido` do
`conferir` antes de aceitar qualquer tempo, e por isso **toda tradução é
feita antes da primeira correção entrar**: a primeira que entra invalida o
relógio das outras.

E o destino de cada linha colada é cobrado como o das outras duas camadas
de julgamento: recebeu 7 observações, o relatório tem 7 linhas, inclusive
"não achei". Diferente da anotação desobedecida e da pendência sem
resposta, essa cobrança **não** tem uma função que a faça — o
`work/<video>/ouvido.md` é o registro, e a conta é do relatório. Se um dia
uma observação sumir no caminho, é aqui que mora o buraco.

**A lista de pendências é curta de propósito, e custou medição ficar
assim.** Eram 268 nos 10 fluxos de `work/` (7 a 50 por vídeo); são 55 (0 a
12). Saíram três categorias, e nenhuma por gosto:

- `fala nao transcrita` (106) apontava a PALAVRA em que o áudio estava
  grudado, mas `resolve_suspeitos` já separou esse áudio num token próprio
  antes de o arquivo existir. Nos 106 casos o vizinho ou já sai (90) ou já
  é outra pendência (16). Mesma pergunta, do lado errado.
- `cauda arrastada` (76) era pergunta de geometria feita a quem lê texto.
  Virou medida: `ARRASTO SOBREVIVE` no `conferir`. Das 76, 64 deixam
  0,06-0,14s (a folga que `_respeita_palavras` devolve por construção), 1
  sai inteira, e 11 sobrevivem 100% — as 11 têm cauda de 0,25-0,30s, e o
  corte foi descartado por ser menor que `minimo` + `folga_palavra`. Mais
  3 sobram em região que o alinhador embaralhou: 14 achados, e perguntar
  as 76 escondia justamente esses.
- `disputa` (31) o próprio `/revisar` mandava não responder. E a causa que
  o docstring de `sobrepostos` afirmava estava errada: 27 das 31 têm dentro
  uma palavra cujo tempo o alinhador já recusou (a que sai com `~`), 8 são
  entre dois tokens que já saem, e a única consequência que ela previa —
  ilha curta — aparece em 3, que o `conferir` já listava.

O que essas três tinham em comum: perguntavam o que o leitor não tinha como
responder. Alarme que dispara onde ninguém pode agir gasta a atenção de
quem responderia os outros — é o mesmo defeito do alarme de "emenda em
ponto alto" e do de "frase pendurada", só que disfarçado de pergunta.

**Cabeçalho de `transcript.md` que não confere é recusa, não aviso.** O selo
no `<!-- ... selo=... -->` é a impressão digital do `stream`; sem ele ou
diferente dele, o `reconcilia` casa token errado com token errado e reimpõe
decisões antigas em silêncio. Foi assim que uma anotação do IMG_0246 morreu
(o `e` de 127,93s voltou ao vídeo). Não reintroduza um "arquivo antigo,
passa".

**O navegador não calcula corte.** Todo gesto vai ao servidor e volta com os
trechos, vindos da mesma `intervalos()` que o `render` usa. Refazer a
geometria em JS é mais rápido e mente em algum canto. Não porte
`spans_corte` para o front — nem "só para desenhar".

Isso vale até para as contas pequenas: qual trecho cada botão vai tirar ou
devolver sai do `Sessao.gestos`, e a letra dentro da palavra vira tempo no
`Sessao.selecao`. A tela manda "palavra 137, letra 4 de 8" e desenha a
resposta.

**O riscado sai dos trechos mantidos, não do `cut` do token.** É o que faz a
tela mostrar o corte de verdade — inclusive o que uma anotação tirou e o que
o snap raspou. Pintar pelo `cut` mostraria a *proposta*, e a diferença entre
as duas é exatamente onde moram os defeitos deste programa.

**Riscado cinza é regra; riscado azul é gente.** O riscado dizia *que* a
palavra sai e nunca *quem* mandou. São 41, 179 e 14 tokens nos três vídeos
de calibração — 19% do IMG_0247 — cortados pelo passe de julgamento, e até
04/09/2026 saíam com o mesmo cinza da regra automática; para saber que uma
palavra saiu por julgamento era preciso clicar nela e ler o "Por que saiu",
uma palavra de cada vez, na camada mais cara de produzir que este programa
tem. Agora elas saem em azul, com o motivo escrito no `title`. A regra de
cobertura é a MESMA de `aplica_anotacoes` — qualquer sobreposição, por
menor que seja — e não uma parecida: com um limiar de 0,05 s sobravam 3
tokens desenhados como corte mecânico depois de uma pessoa mandar cortá-los.
E o `_anot_antes` responde a mesma pergunta para o VÃO entre dois tokens,
porque uma anotação pode cair inteira no silêncio: são 22 assim no
IMG_0247, sem nenhum token onde pendurar a marca.

**O editor fala nos dois relógios, e diz quando o segundo caducou.** Ele
anota ouvindo o `.m4a` (`[01:29]`); a folha pensa em tempo de fonte, e até
04/09/2026 não mostrava tempo nenhum — achar na tela o lugar de uma
anotação pedia rodar `conferir --em` em outra janela. Agora a caixa "ir
para" aceita `1:29`, a seleção mostra o tempo dela na prévia, e a lista "o
que ouvir" corre na ordem da prévia (a da fonte fica no `title`), que é a
ordem do `conferir.txt` porque é a ordem em que se ouve. Quem traduz é o
servidor, pela `_mapa_previa` — o mapa é feito do corte, e corte não se
calcula no navegador nem "só para navegar".

O aviso que vem junto é o que fecha o buraco: **o primeiro gesto no editor
já invalida o `.m4a` do disco**, e um `[01:29]` anotado ouvindo o arquivo
velho passa a apontar para outro lugar em silêncio. `previa_no_disco`
compara a duração do arquivo com o `mantido` de agora, com a mesma folga de
0,15 s da fase 0 do `/anotar` — e é cego onde ela é cega (tirar 200 ms num
lugar e devolver 200 ms em outro passa por igual). É a fase 0 do `/anotar`
feita antes, por quem causou a diferença.

**A seta do teclado para onde há o que ouvir, não onde há letra.** Eram 16,
49 e 48 vãos com voz por vídeo fora do alcance do teclado — inclusive os
que aceitam texto, que é justamente onde ele precisa chegar para escrever o
que ouviu. O critério é o mesmo do `_corrigivel` no servidor, e pela mesma
razão. Pausa muda continua de fora: lá não há nada para ouvir nem escrever.

**Decisão de palavra caduca; trecho de tempo não.** Índice de token deixa de
valer assim que a análise insere um token antes dele. Por isso cada decisão
do `montagem.json` guarda o `s`/`e` da palavra em que foi tomada, e o
`confere_montagem` recusa em vez de avisar — mesma regra do selo do
`transcript.md`, só que por palavra. O editor guarda cópia em
`montagem-anterior.json` antes de gravar por cima.

**`correcoes.json` é sobre o que a palavra DIZ, nunca sobre se ela sai do
vídeo.** É a correção de transcrição do editor (botão direito → "corrigir
transcrição"), para a legenda que uma etapa futura vai gerar a partir do
`stream`. Só troca `t["t"]`; nunca `cut`, nunca `s`/`e`. Por isso não é
uma terceira camada de `montagem.json` — misturar "sai ou fica" com "diz
o quê" no mesmo campo é como o `edicao.json` do editor antigo esquecia
qual das duas coisas cada campo respondia. Mesma regra de caducidade do
parágrafo acima, pela mesma razão (índice de token caduca, `s`/`e` não):
`confere_correcoes` recusa em vez de avisar, e o editor guarda cópia em
`correcoes-anterior.json` antes de gravar por cima. **Não vira correção
no `transcript.md`**: aquele arquivo casa token por chave normalizada
(`chave_token`/`reconcilia`) para decidir cortar/manter, e editar o texto
ali mudaria a chave — a palavra corrigida ficaria sem par no fluxo
original e o `reconcilia` a marcaria "apagado no texto", cortando-a em
silêncio. É a mesma família de defeito que a checagem de selo do
`confere_atualidade` existe para evitar, só que por um caminho novo:
julgamento humano (aqui, ortografia) sumindo sem avisar.

**Não se corta no escuro, e agora isso é verdade.** `parece_frase` promete,
por escrito, que som alto sem texto legível não vira corte: "o trecho fica e
aparece como `[?? N.Ns]` no revisao.txt para o passe de julgamento decidir".
As passadas seguintes desmentiam a promessa — `marca_tentativas`,
`marca_ecos` e `marca_cacoetes` decidem por TEXTO, e num `??` texto é
exatamente o que não existe. Pior: o corte levava junto a PERGUNTA, porque
`pendencias` pula token cortado — o trecho sumia do vídeo e do `revisao.txt`
na mesma linha. `protege_naolidos` fecha isso, e corre DEPOIS das passadas
automáticas e ANTES de `aplica_anotacoes`: regra não corta o que ninguém
leu, gente continua cortando.

MEDIDO (04/09/2026) nos 7 fluxos de `work/`: 20 tokens `??`, UM cortado por
regra — 669,1 s do "as de espadas", 1,13 s marcado `tentativa`. Que havia
fala ali não é suposição: o wav2vec2 do `align`, decodificado LIVRE em vez
de alinhado, ouve "...sica, e isso é t...". Foi a única discordância entre
os dois reconhecedores nos 114 cortes automáticos de ≥1 s dos 7 vídeos — nos
outros 113 ambos ouvem a mesma vogal arrastada ou o mesmo silêncio. **Essa é
a medição que responde "o Whisper está comendo fala?": não está.** O modelo
é o `small`; trocar por `large-v3` custa ~15 min por vídeo em CPU e não
consertaria nenhum dos 8 achados do `/anotar` no "as de espadas", que são
todos de geometria e de regra.

A guarda também recusa desfazer `anotado:`, embora `aplica_anotacoes` corra
depois dela e o caso não possa acontecer pelo caminho normal. É de
propósito: rodá-la fora de ordem (num teste, num script de medição) sobre um
`stream.json` pronto ressuscitaria 9 trechos que uma anotação mandou cortar,
e ressuscitar julgamento humano é o mesmo defeito de apagá-lo, do outro
lado.

**A ponta do vídeo é uma lacuna, e por muito tempo foi a única que ninguém
media.** A lacuna do MEIO já media voz com `mede_voz`, já re-transcrevia
isolado com `vad_filter=False` e já protegia o que não deu para ler. A
ABERTURA (antes da 1ª palavra) saía do `cmd_analyze` com `cut=True` escrito
no braço — um `silencio` rotulado `abertura` —, e o FECHAMENTO (depois da
última) nem token tinha: era cortado lá na geometria, no passo 2 do
`spans_corte`. Nos dois, a única pergunta feita era "a lacuna passa de
`pausa_intocavel`?". Nenhuma das quatro guardas conhecidas cobria esse
caminho, porque **todas elas moram depois dessa linha**.

MEDIDO no "5 de espadas": o Whisper não transcreveu NADA nos primeiros
74,4 s, e o Rafael ouviu ali mais de um minuto de aula, com a frase "o 5 de
espadas" dentro. Sumiu do corte sem `[??]`, sem pendência, sem nada. Hoje as
duas pontas passam pelo mesmo caminho da lacuna do meio, e a introdução volta
transcrita ("Os cinco de espadas. Essa carta tem uma dor, ela tem uma cena
tão triste…").

MEDIDO (05/09/2026) nas 24 pontas dos 12 fluxos de `work/`: 9 são silêncio
puro e 13 têm a leitura recusada por `alucinou` (frase-fantasma, loop
repetido, logprob, denso demais) — nessas 22 nada muda, e **é o `alucinou`
que filtra lixo nesta escala**, não a densidade. Passam duas, as duas
aberturas: os 74,4 s do "5 de espadas", que são aula, e 8,8 s de "Eu sei." no
"6 de espadas", que provavelmente não são. As duas viram pendência numerada
(`texto reconstruido`), que o `conferir` cobra até alguém responder.

Duas regras de texto tiveram de sair do caminho, e as duas pela MESMA razão —
**foram calibradas em trecho recuperado de 1 a 2 s, e uma ponta pode ser um
pedaço inteiro de aula**:

- `classifica_fala` divide letras por DURAÇÃO, e numa ponta a duração
  carrega as pausas da aula: 74,4 s a 29 % de voz são 53 s de silêncio no
  denominador. Deu 2,4 car/s e a regra cortou a introdução como se fosse um
  "eeee". MEDIDO: essa regra já cortou 85 vezes nos 12 fluxos e NUNCA acima
  de 8,0 s (mediana 1,60 s); todos os casos acima de 4,5 s são vogal
  sustentada. Por isso texto recuperado numa ponta não passa por ela.
- `marca_ecos` cortou a mesma introdução como `retomada colapsada`. A regra
  está certa e é estreita — trecho recuperado que repete palavra vizinha —,
  só que "vizinha" é uma janela de 6 tokens (`[cacoete].eco_janela`,
  calibrada no "como se [como seja…] existisse uma tradição"), e um trecho de
  74 s tem dezenas de palavras: acertar uma das 6 seguintes é quase certo.

`protege_pontas` fecha isso, irmã de `protege_naolidos`, no mesmo lugar e com
a mesma forma — depois das passadas automáticas, antes de `aplica_anotacoes`,
com as mesmas duas exceções (`ela mandou cortar` e `anotado:` são gente, não
regra). A guarda é por TOKEN DE PONTA e não por regra de propósito: são três
passadas com o mesmo defeito (`marca_cacoetes`, `marca_tentativas`,
`marca_ecos`) e uma pergunta só. O token carrega `ponta` — campo extra, como
`ouviu`/`recusa`/`cauda`, fora do `selo_stream` (só `k`/`s`/`e`/`cut`), então
não invalida `transcript.md` nenhum.

O que NÃO mudou, e é de propósito: quando a ponta é silêncio de verdade, ou
quando a leitura é recusada, o comportamento é o de sempre, byte a byte — a
abertura continua saindo como `silencio` rotulado `abertura` e o fechamento
continua sem token, porque quem corta o rabo do vídeo é o passo 2 do
`spans_corte` e dar-lhe um token ali encurtaria a cauda de TODO vídeo em
0,39 s sem medição nenhuma pedindo isso. **Muda-se o que se decide, não a
geometria de quem já decidia certo.** MEDIDO: IMG_0246, IMG_0247 e IMG_0248
saem com o corte idêntico ao de antes — 22, 70 e 70 emendas, `conferir.txt`
igual linha por linha.

O passo 2 do `spans_corte` também teve de aprender que "depois da última
palavra não há token nenhum" deixou de ser sempre verdade: a conta de `ult`
agora soma a `hesitacao` MANTIDA, senão ela cortaria por cima da recuperação
e a fala apareceria no `transcript.md` só para sumir do vídeo na linha
seguinte. MEDIDO nos 12 fluxos analisados pela versão antiga: a conta larga
dá exatamente o mesmo número que a estreita nos 12.

**A segunda opinião sobre o tempo é do Whisper, e ela sai onde se confere.**
Os seis outros achados do `conferir` medem o corte contra os tempos do
ALINHADOR — os mesmos com que ele foi calculado. Quando é o alinhador que
erra, todos concordam com o erro e ninguém reclama, que foi como o corte do
IMG_0247 comeu "que muitas". `enterradas_pelo_whisper` é a única pergunta
feita com um relógio independente, e até 04/09/2026 ela só era feita dentro
do `_plano` — ou seja, só aparecia para quem já estava gastando minutos
gerando áudio ou vídeo. O `conferir`, o comando que a doc manda rodar ANTES
de ouvir justamente para não depender do ouvido, era o único lugar onde o
aviso não existia. MEDIDO nos 7 fluxos: dispara em TODOS os 7, 26 palavras
(2 a 7 por vídeo) — e a disputa "lâmina"/"mental," que o Rafael ouviu como
um "ou" estranho estava entre elas. Hoje a função devolve a lista, o
`_plano` a imprime e o `achados_corte` a lista como `PALAVRA ENTERRADA`; o
`d` de `achados_corte` é obrigatório de propósito, porque parâmetro opcional
ali seria uma lista de achados divergindo em silêncio da outra.

**A passada mecânica pergunta o mesmo que a tela.** `Sessao.avisos` sempre
soube dizer "essa palavra sobrou inteira", mas só para quem abre o editor;
o `conferir` só acusava sobra acima de 0,15s, e uma palavra de 0,08s
sobrevive 100% sem chegar perto disso. Hoje ele marca `INTEIRA` sempre que
o token cortado sobrevive por completo, seja qual for o tamanho — dois
casos nos 10 fluxos, um `eu` de 0,08s e um `na` de 0,06s dentro de uma
anotação.

**Ocultar palavra curta pode não ocultar nada.** `_respeita_palavras` recua
a emenda `folga_palavra` (0,06 s) de cada palavra mantida vizinha, então uma
palavra mais curta que 0,12 s fica coberta pelas duas folgas e sobrevive
inteira — medido no `de` de 100,19s do IMG_0248. Não se conserta baixando a
folga: 0,06 é o erro de fronteira medido do alinhador. O `Sessao.avisos`
pergunta e a tela diz; o conserto é a pessoa terminar com o botão de menos,
que passa por cima da geometria.

**Restaurar à mão vale para qualquer token, e por um tempo não valia para
metade deles.** A anotação de julgamento entra no corte como TRECHO DE
TEMPO (passo 4 do `spans_corte`), fora da camada de palavra, e o único
lugar que escava token mantido de dentro de um trecho de tempo é o
`_respeita_palavras`. Enquanto a lista que ele protegia fosse só
`k == "palavra"`, restaurar uma `hesitacao` ou um `silencio` dentro de
anotação era um clique que não fazia nada: o token voltava a `cut=False`,
a tela o pintava de volta, e o vídeo continuava sem ele. MEDIDO no "as de
espadas": cinco tokens seguidos restaurados dentro da mesma anotação, as
quatro palavras voltaram inteiras e a quinta — uma `hesitacao` de 6,06 s,
que o `resolve_suspeitos` fez de um bloco de áudio só — sobrou 0,06 s, e os
0,06 nem eram dela: era a folga vazando da palavra vizinha. É a família de
defeito de sempre, decisão humana sumindo em silêncio, na versão mais
difícil de notar — some pela METADE, com o mesmo gesto dando dois destinos.

Hoje o `_respeita_palavras` protege também o token cujo `why` está em
`MAO_MANTEM`. A regra é estreita de propósito: é a frase que SÓ uma pessoa
escreve ao restaurar, nunca a regra automática. MEDIDO nos três de
calibração: IMG_0246 e IMG_0248 saem idênticos; o IMG_0247 muda 63 ms e
perde uma emenda (71 → 70), e as duas diferenças são consequência direta de
dois restauros que a pessoa já tinha feito.

Isso só vale porque o editor passou a escrever o mesmo `why` que o
`aplica_montagem` escreve. As duas implementações da mesma decisão
discordavam, e era o editor que ficava com a versão velha da verdade: a
caixa "Por que saiu" mostrava `anotado: O1 ...` num token já restaurado, e
as três coisas que dependem de `MAO_MANTEM` — a defesa da geometria, a
pendência que conta como respondida à mão, o motivo na tela — só passavam a
valer depois de salvar e reabrir. `Sessao._aplica` guarda o `why` da
proposta em `_why_base` pela mesma razão que o `_t_orig` guarda o texto
original: desfazer o clique tem de devolver o motivo de antes dele.

O aviso que acompanha isso dizia o contrário do que acontecia. O
`Sessao.avisos` sempre soube a diferença entre "você ocultou e não saiu" e
"você restaurou e não voltou", mas a lista **o que ouvir** rotulava as duas
como `NÃO SAIU`, e no restauro isso é a desobediência trocada. Quem sabe
qual é qual é o servidor, que manda o `estado`; a tela escolhe a palavra e
não recalcula nada.

**O passe de julgamento julga o que ENTENDEU, não o que está marcado.** O
`revisao.txt` abre com um bloco `## PENDENCIAS`: a lista numerada de tudo
que o programa sabe que não sabe resolver (texto reconstruído, fala não
transcrita colada numa palavra, cauda arrastada, `[??]`, disputa, fala
rarefeita), cada item ancorado no texto como `[Pn]`. Isso fecha metade do
problema — a metade que o programa consegue suspeitar. A outra metade não
tem marca nenhuma e só aparece para quem sabe do que a aula trata: por isso
a fase 1 do `/revisar` lê o verbete da carta em
`material/guia-taro-de-yala.md` e escreve `work/<video>/entendimento.md`
antes de qualquer corte. Foi a falta disso que deixou um bocejo inteiro
("ai que horror", bocejado) atravessar o 7 de espadas transcrito como a
palavra inexistente "despeçoso", no meio de uma frase de texto normal.

Uma das pendências é `retomada?`: a MESMA `detecta_tentativas` rodada com
`min_ngram_aviso` (2 palavras em vez de 3), só para perguntar. Medido nos 10
fluxos de `work/`, o limiar frouxo acha 9 das retomadas que uma pessoa
anotou à mão — e mais 4 que são anáfora de propósito ("na Carta da Rainha,
por exemplo… na Carta do Rei é o Rio"). Cortar anáfora seria apagar
conteúdo em silêncio, e a diferença entre as duas coisas não está na forma,
está no sentido. Por isso o limiar frouxo **vira pergunta, nunca corte**:
0 a 4 por vídeo, cada uma com o texto citado.

`carrega_vocabulario` é a versão mecânica e fraca da mesma pergunta: junta
as palavras da própria aula, do guia do Yala e do léxico, e marca quando um
texto RECONSTRUÍDO E MANTIDO inventa palavra que não existe em lugar nenhum
do material. Medido nos 10 fluxos de `work/`: quatro alarmes no total, os
quatro verdadeiros ("despeçoso", "Disse percebe", "Núrope", "Os pardes").
Não corta nada e não pode: 3 mil palavras tiradas de um livro e de dez
vídeos não são o português.

**Gaguejada se mede pela próxima palavra AUDÍVEL, não pela próxima do
fluxo.** `proxima_audivel` pula o que já está marcado para sair, porque o
que some não separa nada para quem escuta. E a comparação é por `chave_ac`,
COM acento — sem isso "e é" vira repetição e o programa come a conjunção.
Não há piso de tamanho de palavra: houve (`len > 2`) e ele não protegia
nada, só deixava passar "de de", "no no", "um um" e "ou ou", que são
exatamente as gaguejadas que sobravam nos vídeos de calibração. Quem faz
esse trabalho são o acento e o `GAGUEJADA_MAX` (1,5 s).

A mesma pergunta é refeita no `conferir` (`REPETICAO AUDIVEL`) sobre o
corte PRONTO, e a distância ali se mede na PRÉVIA, não na fonte: metade
desses casos não existia no fluxo — foi o corte que criou a vizinhança. No
IMG_0248 a anotação de 343,7-350,4 s encosta o "que" de antes no "que" de
depois, 6,4 s de distância na fonte e 0,12 s no que se ouve.

**Testado e RECUSADO: separar fala de bocejo pela acústica.** Duas medidas
foram implementadas e jogadas fora — taxa de picos silábicos no envelope e
razão de energia de modulação em 2,5-9 Hz. Contra os 191 trechos
recuperados de `work/`, fala de verdade e vocalização sustentada se
sobrepõem por inteiro (fala: mediana 0,33 pico/sílaba e 0,48 de modulação,
p10 em 0,17 e 0,14; o bocejo do 7 de espadas cai dentro dessa faixa). Não
insista por aí: o que separa os dois é o SENTIDO do texto, não a forma do
som. O espectrograma continua útil para uma pergunta diferente — "isso é um
som sustentado ou dois pulsos?" -, e essa está no `/revisar`.

**Testado e RECUSADO: avisar quando a emenda deixa a frase pendurada** (o
trecho mantido termina em preposição ou conjunção). Medido: 13 a 19 por
vídeo, e quase todas são emenda no meio de frase, que é o normal - a pausa
foi encurtada e a frase continua do outro lado. Alarme que dispara em 20%
das emendas é papel de parede, mesmo problema do alarme de "emenda em ponto
alto do envelope" descrito no `cmd_conferir`.

**`align` é etapa separada por medição:** 72% dos fins de palavra do Whisper
caem tarde (mediana +27 ms). Cortar pelos tempos do Whisper come o "s" final.

**Menos emendas é melhor que corte mais justo.** Cada emenda é um pulo na
posição da cabeça dela. `minimo`, `ilha_minima` e `snap` existem para isso.

**Adicionar cartas** (`cmd_cartas`, `baralho.py`, `rosto.py`) é comando à
parte, não integrado ao `render`: acha menção de carta na fala
(`baralho.acha_mencoes`, mesma técnica de `corrige_frases()` — tupla de
`chave()`, a mais comprida primeiro) e gera uma prévia lo-res em
`cartas_previa.mp4` para aprovar antes de qualquer coisa virar
`config.toml` calibrado de verdade — só há vídeos de teste (IMG_0301 para
o modo padrão, IMG_0246 para `--sem-principal`), zero medição contra
edição à mão ainda. Duas menções de carta são a mesma: `baralho.catalogo()`
dá a UMA carta representante de cada posto da corte (rainha/rei/pajem/
cavaleiro/cavaleira) as chaves de categoria também, de um token só ("as
rainhas" sem naipe) — por isso "rainha" sempre vira a mesma "Rainha de
Copas", nunca uma rainha diferente a cada menção.

Dois modos, mutuamente exclusivos (`--carta` força o primeiro,
`--sem-principal` liga o segundo):

- **Padrão** (layouts 1/3): uma carta PRINCIPAL fixa à esquerda o vídeo
  inteiro (fade suave); secundárias entram à direita quando mencionadas,
  corte seco (`grafo_cartas`, `_agrupa_spans_cartas`).
- **`--sem-principal`** (layout 4, empilhamento): para vídeo que enumera
  VÁRIAS cartas ou TIPOS de carta sem nenhuma ser o tema (pedido do
  Rafael em 02/09, video-teste IMG_0246: "rainha, rei, cavaleiro,
  cavaleira, pajens" ditos em sequência). Cada menção nova alterna de
  lado e empilha sobre a anterior do mesmo lado; depois de
  `persistencia` sem menção nenhuma, as duas pilhas saem em fade — a
  direita primeiro, a esquerda por último (`grafo_cartas_enumeracao`,
  `_agrupa_cartas_enumeracao`).

**A imagem do overlay não é a imagem do catálogo.** `_catalogo_img` lê de
`baralho completo/tarodeyala-borda/` (cópia com 1px de borda preta,
gerada por `bordar_cartas.py` — rode de novo se `tarodeyala/` mudar),
nunca de `tarodeyala/`. `baralho.catalogo()` continua apontando pro
original: é dali que vem o nome de arquivo que casa mencão na fala, e
misturar as duas pastas faria decisão de estilo visual (a borda) vazar
pra decisão de casamento de texto.

**`overlay` é caro por CAMADA, não só por pixel, nesta versão do ffmpeg.**
MEDIDO no IMG_0246: uma camada por MENÇÃO (26 no vídeo de teste, sem
juntar repetição de carta) renderizou a 0,6 fps — quase 50 min para 2 min
de prévia em 360p/15fps, contra "minutos" do modo padrão (1-3 camadas).
`_agrupa_camadas_enumeracao` junta menções que caem na MESMA posição de
pilha (lado+profundidade+carta) numa camada só, com uma ocorrência (e um
fade de saída) por menção — 11 camadas no mesmo vídeo. Se `--sem-principal`
voltar a ficar lento, é aqui que se conta quantas camadas o vídeo pediu
antes de mexer em qualquer outra coisa.

## Verificar uma mudança

Mexeu no editor ou na geometria? **`venv\Scripts\python.exe
teste_editor.py`** roda nos três vídeos de calibração e checa o que quebra
calado: o mais desfaz o menos exato, ocultar a palavra tira a palavra (ou
avisa que não tirou), e o corte que a tela mostrou é byte a byte o que o
`render` relê do disco.

Mexeu na marcação ou no `revisao.txt`? **`venv\Scripts\python.exe
teste_revisao.py`**, o irmão dele para a outra metade: a gaguejada pela
próxima palavra audível (com acento, com ENFASE, com o limite de tempo), o
aviso de palavra inventada, as promessas do `revisao.txt` — toda pergunta
sem resposta automática vira pendência numerada, toda pendência está
ancorada no texto, e a lista não incha —, e o laço da resposta: a
pendência sem resposta chega ao `conferir`, a resposta casa pelo tempo
mesmo com a lista renumerada, e o arrasto que a poda não levou vira medida
(sem disparar na folga de desenho), e as duas guardas irmãs — no corte no
escuro, regra não corta `??`; na ponta do vídeo, regra de texto não desfaz a
fala recuperada, e nas duas anotação continua cortando e o trecho protegido
volta a ser pendência.
Foi este teste que achou a `[disputa]` sumindo quando caía num token
cortado. Nele mora também a promessa invertida: a pergunta sobre trecho
que **já saiu** continua sendo cobrada até alguém restaurar ou responder,
e uma vogal sustentada conta como um som e não como 200 letras.

**Rode `conferir` antes de ouvir.** Ele lê o corte pela mesma função do
`render` e lista só o suspeito: pendência sem resposta, anotação
desobedecida, fala cortada que sobrevive, ilha curta, palavra raspada,
arrasto que a poda não levou, palavra que o Whisper põe dentro do corte.
Cada achado sai no tempo da fonte
*e* no da prévia. Sem ele, defeito de geometria só aparecia quando o Rafael
ouvia — foi assim que seis defeitos chegaram juntos a ele, nenhum de
julgamento de conteúdo. `conferir <video> --em 3:05` traduz um tempo da
prévia de volta para a fonte.

Depois ouça, não renderize: `audio` sai em ~50 s (3 MB) contra 5–6 min do
`render` (286 MB) — o gargalo é decodificar 4K60 HEVC 10 bits. `render
--dry-run` mostra quanto sai sem gastar tempo. `previa` abre no mpv quando a
dúvida for visual.

Os vídeos de calibração são **IMG_0246, IMG_0247 e IMG_0248** — já julgados,
com `anotacoes.json` pronto. É contra eles que os números do `config.toml`
foram medidos; uma mudança de comportamento só está verificada depois de
passar nos três. IMG_0247 tem o caso do alinhador embaralhando tempos
(575–598 s).

## Convenções

Comentários e identificadores em português, quase sempre sem acento; acento
só onde faz parte do dado ou do texto que o usuário lê. Comentário aqui
explica **por que** o número é aquele e o que quebrou antes — não o que a
linha faz. Mantenha esse tom.

## Cuidado

**É repositório git** desde `cf6e07e`, mas `work/` está no `.gitignore`:
código tem desfazer, `anotacoes.json` e `transcript.md` não têm. Antes de
reescrever um arquivo grande, mostre o que muda em vez de aplicar direto.
