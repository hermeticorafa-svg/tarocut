# tarocut — estado das correcoes (01/09, revisao completa)

As correcoes de 1 a 8 estao todas FEITAS. Este arquivo virou registro do que
foi feito e por que, mais o que sobrou para o OUVIDO decidir.

## O que quebrava, em uma frase

Quase nada era erro de julgamento de conteudo. Era geometria de corte — e o
pior caso era **anotacao de julgamento sumindo em silencio**: o passe podia
estar perfeito e o audio sair errado do mesmo jeito. Foram encontrados TRES
caminhos por onde isso acontecia, dois deles nunca mapeados.

## Correcoes 1, 2, 3, 4

Feitas antes desta revisao. A 4 (`RE_NUM` protegendo lixo) esta em
`protege_enumeracoes` e `classe_num`: numeros inequivocos, `fortes_min`,
`gap_max`, e `prot` fora da poda de cauda. Medida: IMG_0247 e IMG_0248 dao 0
runs, IMG_0246 mantem a contagem de verdade em 192.58.

## Correcao 5 — a emenda nao tinha margem  [FEITA]

`folga_palavra` foi de 0,02 para **0,06**, e `[silencio].margem` — a unica
chave morta do config, nunca lida por ninguem — foi apagada. Era o mesmo
0,06, no lugar errado.

A medicao antiga que justificava 0,02 se enganava: contava invasao contra os
tempos do ALINHADOR, os mesmos com que o corte e calculado. Nunca poderia dar
outra coisa senao zero. O erro esta no proprio tempo (mediana +27 ms nos fins
de palavra do Whisper; 60 ms no inicio de "leitura" em 271,94s).

Custo medido, contra 0,02: IMG_0246 +0,5s / 22 para 22 emendas; IMG_0247
+3,6s / 74 para 71; IMG_0248 +2,4s / 71 para 69. **Melhora nos dois eixos** —
mais margem e menos emendas. 0,12 ja devolve 12s de fala cortada de proposito.

Testada e RECUSADA uma folga maior so nas palavras com `align_rejeitado`
("deriva"/"ordem", 4% delas): de 0,06 a 0,30 rendeu 1-2s e uma emenda nos
tres videos somados. Nao paga um segundo botao.

## Correcao 6 — emenda dentro de voz continua  [RESOLVIDA PELA 5]

Nao virou codigo proprio, e o motivo esta medido. O alarme proposto ("emenda
pousada em ponto alto do envelope") foi implementado e jogado fora: com o
piso no percentil 5 e o gatilho em 25 dB acima, disparava em **51 das 74**
emendas do IMG_0247 e **51 das 71** do IMG_0248. E esperado — a emenda
encosta na palavra mantida por construcao, e ali sempre ha voz. Alarme que
dispara em 70% das emendas e papel de parede.

O sintoma real (fim de "consulente" engolido) era falta de margem, nao erro
de snap. A folga de 0,06 ataca a causa. O registro fica no docstring do
`cmd_conferir` para ninguem tentar de novo.

## Correcao 7 — fala sobreposta a palavra mantida  [FEITA]

`_respeita_palavras` agora **SUBTRAI** a palavra do corte em vez de encolher
o corte para um lado so — logo um corte pode sair de la partido em dois.

Era o caminho no 1 de anotacao sumindo. No IMG_0247 o token "Mae! Mais do..."
(49.806-53.078, ela falando com outra pessoa) atravessa a palavra "do"
(50.640-50.896). O corte travava em 50.620 e **2,33s de fala alheia
continuavam tocando**. E havia uma anotacao (50.90-53.23) pedindo exatamente
aquele trecho: o `_funde` a tinha juntado ao token, entao ela morreu junto,
sem uma linha de aviso.

Medido: IMG_0247 vai de 2,33s para 0,30s ali, e de 2,46s para 0,30s em
49,81s. Custo: +2 emendas, pago de volta com juros pela correcao 5.

## Correcao 8 — nao havia como conferir sem ouvir  [FEITA]

Comando novo: `tarocut.py conferir <video>`. Le o corte pela MESMA funcao do
`render` (nao pelo `graph_audio.txt`, que seria o corte anterior) e lista so
o suspeito, cada achado no tempo da fonte E no da previa:

- anotacao desobedecida (sobra > 0,15s) — o alarme mais importante;
- fala marcada para sair que continua tocando;
- ilha mantida abaixo de `ilha_minima`;
- palavra mantida raspada pela borda (rede de regressao: tem de dar zero).

`conferir <video> --em 3:05` faz o caminho inverso: traduz um tempo da previa
para o da fonte. Era o trabalho manual de duas rodadas inteiras de revisao.

Saida hoje: IMG_0246 **4** pontos, IMG_0247 **15**, IMG_0248 **17** — contra
22, 71 e 69 emendas. Acionavel, nao papel de parede.

---

# Achados NOVOS desta revisao (nao estavam mapeados)

## A. Corte de largura zero estilhacando o trecho mantido  [FEITA]

O `edicao.json` do IMG_0246 tinha CINCO cortes de largura 5e-15 —
`[37.19, 37.190000000000005]`, ruido de ponto flutuante, nao clique de
ninguem. `aplica_manual` parte o trecho mantido em cada corte que recebe,
entao eles picaram "Os Arcanos" em **cinco cacos de 0,10s**, cada um com fade
de 10 ms na entrada e na saida. Metralhadora.

Vinham do `subtrai` do `editor.html`, que nao tinha guarda de epsilon (o
`funde` ao lado tinha). Corrigido nos dois lados: `_spans` no `tarocut.py` e
`subtrai` no `editor.html` agora exigem 1 ms — o mesmo limite que o
`aplica_manual` ja usava na saida.

Medido: IMG_0246 caiu de **27 para 22 emendas** e de 6 para 1 ilha curta, sem
custo nenhum. O `edicao.json` na pasta nao foi tocado; os cortes ruins sao
filtrados na leitura.

## B. Anotacao curta descartada pelo `minimo`  [FEITA]

Caminho no 3 de anotacao sumindo. Um "eee" de 0,35s anotado vira corte de
0,35s, perde 0,06 de folga de cada lado e sai com 0,23 — abaixo de
`minimo = 0.28`. Era lido, aplicado e descartado sem aviso.

O `aplica_manual` ja sabia que isso era errado ("nada aqui passa por snap,
fusao ou minimo — seria desfazer, no escuro, o ajuste que a pessoa fez"). A
anotacao e a mesma decisao humana escrita noutro arquivo. Agora corte que
encosta em anotacao e isento do `minimo`. Verificado: nenhuma das 19
anotacoes atuais do IMG_0247 mudou de resultado (todas ja passavam); uma
anotacao de teste de 0,42s, que antes nao fazia nada, agora tira o cacoete.

## C. `audio` e `render` passavam o nome curto ao ffmpeg  [FEITA]

`audio IMG_0247` calculava o corte inteiro e morria em "No such file" — o
ffmpeg recebia `a.video` em vez de `meta["src"]`. O `previa` ao lado ja fazia
certo. Corrigido nos dois.

## D. IMG_0248 estava bloqueado

`transcript.md` era de uma analise anterior, entao `audio`, `render`,
`previa` e `conferir` abortavam. Um terco da base de calibracao estava
invisivel. Regenerado com `draft`; o antigo esta em
`transcript.md.antes-revisao`.

---

# O que sobrou para o ouvido

## 1. Ouvir `IMG_0247_previa_revisao.m4a` (04:45)

E a primeira previa com as correcoes todas. Os 15 pontos do `conferir` sao o
roteiro; os tempos de previa ja estao impressos.

## 2. Tres anotacoes do IMG_0247 viraram redundantes — MEDIDO

Eram curativo de bug, nao julgamento de conteudo. Com o codigo corrigido,
apagar cada uma muda o corte em **0,00s, 0,12s e 0,11s**:

| anotacao | era curativo de |
|---|---|
| 79.50-86.50 | correcao 4 (o "Aaas" de 80,0s) |
| 578.14-604.05 | zona do alinhador embaralhado |
| 50.90-53.23 | correcao 7 — a propria anotacao diz isso no motivo |

`anotacoes.json` e sagrado: **nao foram apagadas.** Decisao do Rafael. Manter
nao custa nada; o incomodo e so que o arquivo passa a mentir sobre o motivo.

## 3. `minimo = 0.28` e uma escolha de gosto que agora tem numero

O residuo que sobra hoje sao hesitacoes de 0,28-0,40s ("voz sem texto") cujo
corte, encolhido pela folga, cai abaixo do minimo. Medido, com folga 0,06:

| `minimo` | IMG_0247 | IMG_0248 |
|---|---|---|
| 0,28 (hoje) | 71 emendas, 3,4s de cacoete sobrando | 69 emendas, 5,7s |
| 0,22 | 75 emendas, 1,9s | 73 emendas, 4,2s |
| 0,18 | 75 emendas, 1,9s | 79 emendas, 2,3s |

Trocar ~3s de "eee" audivel por 6-10 emendas a mais. **Nao foi mexido** — o
comentario do config diz "nao baixe muito", e isso e ouvido, nao medicao. O
`conferir` lista essas hesitacoes uma a uma, entao da para mata-las por
anotacao sem mexer no numero (ver achado B, que fez isso funcionar).

## 4. O caso que geometria nao resolve

IMG_0247, 437.18-440.87 ("Por exemplo, por exemplo, em Yala"): a palavra
"Naip" comeca DENTRO do token de junk — os dois se sobrepoem de verdade, sem
um conter o outro. Partir nao resolve. Sobram 0,63s. O `conferir` reclama
disso toda vez; a saida e anotacao.

## 5. A skill `revisar`  [FEITA — ver "O laco fechado" no fim]

---

# Regras da casa

- **`config.toml` e medicao, nao gosto.** Cada numero tem comentario dizendo
  contra o que foi calibrado. Medir num video real e atualizar o comentario.
- **`anotacoes.json` e sagrado.** Nunca regenerar, sobrescrever ou apagar.
- **E repositorio git** desde `cf6e07e`. Os arquivos `.antes-revisao` de
  codigo foram apagados: `git show cf6e07e:<arquivo>` devolve qualquer um.
  `work/` continua fora do git — la nao ha desfazer.
- **`conferir` antes de ouvir; `audio` antes de renderizar.**
- **Videos de calibracao: IMG_0246, IMG_0247, IMG_0248.** Uma mudanca so esta
  verificada depois de passar nos tres. Os tres estao alinhados, analisados e
  conferiveis com o codigo atual.
- **Teste que nao pode falhar:** toda anotacao tem de sobrar ~0s no
  `conferir`.

---

# Revisao lean (01/09, depois desta)

## Editor removido

`editor.py`, `editor.html` e a camada `edicao.json` do `tarocut.py`
(`_spans`, `carrega_edicao`, `aplica_edicao`, `aplica_manual`) sairam
inteiros — vai ser refeito do zero. Estao em `git show cf6e07e:editor.py`.

Verificado contra os tres videos de calibracao: IMG_0247 e IMG_0248 dao
`conferir.txt` **byte a byte identico** ao de antes. IMG_0246 tinha o unico
`edicao.json` com conteudo, e era ruido — um corte de 0,35s e um restauro
colado nele, que se anulavam; continua com **22 emendas** e 00:02:07.

A flag `--ignorar-edicao` virou `--ignorar-transcript`, que e o que ela
sempre fez de verdade.

## E. `confere_atualidade` deixava passar cabecalho sem selo  [FEITA]

Quarto caminho de anotacao sumindo em silencio, e o mais bobo: havia um
`return True` comentado como "arquivo antigo, sem selo".

O `transcript.md` do IMG_0246 era de uma versao anterior do programa (382
tokens no cabecalho contra 383 no `stream.json`). Passava batido, e o
`difflib` do `reconcilia` casava o `e` de 127,934-128,034 — cortado pela
anotacao 116,46-128,03 — com o `e` seguinte, que fica. O corte devolvia ao
video 0,18s de um trecho que uma pessoa mandou sair, e o `conferir` gritava
`ANOTACAO NAO OBEDECIDA` sem ninguem saber de onde vinha.

Agora cabecalho sem selo, ou com selo diferente, e **recusa**. O
`transcript.md` do IMG_0246 foi regerado (o unico `⟨ ⟩` que ele tinha
apontava para um token que a analise atual nem tem mais). Resultado: o
`ANOTACAO NAO OBEDECIDA` do IMG_0246 **sumiu**, mesmas 22 emendas.

## F. `_clip.wav` ficava para tras  [FEITA]

O `unlink` acontecia antes de `resolve_suspeitos`, que chama `escuta` de
novo. Sobrava um `_clip.wav` em toda pasta de trabalho.

## G. `cadeias` gravava indice que ja tinha mudado de lugar  [FEITA]

`detecta_tentativas` guarda `ini`/`fim` como indices no fluxo;
`resolve_suspeitos` insere tokens depois disso. Ninguem lia esses dois
campos (o `relatorio` usa texto e tempo), mas quem lesse leria errado.
Deixaram de ser gravados no `stream.json`.

## H. `conferir --ignorar-edicao` nao fazia nada  [FEITA]

A flag era lida pelo argparse e nunca chegava ao `_base`.

## Codigo morto tirado

- `calibra.py` — script de calibracao solto, sem referencia em lugar nenhum,
  com caminho de bash cravado. `git show cf6e07e:calibra.py`.
- `RE_SO_NUM` no `tarocut.py`, `cc` nao usado em `spans_corte`.
- `cmd_render` reimplementava o `_loudnorm` que ja existia ao lado, e
  chamava `cfg()` de novo tendo o `C` na mao.
- `alinhar.py`: imports `sys`, `unicodedata`, `Path`; parametro `folga` do
  `blocos`; `torch.set_num_threads(torch.get_num_threads())`.
- `lexico.py` lia o guia inteiro do disco para escrever um comentario com a
  contagem de caracteres.

## A janela ganhou `2. Conferir corte`

O `conferir` nao tinha caminho na GUI, e o README manda roda-lo sempre
depois do julgamento.

---

# O laco fechado (01/09)

O passe de julgamento nao tinha como saber se tinha sido obedecido. Fechado
dos dois lados.

## Lado 1: a skill virou arquivo

`.claude/commands/revisar.md`. Antes ela vinha registrada na sessao do
Claude Code e nao dava para ler nem editar — o que e um problema em si:
nao se conserta o que nao se ve. Agora esta no git, com o resto.

O que ela passou a fazer, alem de julgar:

- roda `conferir` DEPOIS de escrever o `anotacoes.json`, e o criterio de
  terminar e binario: **zero linhas `ANOTACAO NAO OBEDECIDA`**;
- quando sobra, tem uma tabela de diagnostico (borda no meio de palavra,
  palavra mantida dentro do trecho, regiao com `~` do alinhador) e ajusta;
- para na terceira rodada e reporta, em vez de girar em falso;
- `anotacoes.json` continua sagrado: copia antes de escrever, nunca apaga
  entrada nem reescreve `motivo`, e so mexe em `ini`/`fim` de anotacao
  antiga quando o `conferir` provar que ela nao foi obedecida;
- achado de codigo vai no relatorio, **nunca dentro do campo `motivo`** —
  era o segundo item pendente, e o exemplo esta escrito la (as duas
  anotacoes do IMG_0247 que sao diagnostico em prosa).

Tambem diz para nao ancorar borda de corte em palavra marcada com `~`, e
para mirar dentro de um `[-N.Ns]`, que e o unico ponto do `revisao.txt`
onde a borda nao atravessa palavra nem come a vizinha.

## Lado 2: o programa passou a perguntar sozinho

A conta "quanto desta anotacao continua no video" morava dentro do
`cmd_conferir`. Virou `anotacoes_desobedecidas`, e agora e feita tambem no
`_plano` — o caminho por onde `render`, `audio` e `previa` passam. Ou seja,
o aviso sai mesmo de quem nunca rodou `conferir`.

Nao aborta, avisa: ha residuo que nao da para consertar por anotacao (o caso
437.18-440.87 do IMG_0247, tokens que se sobrepoem de verdade), e abortar ali
so impediria um render legitimo.

Procedimento se pula; pergunta no caminho de todo mundo, nao.

**Medido:** os tres videos de calibracao dao zero `ANOTACAO NAO OBEDECIDA`,
e o `conferir.txt` dos tres continua identico ao de antes do refactor. Teste
positivo com anotacao inventada sobre fala mantida: o aviso sai no `render`
com o tempo, a sobra e o motivo.
