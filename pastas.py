#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Onde mora o que o tarocut ENTREGA: a previa que se ouve, a previa com
cartas, o video final e o .srt dele.

Moram em `export/`, dentro da pasta dos brutos, e nunca soltos junto deles.
A pasta dos brutos e a LISTA DE ENTRADA da janela e do `lote`: ate
25/09/2026 o `audio` e o `exportar` gravavam ali, e o
`2 de espadas_final.mov` aparecia na janela como mais um video a analisar -
o `lote` o teria processado como bruto. Saida nao e entrada.

Mora num modulo a parte, e nao no tarocut.py, porque a janela precisa da
MESMA resposta e nao pode importar o tarocut. MEDIDO: aberta como o
`tarocut.bat` abre (`pythonw`, sem console), ela tem `sys.stdout = None`, e
o import do tarocut morre com AttributeError na linha que acerta o encoding
da saida - a janela nao abriria, sem uma linha dizendo por que. Um segundo
lugar decidindo o mesmo caminho seria a janela olhando uma pasta e o
programa gravando noutra.

`work/` continua sendo do programa: o que ele le e reescreve. Aqui e o que
se abre para ver, ouvir ou mandar para frente.
"""
from pathlib import Path

EXPORT = "export"
PREVIAS = "prévias"     # para julgar o corte
FINAIS = "finais"       # o que vai para o curso

# Cada saida: em que pasta cai, e o que se soma ao nome do bruto. O .srt nao
# esta aqui porque nao decide nada: sai ao lado do video dele, com o mesmo
# nome, que e o que faz o player carrega-lo sozinho.
AUDIO = (PREVIAS, "_previa.m4a")                   # `audio`
PREVIA_CARTAS = (PREVIAS, "_previa_cartas.mp4")    # `exportar --previa`
FINAL = (FINAIS, "_final.mov")                     # `exportar`
CORTADO = (FINAIS, "_cortado.mov")                 # `render`


def pasta(brutos, tipo):
    """A subpasta `tipo` (PREVIAS ou FINAIS) do export/ de uma pasta de
    brutos."""
    return Path(brutos) / EXPORT / tipo


def onde(src, nome, saida):
    """Onde cai a `saida` (AUDIO, FINAL...) do video `nome`, cujo bruto e
    `src`. Nao cria nada: quem grava cria a pasta.

    Pela pasta do BRUTO, e nao por uma pasta fixa: era o que ja valia quando
    tudo saia solto ao lado dele, e e o que a janela ve - ela lista uma
    pasta de brutos e procura o export/ dentro dela."""
    tipo, sufixo = saida
    return pasta(Path(src).parent, tipo) / f"{nome}{sufixo}"
