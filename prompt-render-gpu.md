# Fazer o `render` do tarocut usar a GPU e parar de trabalhar à toa

Você vai mexer no caminho de exportação de vídeo do tarocut. **Leia o
`CLAUDE.md` inteiro antes de tocar em código** — ele documenta a arquitetura e
uma lista longa de defeitos já corrigidos que não podem voltar. Este documento
é o complemento: o diagnóstico já feito, com todas as medições, para você não
refazer nenhuma.

Rafael é o usuário e dono do projeto. Ele **não é editor de vídeo**, se
descreve como noob e odeia a tarefa. Ele delegou o encanamento
explicitamente. Não pergunte a ele sobre camada interna, pixel format ou
estrutura de filtro — decida você e mostre o resultado medido.

**Escreva para ele em português, em frases curtas.** Ele já reclamou, mais de
uma vez, de resposta em parágrafo denso e cheio de referência interna.
Explique uma coisa de cada vez, com o número do lado.

Branch atual: `editor-onda`. **Não commite nada sem mostrar antes ao Rafael.**

---

## 1. O problema, nas palavras dele

> "parece que chegamos a um gargalo no que diz respeito à exportação de
> vídeos. eu sinceramente não consigo entender o pq ficou tão lento. [...] no
> meu outro computador que é 12 anos mais velho que esse, eu tenho um editor
> de vídeo que eu mesmo criei em OPENRNDR e ele exporta vídeos a 1080p em
> tempos realistas. [...] pq não podemos fazer, nós que nem temos interface
> gráfica e estamos rodando direto no ffmpeg?"

Ele está certo, e há duas causas independentes. As duas foram medidas em
09/09/2026 e estão abaixo.

---

## 2. A máquina e o material

| | |
|---|---|
| CPU | Intel Core 3 100U — 6 núcleos, 8 threads, classe 15 W |
| GPU | Intel Graphics integrada (QSV disponível) |
| ffmpeg | 9.0.1 (build Gyan, via winget) |
| fonte de teste | `C:\Users\marip\Videos\IMG_0247.MOV` |
| formato da fonte | HEVC Main 10, 3840x2160, 60000/1001 fps, yuv420p10le, ~60,8 Mbps |
| duração da fonte | 677,3 s — 40.493 quadros |
| corte de teste | `work/IMG_0247/cuts.json` — 70 trechos, 291,7 s mantidos |
| perfil | `[render].perfil = "1080p30"`, `qualidade = 24`, `gpu = true` |

`ffmpeg -hwaccels` lista `qsv`. `hevc_qsv`, `h264_qsv` e `av1_qsv` existem no
build.

---

## 3. Problema 1: o `render` nunca usou a GPU, nem uma vez

### O que acontece

`_corta_final` (tarocut.py:5421) tenta QSV primeiro e cai para software se
falhar:

```python
if cr["gpu"]:
    r = subprocess.run(_cmd(["-hwaccel", "qsv"], g_hw))
    if r.returncode != 0:
        print(f"[{rotulo}] o caminho QSV falhou; refazendo em software")
if r is None or r.returncode != 0:
    r = subprocess.run(_cmd([], g_sw))
```

A tentativa QSV **falha sempre**, em ~1,5 s:

```
[vost#0:0/hevc_qsv] Task finished with error code: -22 (Invalid argument)
[out#0/mov] Nothing was written into output file
```

### A causa

`cmd_render` (tarocut.py:5511) passa o **mesmo** grafo como `g_hw` e `g_sw`:

```python
_corta_final(d, C, meta, gscript, gscript, ["-i", str(meta["src"])],
             saida, "render", len(iv))
```

E `grafo()` (tarocut.py:2808) monta `scale=-2:1080`, um filtro de software. No
ffmpeg 9, `-hwaccel qsv` entrega quadro em memória de GPU e **não** insere
`hwdownload` sozinho. Erro reproduzido isolado:

```
Impossible to convert between the formats supported by the filter
'graph -1 input from stream 0:0' and the filter 'auto_scale_0'
  src: qsv    dst: yuv420p yuyv422 rgb24 ...
```

Ou seja: o caminho `hw` do `render` nunca existiu. O `grafo_cartas`
(tarocut.py:3001) **tem** o padrão certo (`hw=True` → `scale_qsv` +
`hwdownload,format=p010le`); o `grafo()` nunca ganhou o equivalente.

Consequência: toda exportação roda **decode em software E `libx265 -preset
medium`**.

### Quanto custa (medido)

| | velocidade | arquivo inteiro (677,3 s) |
|---|---|---|
| decode software | 0,614x (37-40 fps) | ~1110 s |
| decode QSV | 4,71x em fatia / 5,1x no arquivo | **132,4 s** |

O encoder `hevc_qsv` funciona sozinho: testado em 8 bits, `p010le`,
`yuv420p10le`, com as tags de cor bt2020/arib-std-b67/hvc1, e com `h264_qsv`.
Nenhum falhou. **O problema nunca foi o encoder; foi o pixel format na entrada
do grafo.**

---

## 4. Problema 2: o grafo de 70 ramos custa mais que tudo o resto junto

Este é o achado maior, e não tem nada a ver com GPU.

`_grafo_corte` (tarocut.py:2789) monta **uma linha por trecho mantido**:

```python
for i, (a, b) in enumerate(iv):
    L.append(f"[0:v]trim=start={a}:end={b},setpts=PTS-STARTPTS[v{i}];")
```

Com 70 trechos, o ffmpeg insere um `split` de 70 saídas. **Cada um dos 40.493
quadros da fonte é entregue aos 70 filtros `trim`**; um fica com ele, 69
descartam.

### Medido, tudo já rodando na GPU, vídeo inteiro

| | tempo |
|---|---|
| só decode QSV do arquivo inteiro | **132,4 s** |
| decode + os 70 `trim`/`concat` + `scale_qsv`, **sem encode nenhum** | **558,7 s** |
| render completo (com `hevc_qsv`) | **511,5 s** |

O encode é praticamente de graça. **O grafo é ~75% do tempo.** (Que a linha
sem encode tenha dado mais que a com encode é variação de execução; as duas
dizem a mesma coisa.)

---

## 5. O placar de hoje

Tudo no IMG_0247. Onde diz "medido" foi `Measure-Command`.

| caminho | 10 trechos (49,9 s de saída) | 70 trechos (291,7 s) | corte muda? |
|---|---|---|---|
| como está hoje (software + libx265 medium) | ~480 s *(aproximado, por timestamp de arquivo)* | ~34 min *(estimado)* | — |
| só ligando a GPU (`scale_qsv` + `hevc_qsv`) | **95,6 s** *(medido)* | **511,5 s** *(medido)* | **NÃO** |
| GPU + uma entrada por trecho (`-ss`/`-t` antes de cada `-i`) | **59,5 s** *(medido)* | não medido | **SIM** — ver 6.2 |
| GPU + um `select` só | ~43 s *(só vídeo, sem encode)* | não medido | **SIM** — ver 6.2 |

A estimativa de ~34 min vem de: decode 677,3 s a 0,614x = 1110 s, mais encode
de 8.751 quadros a ~9,4 fps (taxa inferida da corrida de 10 trechos) = 931 s.

### Ligar a GPU não move o corte — medido

Mesmo grafo (`trim`/`concat`), 10 primeiros trechos, um em software e outro na
GPU:

| | quadros | vídeo | áudio |
|---|---|---|---|
| software + libx265 (hoje) | 1498 | 49,954 s | 49,954 s |
| GPU + `scale_qsv` + `hevc_qsv` | **1498** | **49,954 s** | **49,954 s** |

Idêntico. É por construção — a seleção de quadro acontece no `trim`/`concat`,
antes do `scale`, e o `scale` é a única coisa que muda. Mas estava medido, e
tem de continuar sendo medido a cada mexida.

**O README.md:232 e o CLAUDE.md dizem que o render leva "5–6 min". Está
velho — corrija junto com a mudança.**

### A saída não muda de espec

| | codec | perfil | tamanho | pix_fmt | fps | arquivo |
|---|---|---|---|---|---|---|
| libx265 (hoje) | hevc | Main 10 | 1920x1080 | yuv420p10le | 30/1 | 15,0 MB |
| hevc_qsv | hevc | Main 10 | 1920x1080 | yuv420p10le | 30/1 | 16,7 MB |

---

## 6. O que fazer, e a ordem

### 6.1. Ligar a GPU no `render`

Dar ao `grafo()` o mesmo parâmetro `hw` que o `grafo_cartas` já tem, e fazer
`cmd_render` gerar os dois arquivos, como o `cmd_cartas` faz. Com
`perfil = "1080p30"` o caminho hw é `scale_qsv=w=-1:h=1080` e o quadro nem
precisa descer para a memória normal — o `hevc_qsv` aceita quadro de GPU
direto. **Não ponha `hwdownload` onde não precisa**: baixar quadro 4K 10 bits
(~24 MB cada) é justamente o que fez a prévia do IMG_0301 levar 14 min, e está
documentado no `grafo_cartas`.

Com `perfil = "igual"` não há `scale` nenhum: o grafo de corte puro já
funciona com quadro de GPU.

A chamada do ffmpeg precisa de `-hwaccel qsv -hwaccel_output_format qsv`.
`-hwaccel` sozinho não basta: sem `-hwaccel_output_format` o ffmpeg 9 ainda
entrega quadro de GPU, e o grafo de software quebra.

### 6.2. Matar o split de 70 ramos — as duas saídas óbvias foram MEDIDAS e as duas MOVEM O CORTE

**Não implemente nenhuma das duas sem resolver o que está descrito aqui.** As
duas foram testadas em 09/09/2026 nos 10 primeiros trechos do IMG_0247,
comparando `framemd5` quadro a quadro contra o corte de hoje.

**Opção A — uma entrada por trecho** (`-ss X -t D -i FONTE`, 70 vezes, com
`-hwaccel` repetido antes de cada `-i`). Rápida: 59,5 s contra 95,6 s nos
mesmos 10 trechos. **Mas muda o corte, e descasa o áudio:**

| | quadros | vídeo | áudio |
|---|---|---|---|
| corte de hoje | 1498 | 49,954 s | 49,954 s |
| opção A | **1500** | **50,000 s** | **49,983 s** |

Dois quadros a mais em dez trechos, e vídeo e áudio já divergindo 17 ms. Em 70
trechos isso cresce. É a família de defeito que este projeto existe para
impedir.

**Opção B — um `select` só com as 70 faixas** (`select='between(t,a1,b1)+...',
setpts=N/FRAME_RATE/TB`). **Não funciona, e a causa é estrutural.**

Medido: 1494 quadros contra 1498 do corte de hoje. A razão é que **a fonte não
tem taxa constante** — 40.493 quadros em 677,3 s = 59,78 quadros/s, com
`r_frame_rate=60000/1001` declarado e `time_base=1/600`. O `trim`+`concat` de
hoje preserva a duração real de cada trecho; `setpts=N/FRAME_RATE/TB` impõe uma
taxa constante inventada, e o vídeo escorrega do áudio ao longo do arquivo.
Nenhum ajuste da expressão conserta isso: o problema é o `setpts`, não o
`select`.

De passagem, medido: `trim=start=a:end=b` **não** se comporta como `a <= t < b`
nas bordas exatas. Há quadros caindo em cima da borda (`time_base=1/600` faz
qualquer tempo de 3 casas cair exato: 0,100 s, 44,510 s, 181,320 s nos 10
primeiros trechos). Nesses pontos `trim` e `between` decidem por comparação de
float e acertam por sorte — os dois deram 2985 quadros num teste onde a regra
pura `a<=t<b` dá 2984 e `a<=t<=b` dá 2987. **Não conte com equivalência de
regra nas bordas; compare `framemd5`.**

**O que sobra.** Nenhuma das duas serve como está. Se for atacar o split, o
requisito é: reproduzir o corte de hoje quadro a quadro *e* manter
vídeo e áudio na mesma duração. Ideias não testadas, em ordem de promessa:

1. Opção A com `-accurate_seek` explícito e `-copyts`, investigando de onde
   vêm os 2 quadros e os 17 ms antes de aceitar qualquer coisa.
2. Cortar em dois passos: extrair os 70 trechos e concatenar pelo demuxer
   `concat`. Custa reencodar duas vezes ou casar parâmetros na unha.
3. Deixar o split como está. **Ligar só a GPU já resolve de 34 min para
   8,5 min, e isso é seguro** (ver 6.1 e a tabela da seção 5).

O áudio, em qualquer caminho: o custo medido está no vídeo, e o `afade` de cada
emenda (`[audio].fade = 0,010`) não se faz num `aselect` só. Se o vídeo deixar
de usar `concat`, `concat=n=70:v=1:a=1` deixa de servir para os dois, e o
casamento entre um ramo de vídeo contínuo e 70 ramos de áudio vira problema
novo — **que é onde os 17 ms da opção A apareceram.**

### 6.3. Onde a mudança tem de morar

`_grafo_corte` é chamada por `grafo()` **e** por `grafo_cartas()` /
`grafo_cartas_pilhas()`. O docstring dela diz por quê:

> "as duas nunca podem discordar de onde cai a emenda, e ter dois lugares que
> montam isso é como ter dois lugares que decidem [corte] — só se descobre
> desalinhado tarde, ouvindo."

**Não escreva um segundo caminho de corte para o modo rápido.** Se o `select`
não servir ao caminho das cartas (o `overlay` precisa de quadro em memória
normal), a resposta é uma função que sirva aos dois, não duas funções.

---

## 7. Já testado e RECUSADO — não repita

- **`-hwaccel qsv` sem `-hwaccel_output_format`, com filtro de software.**
  É o bug de hoje. Erro: "Impossible to convert between the formats".
- **`-hwaccel` uma vez só antes de vários `-i`.** É opção **de entrada**: só a
  entrada 0 vem em quadro de GPU, e o `concat` recusa misturar. Na opção A tem
  de vir repetida antes de cada `-i`.
- **Baixar quadro 4K 10 bits para escalar em software.** Testado nesta sessão
  (decode QSV → `-hwaccel_output_format p010le` → `scale` de software →
  `hevc_qsv`): funciona e é lento. Já estava medido e documentado no
  `grafo_cartas` (14 min contra ~1 min no IMG_0301). Escale na GPU **antes**
  de baixar, ou não baixe.
- **Culpar o encoder.** `hevc_qsv` foi testado isolado em cinco combinações
  (8 bits, p010le, yuv420p10le, tags de cor, h264_qsv) e passou em todas.
- **Trocar os 70 `trim` por um `select` só.** Medido, quebra: a fonte é de taxa
  variável e o `setpts=N/FRAME_RATE/TB` que o `select` exige impõe taxa
  constante. Ver 6.2.
- **Uma entrada por trecho, como está.** Medido, move o corte em 2 quadros e
  descasa o áudio em 17 ms já em 10 trechos. Ver 6.2.
- **Presumir que `trim` e `between` são a mesma regra nas bordas.** Não são, e
  nenhum dos dois é a regra pura. Compare `framemd5`, não contagem.
- **Comparar por contagem de quadros.** Contagem igual não prova vídeo igual: o
  `select` e o `trim` deram 2985 quadros os dois num teste, e o vídeo era
  diferente. Use `-f framemd5` e compare posição por posição.

---

## 8. Como verificar que não quebrou nada

1. **O corte não pode mudar.** `cuts.json` e o `conferir.txt` dos três vídeos
   de calibração (IMG_0246, IMG_0247, IMG_0248) têm de sair **iguais linha por
   linha** ao que sai hoje. Gere antes de mexer e guarde.
2. **Compare o vídeo, não só o log.** Extraia os mesmos instantes do arquivo
   velho e do novo e compare os quadros. É a única prova de que a borda não
   escorregou. Uma emenda deslocada não aparece em nenhum log.
3. `venv\Scripts\python.exe teste_editor.py` — o corte que a tela mostra tem
   de continuar sendo byte a byte o que o `render` relê do disco.
4. `venv\Scripts\python.exe teste_cartas.py` — o caminho das cartas usa a
   mesma `_grafo_corte`.
5. **Meça e escreva o número.** O `config.toml` e o `CLAUDE.md` deste projeto
   guardam a medição junto com o valor. Se a exportação passar a levar X,
   escreva X e contra o que foi medido.

---

## 9. Onde estão as coisas

| | |
|---|---|
| `_grafo_corte` | tarocut.py:2789 — as 70 linhas de `trim` |
| `grafo` | tarocut.py:2808 — falta o parâmetro `hw` |
| `grafo_cartas` | tarocut.py:3001 — **o padrão a copiar** (`hw=True`) |
| `_previa_lores` | tarocut.py:5200 — já tenta QSV com grafo `hw` de verdade; confira se ainda funciona, não presuma |
| `_corta_final` | tarocut.py:5421 — a queda silenciosa para software |
| `cmd_render` | tarocut.py:5489 — passa `gscript` duas vezes (a causa) |

Sintaxe de grafo em arquivo neste ffmpeg: `-/filter_complex arquivo.txt`
(o `-filter_complex_script` saiu no ffmpeg 9).
