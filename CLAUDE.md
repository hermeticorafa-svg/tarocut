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
| `lexico.py` | gera `lexicon/terms.txt` a partir do livro do Yala |
| `.claude/commands/revisar.md` | o passe de julgamento: o que o Claude lê e o que ele tem de conferir depois |
| `.claude/commands/preparar.md` | run + revisar + conferir + audio num comando só, pronto pro editor de texto |
| `config.toml` | **todos** os números do gosto de corte |
| `material/padroes-edicao.md` | medições contra os vídeos editados à mão |

Estado por vídeo em `work/<video>/`: `meta.json`, `audio.wav`, `words.json`,
`stream.json` (a fonte da verdade), `transcript.md`, `revisao.txt`,
`anotacoes.json`, `montagem.json`, `correcoes.json`.

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

**`anotacoes.json` é sagrado.** É o passe de julgamento: caro de produzir, uma
vez por vídeo, para sempre. Nunca regenere, sobrescreva ou apague. Ele
sobrevive a `run --force` de propósito.

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

**Ocultar palavra curta pode não ocultar nada.** `_respeita_palavras` recua
a emenda `folga_palavra` (0,06 s) de cada palavra mantida vizinha, então uma
palavra mais curta que 0,12 s fica coberta pelas duas folgas e sobrevive
inteira — medido no `de` de 100,19s do IMG_0248. Não se conserta baixando a
folga: 0,06 é o erro de fronteira medido do alinhador. O `Sessao.avisos`
pergunta e a tela diz; o conserto é a pessoa terminar com o botão de menos,
que passa por cima da geometria.

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

**Rode `conferir` antes de ouvir.** Ele lê o corte pela mesma função do
`render` e lista só o suspeito: anotação desobedecida, fala cortada que
sobrevive, ilha curta, palavra raspada. Cada achado sai no tempo da fonte
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
