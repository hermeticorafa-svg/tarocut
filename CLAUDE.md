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
| `gui.py` | janela Tk; só dispara comandos e mostra estado |
| `lexico.py` | gera `lexicon/terms.txt` a partir do livro do Yala |
| `.claude/commands/revisar.md` | o passe de julgamento: o que o Claude lê e o que ele tem de conferir depois |
| `config.toml` | **todos** os números do gosto de corte |
| `material/padroes-edicao.md` | medições contra os vídeos editados à mão |

Estado por vídeo em `work/<video>/`: `meta.json`, `audio.wav`, `words.json`,
`stream.json` (a fonte da verdade), `transcript.md`, `revisao.txt`,
`anotacoes.json`.

**O editor gráfico foi removido** (01/09). Era `editor.py` + `editor.html`,
com estado em `work/<video>/edicao.json`; sai inteiro do histórico com
`git show cf6e07e:editor.py`. Vai ser refeito do zero. Os `edicao.json` que
sobraram em `work/` não são lidos por ninguém.

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

**Quando o editor voltar: o navegador não calcula corte.** Todo clique tem de
ir ao servidor e voltar com os spans, vindos da mesma função que o `render`
usa. Refazer a geometria em JS é mais rápido e mente em algum canto. Não
porte `spans_corte` para o front.

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

**É repositório git** desde `cf6e07e`, mas `work/` está no `.gitignore`:
código tem desfazer, `anotacoes.json` e `transcript.md` não têm. Antes de
reescrever um arquivo grande, mostre o que muda em vez de aplicar direto.
