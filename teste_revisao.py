#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""teste_revisao.py - as promessas do passe de julgamento que quebram calado.

Irmao do `teste_editor.py`, para a outra metade do programa: o que decide
o que SAI lendo o texto, e o que conta ao passe de julgamento o que ele
tem de olhar. Um erro aqui nao aparece em lugar nenhum - o corte continua
saindo, so que com um bocejo dentro, ou com uma pendencia a menos na
lista que alguem deveria ter respondido.

    venv\\Scripts\\python.exe teste_revisao.py

Nao escreve nada em `work/` a nao ser o proprio `revisao.txt`, que e
derivado e se refaz com `tarocut.py revisar`.
"""

import argparse
import io
import contextlib
import json
import sys
from pathlib import Path

import tarocut as tc

CALIBRACAO = ["IMG_0246", "IMG_0247", "IMG_0248"]

falhas = []


def diz(ok, txt):
    print(("  ok    " if ok else "  FALHA ") + txt)
    if not ok:
        falhas.append(txt)


def tok(k, t, s, e, cut=False, why=""):
    return {"k": k, "t": t, "s": s, "e": e, "cut": cut, "why": why}


def teste_gaguejada():
    """A gaguejada e a unica marca automatica que decide pelo que se OUVE,
    e nao pelo que esta escrito no fluxo. Cada linha aqui e um caso que ja
    escapou de verdade nos videos de calibracao."""
    print("\n== gaguejada (marca_cacoetes, 2a passada)")
    sempre, contextual = set(), set()
    cc = tc.cfg()["cacoete"]

    def corta(fluxo):
        tc.marca_cacoetes(fluxo, sempre, contextual, cc)
        return [i for i, t in enumerate(fluxo) if t["cut"]]

    # 1. vizinhas imediatas, palavra de 2 letras: era o caso do `len > 2`,
    #    que deixava "de de" (7 de espadas, 179,1s) passar inteiro.
    f = [tok("palavra", "de", 1.0, 1.2), tok("palavra", "de", 1.21, 1.4),
         tok("palavra", "receber", 1.4, 1.9)]
    diz(corta(f) == [0], "'de de' colado: corta o primeiro, deixa o segundo")

    # 2. separadas por hesitacao JA cortada: para quem escuta sao vizinhas.
    #    Era o "no ... no" do IMG_0248 (488,0s).
    f = [tok("palavra", "no", 1.0, 1.2),
         tok("hesitacao", "hesitação", 1.2, 1.6, cut=True),
         tok("palavra", "no", 1.6, 1.8)]
    diz(corta(f) == [0, 1], "gaguejada atraves de hesitacao cortada: corta")

    # 3. separadas por uma pausa MANTIDA: nao sao vizinhas no corte.
    f = [tok("palavra", "no", 1.0, 1.2),
         tok("silencio", "", 1.2, 1.6, cut=False),
         tok("palavra", "no", 1.6, 1.8)]
    diz(corta(f) == [], "pausa mantida no meio: nao e gaguejada")

    # 4. acento separa duas palavras diferentes. Sem `chave_ac` o programa
    #    comia a conjuncao de "e é importante".
    f = [tok("palavra", "e", 1.0, 1.1), tok("palavra", "é", 1.11, 1.3),
         tok("palavra", "importante", 1.3, 1.9)]
    diz(corta(f) == [], "'e é' nao e repeticao (chave COM acento)")

    # 5. ENFASE: repetir de proposito e figura de linguagem.
    f = [tok("palavra", "não", 1.0, 1.2), tok("palavra", "não", 1.21, 1.4)]
    diz(corta(f) == [], "'nao nao' e enfase, nao gagueira")

    # 6. longe demais no tempo: sao duas frases, nao uma gaguejada. Era o
    #    par de "e" do IMG_0247 (249,6s e 262,8s), que so fica vizinho
    #    DEPOIS do corte - e ai quem pergunta e o `conferir`.
    f = [tok("palavra", "que", 1.0, 1.2),
         tok("silencio", "", 1.2, 9.0, cut=True),
         tok("palavra", "que", 9.0, 9.2)]
    diz(corta(f) == [1], "acima de cacoete.gaguejada_max nao e gaguejada")

    # 7. a palavra seguinte ja esta cortada por outro motivo: nao sobra
    #    vizinha nenhuma, entao nao ha o que colar.
    f = [tok("palavra", "de", 1.0, 1.2),
         tok("palavra", "de", 1.21, 1.4, cut=True, why="anotado: x")]
    diz(corta(f) == [1], "proxima ja cortada: nao corta a primeira tambem")


def teste_vocabulario():
    """O aviso de palavra inventada. Nao corta nada - so aponta -, mas se
    parar de apontar, para de apontar em silencio."""
    print("\n== vocabulario (carrega_vocabulario / fora_do_vocabulario)")
    d = tc.WORK / "IMG_0247"
    if not (d / "words.json").exists():
        diz(False, "IMG_0247 sem words.json - nao da para conferir")
        return
    v = tc.carrega_vocabulario(d)
    diz(len(v) > 2000, f"vocabulario carregado ({len(v)} palavras)")
    diz(tc.fora_do_vocabulario("despeçoso", v) == ["despeçoso"],
        "'despeçoso' (o bocejo do 7 de espadas) e apontado")
    diz(tc.fora_do_vocabulario("as cartas da corte representam", v) == [],
        "fala normal do proprio video nao e apontada")
    diz(tc.fora_do_vocabulario("arcano rainha naipe", v) == [],
        "vocabulario do guia do Yala conta como conhecido")
    diz(tc.fora_do_vocabulario("Ah! e o", v) == [],
        "palavra de 1-3 letras nunca vira aviso")


def teste_pendencias(video):
    """Toda pergunta sem resposta automatica tem de chegar ao passe de
    julgamento como um item numerado. Se uma some da lista, ninguem
    percebe: o revisao.txt continua saindo, so que menor."""
    d = tc.WORK / video
    if not (d / "stream.json").exists():
        return
    print(f"\n== pendencias | {video}")
    a = argparse.Namespace(video=video)
    with contextlib.redirect_stdout(io.StringIO()):
        tc.cmd_revisar(a)
    txt = (d / "revisao.txt").read_text(encoding="utf-8")
    st = json.load(open(d / "stream.json", encoding="utf-8"))["stream"]

    # o que DEVIA estar la: todo token mantido que so uma pessoa resolve
    esperado = 0
    for t in st:
        if t["cut"]:
            continue
        if t["k"] == "hesitacao":
            esperado += 1
        elif t["k"] == "palavra" and t["why"].startswith(
                ("cauda arrastada", "duracao impossivel")):
            esperado += 1
    listadas = [l for l in txt.splitlines() if l.startswith("P")
                and l[1:2].isdigit()]
    diz(len(listadas) >= esperado,
        f"toda hesitacao/cauda/duracao mantida virou pendencia "
        f"({len(listadas)} listadas, {esperado} exigidas)")

    # cada pendencia tem de estar ancorada no texto corrido, senao a lista
    # diz o que olhar e nao diz onde
    orfas = [l.split()[0] for l in listadas
             if f"[{l.split()[0]}]" not in txt]
    diz(not orfas, f"toda pendencia ancorada no texto ({len(orfas)} orfas)")

    # texto reconstruido vai INTEIRO: truncar era esconder a prova
    longos = [t["t"] for t in st if t["k"] == "hesitacao" and not t["cut"]
              and len(str(t["t"])) > 14 and not str(t["t"]).startswith("??")]
    diz(all(x in txt for x in longos),
        f"texto reconstruido sai sem truncar ({len(longos)} caso(s))")


def teste_repeticao_audivel(video):
    """O `conferir` tem de ver a gagueira que o CORTE criou - a que nao
    existia no fluxo e so aparece quando duas frases ficam vizinhas."""
    d = tc.WORK / video
    if not (d / "stream.json").exists():
        return
    print(f"\n== repeticao audivel no corte | {video}")
    with contextlib.redirect_stdout(io.StringIO()):
        C, _, stream, dur, db, hop, anot, mont, _ = tc._base(video, "teste")
        iv = tc.intervalos(stream, C, dur, db, hop, anot, mont)
    _, para_previa = tc._mapa_previa(iv)
    vivas = [t for t in stream if not t["cut"] and t["k"] == "palavra"
             and tc.fim_util(t) > t["s"]
             and sum(max(0.0, min(tc.fim_util(t), b) - max(t["s"], a))
                     for a, b in iv) > (tc.fim_util(t) - t["s"]) * 0.5]
    achados = []
    for x, y in zip(vivas, vivas[1:]):
        k = tc.chave_ac(x["t"])
        if not k or k != tc.chave_ac(y["t"]) or tc.chave(x["t"]) in tc.ENFASE:
            continue
        if (para_previa(y["s"]) - para_previa(tc.fim_util(x))
                <= C["cacoete"]["gaguejada_max"]):
            achados.append((x["s"], x["t"]))
    a = argparse.Namespace(video=video, em=None, ignorar_transcript=False)
    with contextlib.redirect_stdout(io.StringIO()):
        tc.cmd_conferir(a)
    # so as linhas de achado: a legenda do topo tambem diz o nome do
    # alarme, e conta-la fazia este teste passar com um achado a mais.
    rel = (d / "conferir.txt").read_text(encoding="utf-8")
    n = sum(1 for l in rel.splitlines()
            if "REPETICAO AUDIVEL" in l and not l.startswith("#"))
    diz(n == len(achados),
        f"o conferir lista todas as repeticoes que sobram "
        f"({n} no relatorio, {len(achados)} no corte)")


def main():
    alvos = sys.argv[1:] or CALIBRACAO
    teste_gaguejada()
    teste_vocabulario()
    for v in alvos:
        teste_pendencias(v)
        teste_repeticao_audivel(v)
    print("\n" + ("tudo ok" if not falhas
                  else f"{len(falhas)} FALHA(S):\n  " + "\n  ".join(falhas)))
    sys.exit(1 if falhas else 0)


if __name__ == "__main__":
    main()
