# Redesenho do editor do tarocut: transcrição em cima, onda embaixo

Você vai reescrever `editor.py` + `editor.html` do tarocut. **Leia o
`CLAUDE.md` inteiro antes de tocar em código** — ele documenta a arquitetura e
uma lista longa de defeitos já corrigidos que não podem voltar. Este
documento é o complemento: o diagnóstico, a especificação e as decisões já
tomadas com o Rafael (usuário e dono do projeto). Nada aqui está em aberto
para você reabrir com ele; o que ainda for escolha sua está marcado como tal.

Trabalhe em branch novo (`git checkout -b editor-onda` a partir de
`ponta-divisor-e-pedacos`). **Não commite nada sem mostrar antes ao Rafael.**

---

## 1. O problema, nas palavras dele

> "Estou com MUITAS dificuldades de usar o editor. Ele não funciona de maneira
> precisa. Peço para restaurar alguns trechos e ele não restaura, os limites
> são invisíveis, eu nunca sei exatamente o que estou editando."

E, depois de rejeitar a proposta de manter os botões atuais e só acrescentar
uma onda de leitura:

> "esse editor está totalmente clunky [...] recomeçar o editor do zero se for
> mais fácil."

Ele **não é editor de vídeo**, se descreve como noob e odeia a tarefa. O
tarocut existe para ele não ter que editar. Isso não é desculpa para
interface simplista: é a razão de cada gesto ter que ser óbvio e de cada
promessa ter que ser cumprida sem ele conferir.

---

## 2. Diagnóstico: por que o editor atual dá essa sensação

Achados concretos, já verificados no código. Não redescubra do zero.

### 2.1. "Ocultar uma palavra" pode não ocultar nada — e isso é comum

Duas travas da geometria, ambas em `tarocut.intervalos()`, anulam o gesto:

- **`folga_palavra` = 0,06 s.** `_respeita_palavras` (tarocut.py:2547) subtrai
  do corte a região `[vizinha.s − 0,06, vizinha.e + 0,06]` de **cada palavra
  mantida vizinha**. Uma palavra oculta com fala colada dos dois lados perde
  ~60 ms de cada ponta do corte; se ela for **mais curta que ~0,12 s**, as
  duas folgas cobrem ela inteira e o corte desaparece. Medido: o `de` de
  100,19 s do IMG_0248 sobra 140 ms, ou seja, sobra todo.
- **`minimo` = 0,28 s.** O filtro `passa()` dentro de `intervalos()` descarta
  qualquer corte menor que isso (exceto onde há anotação de julgamento). Ou
  seja: **ocultar qualquer palavra com menos de 0,28 s não faz nada hoje** —
  "de", "que", "e", "um", "no", "ou". São exatamente as palavras que mais
  aparecem numa gaguejada.

Hoje o programa **avisa** ("você ocultou 'de' e 140 ms dela continuam tocando;
termine com o botão de menos" — `Sessao.avisos`, editor.py:899), num painel
lateral desconectado da palavra clicada. O aviso está correto e ninguém lê.

### 2.2. "Restaurar" tem três formas de falhar

- A mesma geometria acima, do outro lado.
- Backspace/restaurar só age sobre **tokens** (palavra, hesitação, silêncio
  ≥ 0,75 s). Uma pausa curta entre duas palavras **nunca vira token** — só é
  alcançável pelos botões `+`/`−` na borda da seleção vizinha. Selecionar a
  palavra e apertar backspace esperando que o silêncio do lado volte não faz
  nada, e nada na tela explica por quê.
- O botão `+` (`Sessao._devolve`, editor.py:529) devolve **primeiro o que a
  própria pessoa cortou antes**; só desfaz corte automático quando não sobra
  corte seu na lacuna. É correto semanticamente ("mais desfaz menos exato") e
  imprevisível para quem não sabe da regra. É provavelmente a origem do
  "peço para restaurar alguns trechos e ele não restaura".

### 2.3. Os limites são invisíveis por decisão de design, não por acidente

- A largura do `.vao` (`larguraVao`, editor.html:429) é **tipografia**: cresce
  com o tempo oculto até um teto de 4,2em para não estourar a linha. Nunca foi
  escala temporal.
- O corte "por letra" (ctrl+clique) reparte o tempo do token por
  **interpolação linear de caractere** (`pintaToken`, editor.html:469;
  `Sessao.selecao`, editor.py:682). Nunca é a posição fonética real — o
  alinhador só entrega a fronteira da palavra inteira. No caso extremo
  documentado no `CLAUDE.md`, um "éé…" de 200 caracteres para uma vogal só faz
  o texto mentir por completo sobre a estrutura do som.

### 2.4. A cobertura de 100% do áudio já existe como DADO, não como alcance

Todo instante já está contabilizado no que o servidor manda: cada token
carrega `ocultoAntes`/`vozAntes` (o vão antes dele), e o vão final vem em
`fimVoz`/`fimAnot` (`Sessao.pecas`, editor.py:806). O que falta é **alcance
direto**: para agir sobre uma pausa sem token é preciso clicar na palavra
vizinha e adivinhar qual dos dois lados/botões fala daquele silêncio. Duas
indireções que o Rafael sente como "trecho inalcançável".

---

## 3. A especificação — o que ele pediu, decidido

Tudo nesta seção está fechado. Não reabra com ele.

### 3.1. Layout

- **3/4 superiores: só a transcrição.** Nenhum botãozinho de mais/menos
  flutuando sobre o texto — eles são exatamente o que ele achou confuso.
- **1/4 inferior: a faixa de áudio (onda), fixa, sempre visível.**
- **Painel de avisos/achados/log/exportar: OCULTO.** Abre por um botão no
  **canto superior direito** da tela, por cima da transcrição. Palavras dele:
  *"não me interessa nada disso, por mim pode ser um painel oculto que aparece
  quando eu clico um botão no canto superior direito da tela"*.
  **Mas nada pode virar mudo** (ver §5.6): o botão carrega um contador
  permanente (ex.: "⚠ 3") sempre que houver pedido desobedecido ou achado, e a
  contagem em si fica visível sem abrir o painel.

### 3.2. A ligação de mão dupla — o requisito central dele

Palavras dele, textualmente:

> "as palavras e o tempo tem que ser intercambiáveis, meu objetivo é que o
> áudio saia do jeito mais preciso possível. se eu selecionar uma palavra na
> transcrição, o correspondente a ela vai aparecer na faixa de áudio. se eu
> ocultar a palavra da transcrição, o áudio some, se eu ocultar a faixa de
> áudio, o texto some. [...] eu preciso que funcione"

Isso é contrato de aceitação, não aspiração:

1. Selecionar palavra(s) no texto → a onda centra naquele trecho e marca os
   blocos correspondentes.
2. Selecionar/arrastar na onda → as palavras correspondentes ficam
   selecionadas no texto, e a folha rola até elas.
3. Ocultar palavra no texto → **o áudio some** (garantido, ver §4).
4. Ocultar trecho na onda → **o texto correspondente aparece riscado**. (Isto
   já funciona hoje por construção: o riscado sai de `vivo`, que vem de `iv`,
   e `aplica_manual` já aplica os cortes de tempo em `iv`. Não quebre.)

### 3.3. Gestos: só arrastar

Ele escolheu **"Só arrastar"**: os botões `+`/`−` e os atalhos de passo fixo
(`−`, `=`, `_`, `+`, teclas `1`–`7`) **somem de vez**; toda edição de borda
passa pela onda.

Some junto:
- o seletor de **unidade** (`UNIDADES`, editor.html:328) — ele só fazia
  sentido como passo dos botões;
- o **ctrl+clique por letra** — a onda mostra o tempo real, a interpolação por
  caractere era uma mentira sobre a estrutura do som (§2.3);
- no servidor: `Sessao.gestos`, `Sessao._devolve`, `Sessao.vizinhanca` e as
  ações `menos`/`mais`/`unidade`. Eles respondiam "o que este botão de passo
  fixo faria aqui", pergunta que deixa de existir. A prioridade "desfaz o
  próprio corte antes" também some — e é bom: arrastar nomeia o trecho, então
  `_corta`/`_restaura` com `subtrai_spans` já dão "arrastar de volta desfaz
  exato" sem heurística nenhuma.

**Fica** (não é passo fixo nem mira de mouse — decisão minha, fácil de
reverter se ele reclamar): `backspace` oculta/restaura a seleção, `ctrl+z`
desfaz, `espaço`/`enter` tocam, `o` toca o original sem o corte, setas andam
pela transcrição, `esc` limpa. A caixa "ir para" (tempo do `.m4a`) continua.

### 3.4. A onda

**Janela: fixa ao redor da seleção** (escolha dele). Concretamente:

- ~6 s centrados na seleção, no mesmo aumento, sempre.
- Roda do mouse = zoom; arrastar na régua = pan.
- Seleção maior que a janela **não encolhe a escala** — a janela rola, e as
  duas pontas da seleção ficam marcadas na borda da faixa. Uma borda de 60 ms
  tem que continuar agarrável num parágrafo de 40 s selecionado; era isso que
  a alternativa "cabe a seleção inteira" perdia (40 s em 300 px = 7 px/s, a
  borda vira menos de um pixel).

**Conteúdo da faixa, de cima para baixo:**

1. **Régua de tempo** — tempo da fonte, e o tempo da prévia (o relógio do
   `.m4a`, que é com o que ele anota). Os dois relógios continuam existindo.
2. **Faixa de palavras** — um bloco por token, alinhado ao tempo real, com o
   texto dentro quando couber. As cores são as mesmas da transcrição: tinta =
   mantido, cinza = cortado pela regra automática, **azul = cortado pelo passe
   de julgamento** (`anot`), pontilhado = voz medida sem texto lido.
3. **A onda** — amplitude. Região mantida em tinta sobre o papel, região
   cortada esmaecida/hachurada. É aqui que o riscado deixa de ser tipografia e
   vira medida: a fração `vivo` que hoje risca letra por letra é a MESMA
   fração, agora desenhada sobre o eixo de tempo real — estritamente mais fiel,
   e sem número novo vindo do servidor.
4. **Playhead** — linha andando durante `toca()`, equivalente ao que `acende()`
   já faz na transcrição. (Decidido: sim.)

**Gestos na onda:**

- Clicar num bloco de palavra → seleciona aquela palavra (igual a clicar no
  texto).
- Arrastar sobre blocos de palavra → seleciona a faixa de palavras.
- **Arrastar na onda começando em região MANTIDA** → aquele tempo sai.
- **Arrastar na onda começando em região CORTADA** → aquele tempo volta.
- **Arrastar a alça de borda** (a linha vertical onde mantido encontra
  cortado) → move aquela borda; a direção decide se corta ou restaura.
- A regra do início vale para o arrasto inteiro: começou no mantido, tudo o
  que ele cobriu sai; começou no cortado, tudo o que ele cobriu volta. Sem
  ambiguidade, e reversível pelo gesto contrário.

---

## 4. A garantia: "ocultei e sumiu", sem asterisco

**Este é o coração da tarefa.** Hoje o programa avisa que não obedeceu; a
partir de agora ele obedece.

O `montagem.json` tem duas camadas, e a diferença entre elas é o arquivo
inteiro (leia `carrega_montagem`, tarocut.py:2164, e a seção correspondente do
`CLAUDE.md`):

- **`tokens` — decisão de PALAVRA.** Passa por toda a geometria de emenda
  (fusão, snap, corte mínimo, respeito à palavra vizinha), porque quem oculta
  uma palavra quer a palavra fora, não um número de milissegundos. Carrega o
  `why` (`"oculto no editor"` / `"restaurado no editor"`), que três coisas
  leem: `_respeita_palavras` via `MAO_MANTEM` (tarocut.py:969, 2599), a caixa
  "Por que saiu" na tela, e `pendencias` (tarocut.py:3659), que marca a
  pendência como respondida à mão. Caduca quando a análise muda
  (`confere_montagem` recusa, e o editor guarda `montagem-anterior.json`).
- **`cortes`/`restauros` — trecho de TEMPO.** Entram por último
  (`aplica_manual`, tarocut.py:2350), sem snap e sem mínimo: quem pediu 1 ms
  está olhando a palavra e quer 1 ms. Não caduca.

O Rafael foi perguntado qual das duas um arrasto sobre uma palavra inteira
deveria virar. Ele recusou a pergunta: *"pra mim foda-se isso que vc está
falando, eu preciso que funcione"*. Então a decisão é técnica e é esta:

### 4.1. Entregar pela camada de palavra, conferir, e completar no tempo

1. Gesto que nomeia **palavras** (selecionar no texto e ocultar/restaurar, ou
   clicar num bloco de palavra na onda) → grava decisão de **palavra**
   (`tokens`) e passa por toda a geometria. Isso é o que mantém as emendas
   poucas e pousadas no silêncio — "menos emendas é melhor que corte mais
   justo" é valor medido do projeto.
2. **Logo em seguida, no mesmo gesto, o servidor MEDE o resultado contra a
   promessa**, usando o `iv` que ele acabou de calcular:
   - marcou para sair e sobrou algo tocando → grava `cortes` com **exatamente
     o resíduo**;
   - marcou para ficar e falta algo → grava `restauros` com exatamente o que
     falta.
3. Resíduo abaixo de **1 ms** é nada — mesmo piso que `tc._spans` já usa
   ("largura zero não é gesto"; a 48 kHz são 48 amostras). **Nenhum número
   novo entra no `config.toml` por causa disto.**
4. Gesto que nomeia **tempo** (arrastar na onda) → grava trecho de tempo,
   exato, sempre. Já é o comportamento de `_corta`/`_restaura`.
5. O que o servidor completou **aparece** — na onda (a borda desenhada é a
   real) e no painel ("a geometria deixou 0,06 s de 'de' tocando; completei no
   tempo exato"). Nada de silêncio: a família de defeito que este programa
   inteiro persegue é decisão humana sumindo sem avisar, e completar calado
   seria a versão nova dela.

### 4.2. O custo, que você tem que MEDIR e documentar

Cortar exatamente na fronteira alinhada **abre mão dos 60 ms de proteção da
palavra vizinha**, e 0,06 é o erro de fronteira MEDIDO do alinhador (72% dos
fins de palavra do Whisper caem tarde, mediana +27 ms — está escrito no
comentário de `folga_palavra` no `config.toml`). O risco simétrico é comer a
sílaba do vizinho em vez de deixar farelo do que se mandou sair.

Isso é aceitável agora **porque a onda torna o resultado visível e audível**, e
porque um arrasto conserta em um gesto. Mas não é para ser suposto: **meça**.

- Rode nos três de calibração. O **IMG_0247** é o único com `montagem.json` e
  `correcoes.json` no disco (decisões de mão de verdade) — é contra ele que a
  diferença aparece.
- Reporte: quantas emendas antes/depois, quantos segundos de material, e
  quantas palavras ganharam corte exato que antes não saíam.
- Escreva a medição no `CLAUDE.md`, no tom do arquivo: **por que** o número é
  aquele e **o que quebrou antes**. `config.toml` é medição, não gosto; se
  algum número de lá for tocado, atualize o comentário com a nova medição — o
  comentário é o valor.

### 4.3. Uma consequência a resolver

`pendencias_sem_resposta` (tarocut.py:995) conta como respondida a pendência
cujo token foi trazido de volta à mão, e só reconhece isso pelo `why` em
`MAO_MANTEM` — ou seja, só pela camada de palavra. Com a onda, "trouxe de
volta arrastando" é a mesma decisão humana e hoje **não conta**: a pendência
fica acesa para sempre.

O docstring da própria função já diz a intenção ("TRAZENDO de volta a mão um
trecho marcado para sair — a pessoa olhou aquele token e disse fica"). Estenda
o reconhecimento aos `restauros` que cobrem o trecho da pendência, meça o
efeito no `conferir` dos vídeos de `work/` e documente. Se preferir não mexer
em `tarocut.py`, a alternativa é o editor gravar também a decisão de palavra
quando o arrasto cobre a palavra inteira — mas aí vale a regra que o programa
já usa em `aplica_anotacoes`: **só quando cobre a palavra INTEIRA**, e nunca
um limiar novo inventado.

---

## 5. Invariantes que NÃO mudam

A tentação de violar cada uma fica MAIOR com interface de arrastar-na-onda.

### 5.1. O navegador não calcula corte

Todo gesto vira um POST e volta com os trechos já calculados por
`tarocut.intervalos()` — a mesma função do `render`, com o mesmo snap, a mesma
fusão de ilhas, o mesmo mínimo. **Nunca** implemente `intervalos`,
`spans_corte`, `_respeita_palavras`, snap ou fusão de ilha em JS, nem "só para
desenhar durante o arrasto".

**O que o navegador PODE fazer, para você não travar:** converter pixel ↔
tempo dentro da janela que ele mesmo desenhou (ele conhece `[t0, t1]` e a
largura em px), desenhar amplitude a partir do PCM, e mostrar um **fantasma**
da borda enquanto o mouse anda. Isso é desenho e responsividade, não
geometria.

**Protocolo do arrasto (decidido):** fantasma otimista durante o arrasto, **um
POST só ao soltar**. O valor que vale é o `iv` que volta; se o servidor
corrigir a borda, a tela reflete a correção — mesmo que ela precise "puxar de
volta" visualmente o que o mouse soltou. Nada de POST por frame.

Ações novas no `/acao`:
- `{acao:"cortar_tempo", ini, fim}` — trecho exato sai.
- `{acao:"restaurar_tempo", ini, fim}` — trecho exato volta.
A alça de borda é expressa por uma dessas duas, conforme a direção. Mantenha
`ocultar`/`restaurar`/`alterna` (agora com a garantia da §4), `corrigir`,
`desfazer`.

### 5.2. O riscado sai dos trechos MANTIDOS, nunca do `cut` do token

O que aparece cortado na tela e na onda tem que ser o que de fato não vai sair
no vídeo, depois de toda a geometria — inclusive o que uma anotação tirou e o
que o snap raspou. Pintar pelo `cut` mostraria a *proposta*, e a diferença
entre as duas é exatamente onde moram os defeitos deste programa.

### 5.3. Riscado cinza é regra, riscado azul é gente

São 41, 179 e 14 tokens nos três vídeos de calibração (19% do IMG_0247)
cortados pelo passe de julgamento. A regra de cobertura é a MESMA de
`aplica_anotacoes` — qualquer sobreposição, por menor que seja —, e o
`_anot_antes` responde a mesma pergunta para o VÃO entre dois tokens (22
assim no IMG_0247, sem nenhum token onde pendurar a marca). Isso tem que
sobreviver na transcrição **e** aparecer na faixa de palavras da onda.

### 5.4. `anotacoes.json` e `respostas.json` são sagrados

Nunca regenere, sobrescreva ou apague. `anotacoes.json` é o passe de
julgamento: caro de produzir, uma vez por vídeo, para sempre.

### 5.5. `correcoes.json` é sobre o que a palavra DIZ, nunca sobre se ela sai

Mantenha a correção de transcrição (hoje: botão direito → "corrigir
transcrição" / "escrever o que se ouve") separada do gesto de cortar, e **sem
tocar no `transcript.md`** — editar o texto lá mudaria a chave que o
`reconcilia` usa, e a palavra corrigida ficaria sem par no fluxo original.
Quem decide se um token aceita texto é o servidor (`Sessao._corrigivel`), num
lugar só.

### 5.6. Nada do que hoje alerta pode virar mudo

Continuam obrigatórios, mesmo com o painel escondido:
- `Sessao.avisos` — pedido desobedecido (agora deve ser raro: a §4 cumpre o
  pedido; o aviso passa a relatar o que o servidor completou);
- `Sessao.achados` — o que o `conferir` acharia agora, pela MESMA função
  (`tc.achados_corte`), nunca uma reimplementação;
- aviso de `montagem.json`/`correcoes.json` caducos (`Sessao.aviso`);
- aviso de `.m4a` desatualizado (`previa_no_disco`) — o primeiro gesto no
  editor já invalida o `.m4a` do disco, e um `[01:29]` anotado ouvindo o
  arquivo velho passa a apontar para outro lugar em silêncio.

Ele reclamou que o painel é confuso, não que a informação seja inútil.
Reorganize a apresentação, não apague a garantia.

### 5.7. O relógio duplo continua existindo

A caixa "ir para" (tempo do `.m4a` → palavra na fonte, usada pelo fluxo
`/anotar`) continua acessível, e a lista "o que ouvir" continua correndo na
ordem da prévia — que é a ordem em que se ouve. Quem traduz é o servidor, pela
`_mapa_previa`; o mapa é feito do corte, e corte não se calcula no navegador
nem "só para navegar".

### 5.8. `montagem-anterior.json` / `correcoes-anterior.json`

A cópia de segurança antes de descartar decisão caduca continua existindo.

---

## 6. Dicas de implementação (atalho, não requisito)

- **O PCM já está inteiro no cliente** para tocar (`carregaAudio`,
  editor.html:1031, via Web Audio API). Leia `AudioBuffer.getChannelData()`
  direto de lá para desenhar amplitude — sem endpoint novo. Pré-calcule uma
  pirâmide de picos (min/max por bucket, alguns níveis de zoom) **uma vez por
  sessão**, em vez de reamostrar os samples brutos a cada redesenho. O wav do
  IMG_0247 tem 21 MB; o do IMG_0246, 8,5 MB.
- O servidor também tem um envelope em dB pronto (`self.db`, `self.hop` =
  20 ms, vindos de `tc._base`). Serve para conferência, mas 50 pontos/s não
  chegam para zoom fino — prefira o PCM do cliente para desenhar.
- `Sessao.pecas` já entrega tudo que a faixa de palavras precisa: `s`, `e`,
  `vivo`, `txt`, `voz`, `anot`, `ocultoAntes`, `vozAntes`, `corrigivel`. O vão
  final vem em `fim`/`fimS`/`fimE`/`fimVoz`/`fimAnot`.
- `monta()`/`pinta()` hoje só repintam o token cuja assinatura mudou
  (editor.html:530). Mantenha essa disciplina: são ~900 tokens, e repintar
  tudo a cada gesto pisca e perde a rolagem. A onda é canvas — redesenhe só
  ela no gesto.
- O motor de áudio (`toca`, agendando cada trecho no relógio de amostra do Web
  Audio, com o mesmo `fade` do `config.toml`) funciona bem. Não reescreva por
  reescrever.

---

## 7. Como saber que terminou

### 7.1. Rode nos três vídeos de calibração

**IMG_0246, IMG_0247, IMG_0248** (já têm `anotacoes.json`; o IMG_0247 tem
também `montagem.json` e `correcoes.json`). Sempre pelo Python do venv:

```
venv\Scripts\python.exe teste_editor.py
venv\Scripts\python.exe teste_revisao.py
```

### 7.2. `teste_editor.py` precisa ser reescrito, não apagado

Ele testa as promessas do modelo ATUAL. Algumas deixam de fazer sentido; a
**garantia por trás de cada uma** (nunca sumir decisão em silêncio) continua
valendo e precisa de equivalente novo:

| promessa de hoje | o que vira |
|---|---|
| "mais desfaz menos, exato" (botões) | "arrastar de volta na onda desfaz exato" |
| "ocultar tira a palavra **ou avisa**" | **"ocultar tira a palavra"**, ponto — inclusive palavra de 0,08 s e de 0,20 s, que hoje não saem |
| "cursor de letra vira tempo" | o pixel da onda vira tempo, e o corte comitado vem do servidor |
| "o painel diz o mesmo que o `conferir`" | igual, pela mesma `achados_corte` |
| "o que a tela mostrou é o que o `render` relê do disco" | **igual, e é o teste mais importante do arquivo** |

Testes novos que o contrato dele exige:
- selecionar no texto e selecionar na onda produzem o MESMO trecho;
- ocultar na onda risca as palavras cobertas no texto;
- ocultar qualquer palavra (amostra dos três vídeos, incluindo as mais curtas)
  a remove de `iv` — zero exceções;
- restaurar qualquer token (palavra, hesitação, silêncio), inclusive dentro de
  anotação, o devolve inteiro.

Mantenha os que continuam valendo: voz sem texto chega marcada como voz, pausa
muda recusa texto, o riscado azul cobre exatamente os tokens que a anotação
corta, o round-trip do relógio da prévia, o `.m4a` velho vira aviso,
montagem/correções caducas viram cópia + aviso.

### 7.3. Ordem sugerida

1. Branch novo.
2. Servidor: ações novas + garantia da §4 + medição da §4.2.
3. `editor.html` do zero (licença explícita dele: "recomeçar o editor do zero
   se for mais fácil").
4. `teste_editor.py` reescrito.
5. Medições no `CLAUDE.md`, no tom do arquivo.
6. **Mostrar ao Rafael antes de commitar.**

---

## 8. Armadilhas específicas deste repositório

- **Editar arquivo por script Python exige `write_bytes`.** Os arquivos estão
  em LF no disco; no Windows, `write_text` traduz `\n` para `\r\n` e o diff
  passa a mostrar o arquivo inteiro como alterado, escondendo a mudança real.
  Aconteceu em 04/09/2026 exatamente no `editor.py` e no `editor.html`.
- **Nunca rode `analyze --force` para "conferir".** Ele não é reprodutível:
  medido em 08/09/2026 no "6 de espadas", duas rodadas seguidas da mesma
  versão do código deram cortes diferentes (trecho marginal do Whisper, com
  logprob perto do limiar, desempata para lados diferentes). Você invalidaria
  o `transcript.md` e o `montagem.json` de um vídeo já julgado sem ganhar nada.
- **`work/` está no `.gitignore`.** Código tem desfazer; `anotacoes.json` e
  `transcript.md` não têm. Antes de reescrever um arquivo grande, mostre o que
  muda em vez de aplicar direto.
- **Convenções de código:** comentários e identificadores em português, quase
  sempre sem acento; acento só onde faz parte do dado ou do texto que o
  usuário lê. Comentário aqui explica **por que** o número é aquele e o que
  quebrou antes — não o que a linha faz. `teste_editor.py` mexe no
  `montagem.json`/`correcoes.json` do vídeo e devolve os dois como estavam,
  inclusive quando falha; mantenha isso.
- **Memória do projeto desatualizada:** existe uma memória chamada
  `edita-video-sem-arrastar` dizendo que o Rafael rejeita explicitamente
  arrastar borda e quer só precisão numérica por teclado. **Isso está
  superado**: em 08/09/2026 ele escolheu "só arrastar" depois de ver a
  alternativa. O que continua valendo daquela memória é a outra metade: ele
  quer a transcrição **literal**, com o motivo do corte fora do caminho de
  quem só quer ler (mouseover ou painel), nunca inline no meio do texto.

---

## 9. Uma frase para não perder de vista

Ele não quer entender a diferença entre camada de palavra e camada de tempo, e
não deveria precisar. O trabalho é fazer as duas concordarem por construção,
para que "ocultei a palavra e ela sumiu" seja verdade sem asterisco — e para
que, quando a geometria e o pedido divergirem, quem se dobra seja a geometria,
em voz alta.
