# Cor das imagens sobrepostas no vídeo — o que foi feito, o que falhou

Log para retomar o problema do zero em outra conversa. Escrito em
09-10/09/2026. **Estado: RESOLVIDO em 10/09/2026 — converte só as
PRIMÁRIAS, nunca a curva.** A conclusão está na seção 12, no fim; o resto
do arquivo é o caminho até ela, inclusive o que deu errado.

> Aviso ao leitor: as seções 4 a 9 descrevem o estado ANTIGO do código e
> um raciocínio que a medição derrubou. Ficam como registro do caminho —
> não são instruções, e a seção 9 (onde está cada coisa) está vencida.

---

## 1. O problema original

Projeto `tarocut` (`C:\Users\marip\Videos\tarocut`), um pré-editor de vídeo
que sobrepõe imagens (cartas de tarô e fotos avulsas) sobre vídeo gravado no
iPhone.

**Sintoma relatado pelo Rafael:** as imagens sobrepostas aparecem com
saturação e contraste dramaticamente aumentados — "gritantes" — comparadas
com os arquivos originais delas. O vídeo em si (rosto, parede) aparece
correto. Visto no Reprodutor Multimídia do Windows 11, no arquivo
`3 de espadas_final.mov`.

Exigência: corrigir as imagens **sem alterar em nada** a cor do vídeo.

## 2. Ambiente

| | |
|---|---|
| SO | Windows 11 Home Single Language 10.0.26200 |
| ffmpeg | 9.0.1-full_build (Gyan, via WinGet) |
| Player onde o defeito é visto | Reprodutor Multimídia do Windows |
| Tela | SDR (presumido — não confirmado) |

**Vídeo fonte** (`3 de espadas.MOV`, iPhone):
```
codec=hevc  pix_fmt=yuv420p10le  color_range=tv
color_space=bt2020nc  color_transfer=arib-std-b67  color_primaries=bt2020
```
Ou seja: **HLG / BT.2020, HDR.** 3840x2160, 60 fps.

**Imagens sobrepostas** (JPEG sRGB):
```
carta:  829x1478  yuvj444p  color_range=pc  color_space=bt470bg  transfer=unknown  primaries=unknown
midia:  984x1379  yuvj420p  color_range=pc  color_space=bt470bg  transfer=unknown  primaries=unknown
```

**Saída** (`_corta_final` em `tarocut.py`): quando a fonte é hevc, escreve
```
-color_range tv -color_primaries bt2020 -color_trc arib-std-b67
-colorspace bt2020nc -tag:v hvc1
```

## 3. Diagnóstico (este eu considero sólido)

O grafo do ffmpeg leva a imagem assim:
```
[1:v]scale=-2:918,format=yuva420p,fade=...[carta1];
[base][carta1]overlay=x=48:y=81[...];
```

O filtro `scale` do ffmpeg converte **matriz** e **faixa**, e **não** converte
função de transferência nem primárias. Então os valores sRGB do JPEG entram no
arquivo praticamente intactos — e o arquivo inteiro sai marcado BT.2020/HLG.

**MEDIDO:** recortando a região da carta do `3 de espadas_final.mov` e
comparando com o JPEG original escalado igual, a diferença média por canal é
1,5 de 255 (o resto é croma 4:2:0 e compressão). Confirma: os valores passam
inalterados.

Consequência: um player que respeita os tags aplica a EOTF do HLG (exponencial
na metade de cima da escala) e converte BT.2020→tela sobre valores que são
sRGB. Isso levanta contraste e satura. Bate com o sintoma.

## 4. A correção que foi aplicada (e que não deu certo)

Função nova `_img_para_hlg(C, hlg)` em `tarocut.py`, que insere no ramo de
cada imagem, logo depois do `scale`:

```
,format=gbrp16,zscale=pin=bt709:tin=iec61966-2-1:min=bt709:rin=full:
 p=bt2020:t=arib-std-b67:m=bt2020nc:r=tv:npl=203
```

Mais:
- `saida_hlg(meta)` — decide "esta saída é HLG?" (`"hevc" in meta["vcodec"]`),
  usada tanto para os tags quanto para a conversão, para não divergirem.
- `npl_grafico = 203` no `config.toml`, seção `[cartas]`.
- Aplicada nas 4 entradas de imagem que existem (`grafo_cartas`: principal e
  secundária; `grafo_cartas_pilhas`: pilha e tela cheia).
- 12 casos novos em `teste_cartas.py`.

Escolha do `npl=203`: **MEDIDO** passando uma rampa sRGB pelo zscale e vendo
onde o branco (255) cai no sinal HLG:

| npl | branco vira | % do sinal |
|---|---|---|
| 100 | 161 | 63% |
| 150 | 179 | 70% |
| **203** | **192** | **75%** |
| 250 | 200 | 78% |
| 300 | 208 | 82% |
| 400 | 219 | 86% |
| 600 | 236 | 93% |
| 1000 | 255 | 100% |

Raciocínio: a ITU-R BT.2408 põe o branco difuso do HLG em 75% do sinal, e
203 nits é a luminância que a norma dá a esse branco. Daí o 203.

### Resultado real

**Rafael: "as cartas estão acinzentadas, dessaturadas."** Ou seja, corrigiu
demais, para o outro lado.

## 5. Por que a verificação não pegou isso (a falha do método)

A verificação foi feita contra um **player simulado**: eu decodifiquei o quadro
com `zscale` fazendo a conversão colorimétrica exata HLG/BT.2020 → sRGB
(`pin=bt2020:tin=arib-std-b67 → p=bt709:t=iec61966-2-1`, mesmo `npl=203`).

Isso é **circular**: usei como "olho" exatamente a inversa da transformação que
eu tinha acabado de aplicar. Por construção ela devolve o original. Os números
bonitos que saíram daí (saturação +36%→+6%, contraste +26%→−1%) só provam que
zscale é auto-consistente, não que o Reprodutor do Windows faz aquilo.

**Nunca medi o que o player de verdade faz.** É esse o buraco.

## 6. As medições que valem (valores GRAVADOS no arquivo)

Estas não dependem de simular player nenhum: é o quadro de 00:33 do
"3 de espadas" renderizado pelo pipeline real, com a região de cada imagem
recortada e comparada com o arquivo original dela.

`sat` = saturação HSV média, `luma` = Rec.709, `contr` = desvio-padrão do luma.

```
                       carta  sat / luma / contr      midia  sat / luma / contr
D nada (até 09/09)      +7.3%   -0.3%   -0.2%         +2.0%   -1.4%   -0.4%
C transf+gamut n203    -31.3%   -8.9%  -26.4%        -40.0%  -13.2%  -27.7%   <- o que está no código
C transf+gamut n400    -35.3%  +12.3%  -17.1%        -47.6%   +4.5%  -21.3%
A só transferência     -15.9%  -10.3%  -25.3%        -18.8%  -14.0%  -26.9%
A só transf. n400      -19.0%  +10.1%  -16.2%        -26.5%   +3.6%  -20.1%
B só gamut             -14.1%   +1.1%   -2.0%        -24.1%   -0.1%   -1.8%
```

(O `+7.3%` de saturação da linha D é croma 4:2:0 sangrando nos contornos
pretos da ilustração; **MEDIDO** passando a mesma carta só por 4:2:0 + h265,
sem conversão de cor nenhuma, dá +5,0%. Não tem a ver com cor.)

### O que essa tabela mostra, e que eu não tinha visto

São **dois botões independentes**, e eu mexi nos dois de uma vez:

1. **`npl` controla só o LUMA.** De 203 a 1000 o luma vai de −8,9% a +43,8%.
2. **A conversão de gamut BT.709→BT.2020 corta a SATURAÇÃO em 14–24% sozinha,
   e o `npl` nunca a devolve — aumentar `npl` piora** (−31% em 203, −45% em
   1000). Isso porque BT.2020 é um recipiente maior: a mesma cor real vira
   coordenadas numericamente menos saturadas.

O estado atual (linha C, npl=203) grava valores **31–40% menos saturados e
9–13% mais escuros** que o original. Se o player não desfizesse isso, o
resultado seria exatamente "acinzentado e dessaturado" — que é o que o Rafael
está vendo. **Confere.**

## 7. Hipótese

O Reprodutor Multimídia do Windows **não** está fazendo a conversão
colorimétrica completa que eu assumi. Provavelmente:

- não expande BT.2020 → gamut da tela (ou expande bem menos), então o corte de
  saturação da linha C fica visível;
- faz algum tone-mapping de HDR→SDR que levanta, mas menos que a inversa exata
  da HLG.

Isto é consistente com as DUAS observações juntas, que à primeira vista se
contradizem:
- sem conversão nenhuma (D), a imagem aparece **estourada** → o player levanta
  alguma coisa;
- com a conversão completa (C), aparece **apagada** → o player levanta bem
  menos do que eu tirei.

Ou seja: **a correção certa é parcial**, entre D e C. Falta descobrir quanto.

## 8. O que fazer para resolver

A medição que falta só pode ser feita nessa máquina, com esse player: **qual
transformação o Reprodutor Multimídia aplica a um vídeo HLG/BT.2020.**

Já existe um arquivo de teste pronto para isso:

```
C:\Users\marip\Videos\teste-cor-5-variantes.mov
```

6 segundos, HLG/BT.2020, mesmos tags e mesmo encoder do `exportar`. Mostra a
**mesma carta cinco vezes lado a lado**, cada uma por um caminho de cor
diferente. Da esquerda para a direita:

1. **nada** (o comportamento até 09/09 — o que aparecia estourado)
2. **atual, npl=203** (transferência + gamut — o que aparece apagado)
3. **npl=400** (transferência + gamut, mais claro)
4. **só transferência, npl=203** (sem converter gamut)
5. **só gamut** (sem converter transferência)

Abrir no Reprodutor Multimídia, tirar um print, e comparar com o JPEG original
(`baralho completo\tarodeyala-borda\3 de espadas.jpg`) aberto do lado. A que
parecer igual ao original é a resposta — e, se nenhuma parecer, as duas
vizinhas dizem para que lado ajustar.

Palpite pela tabela: a **5 (só gamut)** ou a **4 (só transferência)**, porque
cada uma mexe num botão só. A 5 é a única que quase não mexe no luma nem no
contraste (+1,1% / −2,0%), o que a torna a candidata mais provável se o
problema do player for só a curva.

### Variantes, para gerar outras

O trecho de filtro entra logo depois do `scale=-2:ALTURA` no ramo da imagem:

```
# transferência + gamut (atual)
,format=gbrp16,zscale=pin=bt709:tin=iec61966-2-1:min=bt709:rin=full:p=bt2020:t=arib-std-b67:m=bt2020nc:r=tv:npl=203

# só transferência (mantém primárias bt709)
,format=gbrp16,zscale=pin=bt709:tin=iec61966-2-1:min=bt709:rin=full:p=bt709:t=arib-std-b67:m=bt2020nc:r=tv:npl=203

# só gamut (mantém a curva sRGB)
,format=gbrp16,zscale=pin=bt709:tin=iec61966-2-1:min=bt709:rin=full:p=bt2020:t=iec61966-2-1:m=bt2020nc:r=tv
```

## 9. Onde está cada coisa no código

Tudo no working tree, **não commitado**. Último commit: `53fdfdd` (era `676992a` antes de as mídias saírem do histórico, em 05/10/2026).

| arquivo | o que mudou |
|---|---|
| `tarocut.py` | `saida_hlg()` e `_img_para_hlg()` novas (~linha 3060); `cor = _img_para_hlg(C, hlg)` inserido nos 4 ramos de imagem de `grafo_cartas` e `grafo_cartas_pilhas`; parâmetro `hlg=False` nas duas assinaturas; `hlg=saida_hlg(meta)` nas 5 chamadas; `_corta_final` passou a usar `saida_hlg(meta)` |
| `config.toml` | `npl_grafico = 203` na seção `[cartas]`, com a tabela da rampa no comentário |
| `teste_cartas.py` | casos 56–60: a conversão existe em HLG, não existe em SDR, o ramo do vídeo não muda, o npl vem do config |
| `CLAUDE.md` | seção nova depois de "A imagem do overlay não é a imagem do catálogo" |

**Para voltar tudo atrás:** `git checkout tarocut.py config.toml teste_cartas.py CLAUDE.md` — mas atenção, `editor.html` e `.claude/settings.local.json` também estão modificados no working tree e **não têm relação com isto** (já estavam assim antes).

Para desligar só a conversão sem mexer no resto, basta fazer `_img_para_hlg`
devolver `""` sempre.

## 10. O que está confirmado e o que não está

**Confirmado (medido):**
- os valores sRGB da imagem entram no arquivo intactos e saem marcados HLG/BT.2020;
- `npl` mexe só no luma; a conversão de gamut mexe na saturação e não é revertida por `npl`;
- o ramo do vídeo não muda: dos 119 linhas do grafo do "3 de espadas" mudam 2, e no quadro pronto o único pixel de vídeo diferente é a linha y=80 acima de cada imagem (o croma 4:2:0 é compartilhado entre as linhas 80 e 81 e o overlay começa em y ímpar);
- custo de render: o ramo da imagem fica 1,44–1,77x mais lento (171→97 fps na carta), mas soma ~7,6 ms por quadro no pipeline inteiro, ~3% — duas rodadas fim a fim deram 1,19x e 0,87x, ou seja dentro do ruído da máquina;
- os três testes (`teste_cartas.py`, `teste_revisao.py`, `teste_editor.py`) passam.

**Não confirmado:**
- o que o Reprodutor Multimídia do Windows realmente faz com HLG/BT.2020 — **é a pergunta que falta**;
- se a tela é SDR ou HDR, e se o Windows está com HDR ligado (isso muda tudo: com HDR ligado o player manda o HLG para a tela quase direto);
- se o mesmo defeito aparece em outros players (mpv, VLC, navegador) — vale testar, porque se a imagem parecer certa no mpv e errada só no Reprodutor, o problema é do player e não do arquivo.

## 11. Banco de teste (renderiza 1 quadro em segundos)

Existiu em
`%LOCALAPPDATA%\Temp\claude\C--Users-marip-Videos-tarocut\<sessão>\scratchpad\quadro.py`
— pode ter sido limpo. A ideia, para recriar: pegar UM quadro da fonte em
`-ss 102.72` (que é o tempo de fonte correspondente a 00:33 da prévia),
aplicar a mesma geometria de overlay do `graph_export.txt`
(`scale=1920:1080`, crop de reenquadramento, `scale=-2:918`, overlay em
`x=48:y=81` e `x=W-48-w:y=81`), e codificar com os mesmos parâmetros do
`_corta_final`. Leva segundos, contra minutos do vídeo inteiro.

Para achar o tempo de fonte de qualquer tempo da prévia:
```
venv\Scripts\python.exe tarocut.py conferir "3 de espadas" --em 0:33
```


---

## 12. A resolução (10/09/2026)

**A escolha do Rafael no `teste-cor-5-variantes.mov` foi a 5ª: só
primárias.** É a medição que faltava — a única feita com o player de
verdade, e não com um simulado.

Antes de aplicar, cada painel do teste foi identificado por medida, não
pela ordem escrita na seção 8: os 5 painéis foram recortados do arquivo e
comparados com os 5 candidatos gerados a partir do JPEG original. Casaram
um a um, e a 5ª é a única sem conversão de curva — é a única com
contraste intacto (−0,3% contra −8% a −22% das outras).

### O que ficou no código

`_img_para_hlg` devolve agora, e só quando a saída é HLG:

```
,format=gbrp16,zscale=pin=bt709:tin=iec61966-2-1:min=bt709:rin=full:
 p=bt2020:t=iec61966-2-1:m=bt2020nc:r=tv
```

`p=bt2020` converte o gamut; `t=iec61966-2-1` na entrada E na saída
significa que a curva não muda. Sem `npl`: ele só vale em HLG/PQ, então
`npl_grafico` foi apagado do `config.toml` — não mudado de lugar.

### Por que só as primárias

O HLG foi feito para ser compatível com SDR: a curva dele é próxima o
bastante da gama de uma tela comum para o sinal aparecer quase certo sem
conversão nenhuma. Um player de SDR aproveita isso — passa o valor pela
curva da tela e converte só o gamut.

Nesse caminho a imagem sRGB **já está certa de curva**. Convertê-la para
HLG antes só faz o player aplicar a curva da tela sobre valor HLG, e é
isso que escurece e achata (o "acinzentado" da seção 4). O gamut, ao
contrário, o player converte de verdade — por isso é o único que precisa
sair pré-convertido, para o estica dele cair de volta na cor original.

Isso responde a hipótese da seção 7, e responde melhor do que ela
supunha: não é uma correção "parcial, entre D e C" num eixo só. São dois
eixos, e a resposta é 100% num e 0% no outro.

A prova de que a leitura está certa é o próprio vídeo: ele é HLG/BT.2020
do iPhone e aparece CERTO nesse player. O player é o instrumento e o
vídeo é o controle.

### Verificação

Não é a simulação circular da seção 5. São os valores GRAVADOS num quadro
renderizado pelo pipeline real (o mesmo `graph_export_qsv.txt` do
"3 de espadas", o mesmo encoder e os mesmos tags do `_corta_final`),
comparados com o arquivo original de cada imagem:

```
                             carta  sat / luma / contr    cura-bh  sat / luma / contr
nada (até 09/09)               0.0%   0.0%   0.0%          0.0%    0.0%    0.0%
primárias+curva npl=203      -28.3%  -2.9% -22.9%        -39.5%  -14.9%  -30.6%
SÓ PRIMÁRIAS (agora)         -14.2%  +1.2%  -1.1%        -24.1%   -0.7%   -2.7%
```

A linha de baixo bate com a linha `B só gamut` da tabela da seção 6
(−14,1 / +1,1 / −2,0 e −24,1 / −0,1 / −1,8), medida na sessão anterior
por outro caminho. Ou seja: o pipeline produz exatamente o painel que o
Rafael escolheu.

Arquivo para conferir a olho, 8 s, mesmo trecho do print original:
`C:\Users\marip\Videos\3 de espadas_cor-so-primarias-8s.mov`.

Custo, MEDIDO: o ramo da imagem cai de 190 para 131 fps (1,45x), contra
3-7 fps do pipeline inteiro — some no ruído, como antes.

### O que continua não confirmado

Player que aplique a EOTF do HLG de verdade (tela HDR, ou Windows com HDR
ligado): lá a imagem sai para o outro lado, clara demais. Não existe
arquivo que acerte nos dois — a diferença está no player. A escolha é a
tela em que o curso é julgado e assistido.
