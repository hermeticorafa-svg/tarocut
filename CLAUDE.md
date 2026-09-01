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

Etapas, em ordem: `ingest → transcribe → align → analyze → draft → render`.
`run` faz ingest→draft; `lote` faz uma pasta inteira. Cada etapa tem cache;
`--force` refaz. `tarocut.bat` abre a janela (`gui.py`), que só chama esses
mesmos comandos.

## Mapa

| arquivo | o que é |
|---|---|
| `tarocut.py` | tudo: CLI, pipeline, marcação, geometria do corte, render |
| `alinhar.py` | wav2vec2 — fronteiras de palavra (o Whisper erra os tempos) |
| `editor.py` | servidor local do editor; `editor.html` é a interface |
| `gui.py` | janela Tk; só dispara comandos e mostra estado |
| `lexico.py` | gera `lexicon/terms.txt` a partir do livro do Yala |
| `config.toml` | **todos** os números do gosto de corte |
| `material/padroes-edicao.md` | medições contra os vídeos editados à mão |

Estado por vídeo em `work/<video>/`: `meta.json`, `audio.wav`, `words.json`,
`stream.json` (a fonte da verdade), `transcript.md`, `revisao.txt`,
`anotacoes.json`, `edicao.json`.

## O que não quebrar

**`config.toml` é medição, não gosto.** Cada número tem um comentário dizendo
contra o quê foi calibrado e o que aconteceu quando estava errado. Não mexa
num valor para fazer um sintoma sumir. Meça primeiro, num vídeo real, e
**atualize o comentário com a nova medição** — o comentário é o valor.

**`anotacoes.json` é sagrado.** É o passe de julgamento: caro de produzir, uma
vez por vídeo, para sempre. Nunca regenere, sobrescreva ou apague. Ele
sobrevive a `run --force` de propósito.

**Anotação que some em silêncio é o pior defeito que este programa tem.**
Já apareceu por três caminhos: o `_funde` juntando a anotação a um token e
o `_respeita_palavras` truncando o resultado; o filtro `minimo` descartando
anotação curta; e a borda encolhida pelo `encaixa_anotacoes` (esse é
proposital). Os dois primeiros estão fechados e o `conferir` vigia. Se
mexer na geometria, o teste é: **toda anotação tem que sobrar ~0 s.**

**`edicao.json` ganha do `transcript.md`** — é a decisão mais recente e mais
explícita. Tem selo do `stream`; se o vídeo for reanalisado, o `render`
**para** em vez de aplicar índices que mudaram de lugar. Não conserte isso
"aplicando mesmo assim".

**O navegador não calcula corte.** Todo clique vai ao servidor e volta com os
spans, vindos da mesma função que o `render` usa. Refazer a geometria em JS
seria mais rápido e mentiria em algum canto. Não porte `spans_corte` para o
front.

**`align` é etapa separada por medição:** 72% dos fins de palavra do Whisper
caem tarde (mediana +27 ms). Cortar pelos tempos do Whisper come o "s" final.

**Menos emendas é melhor que corte mais justo.** Cada emenda é um pulo na
posição da cabeça dela. `minimo`, `ilha_minima` e `snap` existem para isso.

## Verificar uma mudança

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

**Não é repositório git.** Não há desfazer. Antes de reescrever um arquivo
grande, mostre o que muda em vez de aplicar direto.
