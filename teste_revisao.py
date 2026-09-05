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

    # o que DEVIA estar la: todo token RECUPERADO que sobrou no fluxo -
    # e a leitura de menor confianca do arquivo, e quem decide e quem le.
    #
    # `cauda arrastada` e `duracao impossivel` ja estiveram nesta conta e
    # sairam por medicao (ver `pendencias`): a primeira virou medida no
    # conferir, a segunda repetia o que o token vizinho ja dizia. Se
    # voltarem, voltam com numero novo - nao com este teste.
    esperado = sum(1 for t in st if not t["cut"] and t["k"] == "hesitacao")
    listadas = [l for l in txt.splitlines() if l.startswith("P")
                and l[1:2].isdigit()]
    diz(len(listadas) >= esperado,
        f"todo texto recuperado mantido virou pendencia "
        f"({len(listadas)} listadas, {esperado} exigidas)")

    # e o que NAO devia: a lista existe para ser respondida inteira, e
    # cada item que ninguem consegue responder gasta a atencao de quem
    # responde os outros.
    diz(len(listadas) <= esperado + 6,
        f"a lista nao inchou ({len(listadas)} itens para {esperado} "
        f"trechos recuperados)")

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


def teste_respostas():
    """O laco que a auditoria deixou aberto: pendencia sem resposta tem de
    chegar a quem gera o audio. Sao contas puras, entao vao em fluxo de
    mentira - o que quebra aqui e a regra, nao o video."""
    print("\n== respostas (pendencias_sem_resposta)")
    q = {"n": 1, "i": 0, "rot": "som alto sem texto", "desc": "1.8s",
         "s": 223.35, "x": 223.35, "y": 225.12}
    inteiro = [(0.0, 900.0)]

    sem, orfas = tc.pendencias_sem_resposta([q], [], inteiro)
    diz(len(sem) == 1, "pendencia sem resposta nenhuma e cobrada")

    r = [{"em": [223.35], "resposta": "fica", "nota": ""}]
    sem, orfas = tc.pendencias_sem_resposta([q], r, inteiro)
    diz(not sem and not orfas, "resposta escrita fecha a pendencia")

    # o tempo vem copiado do revisao.txt, que imprime com duas casas
    r = [{"em": [223.36], "resposta": "fica", "nota": ""}]
    sem, _ = tc.pendencias_sem_resposta([q], r, inteiro)
    diz(not sem, "arredondamento de 0,01s ainda casa")

    # ...mas nao a ponto de uma resposta cobrir a pendencia vizinha: nos
    # 10 fluxos de work/ o menor vao entre duas ancoras diferentes e 0,13s
    r = [{"em": [223.48], "resposta": "fica", "nota": ""}]
    sem, orfas = tc.pendencias_sem_resposta([q], r, inteiro)
    diz(len(sem) == 1 and len(orfas) == 1,
        "resposta 0,13s fora nao casa, e aparece como orfa")

    # cortar o trecho e responder: a pendencia deixa de existir no video
    sem, _ = tc.pendencias_sem_resposta([q], [], [(0.0, 100.0)])
    diz(not sem, "pendencia que o corte levou embora nao e cobrada")

    # a resposta se guarda pelo TEMPO, nao pelo numero: renumerar a lista
    # (uma pendencia do meio sai) nao pode transferir resposta de uma
    # pergunta para outra - e a mesma armadilha do indice de token.
    q2 = dict(q, n=7, s=500.0, x=500.0, y=501.0)
    sem, _ = tc.pendencias_sem_resposta(
        [dict(q, n=1), dict(q2, n=2)],
        [{"em": [223.35], "resposta": "fica", "nota": ""}], inteiro)
    diz([x["s"] for x in sem] == [500.0],
        "resposta segue o tempo quando a lista e renumerada")

    # dizer "sai" nao corta nada: o respostas.json registra decisao, quem
    # executa e a anotacao. Deixar isso passar como "respondida" seria um
    # caminho novo para julgamento humano sumir em silencio.
    r = [{"em": [223.35], "resposta": "sai", "nota": ""}]
    sem, _ = tc.pendencias_sem_resposta([dict(q, proprio=True)], r, inteiro)
    diz([x["falta"] for x in sem] == ["PENDENCIA NAO OBEDECIDA"],
        "'sai' com o trecho ainda tocando vira PENDENCIA NAO OBEDECIDA")
    sem, _ = tc.pendencias_sem_resposta(
        [dict(q, proprio=True)], r, [(0.0, 100.0)])
    diz(not sem, "'sai' com o trecho fora do corte fecha a pendencia")

    # ...mas so onde trecho e pergunta coincidem. Na `retomada?` o trecho
    # vai da primeira tentativa ate a versao boa, que FICA: cobrar ali
    # seria reclamar de quem obedeceu.
    sem, _ = tc.pendencias_sem_resposta([dict(q, rot="retomada?")], r, inteiro)
    diz(not sem, "'sai' numa retomada nao cobra o trecho inteiro")

    # manter a mao e resposta; e "resgatado" NAO e manter a mao (ver
    # MAO_MANTEM: `texto_token` imprime toda hesitacao mantida entre ⟨ ⟩)
    sem, _ = tc.pendencias_sem_resposta([dict(q, mao=True)], [], inteiro)
    diz(not sem, "trecho restaurado no editor ja e resposta")
    diz("resgatado" not in tc.MAO_MANTEM,
        "'resgatado' nao conta como decisao de mao")

    # A pendencia `cortada` corre ao contrario de todas as outras: ela
    # nasce sobre trecho que JA SAI, entao o corte nao responde nada. Se a
    # conta nao invertesse junto, ela nasceria e sumiria da cobranca no
    # mesmo instante - a pergunta mais silenciosa que este programa
    # poderia ter.
    c = dict(q, rot="texto nao explica o tempo", cortada=True, proprio=True,
             s=42.45, x=42.45, y=62.99)
    sem, _ = tc.pendencias_sem_resposta([c], [], [(0.0, 30.0)])
    diz([x["falta"] for x in sem] == ["PENDENCIA SEM RESPOSTA"],
        "pergunta sobre trecho ja cortado continua sendo cobrada")
    sem, _ = tc.pendencias_sem_resposta([c], [], inteiro)
    diz(not sem, "restaurar o trecho responde a pergunta")
    r = [{"em": [42.45], "resposta": "sai", "nota": ""}]
    sem, _ = tc.pendencias_sem_resposta([c], r, [(0.0, 30.0)])
    diz(not sem, "'sai' com o trecho ja fora fecha a pergunta")
    r = [{"em": [42.45], "resposta": "fica", "nota": ""}]
    sem, _ = tc.pendencias_sem_resposta([c], r, [(0.0, 30.0)])
    diz([x["falta"] for x in sem] == ["PENDENCIA NAO OBEDECIDA"],
        "'fica' com o trecho ainda cortado vira PENDENCIA NAO OBEDECIDA")


def teste_texto_explica_o_tempo():
    """Corte automatico cujo TEMPO o texto que o justificou nao explica.

    O caso real: no "as de espadas" um corte de 27,5s saiu inteiro sem
    pendencia nenhuma porque tinha texto - so que o texto era "éé..."
    repetido 200 vezes. Contando aquilo como 200 letras faladas, 20,5s de
    uma vogal so davam ritmo de fala normal e nada tinha o que estranhar.
    """
    print("\n== texto que nao explica o tempo (pendencias)")
    C = tc.cfg()
    # ritmo_mediano precisa de 20 palavras: um fundo de fala normal, a
    # 0,08 s/letra, que e a mediana medida nos videos de calibracao.
    fundo = [tok("palavra", "palavra", 100.0 + i, 100.56 + i)
             for i in range(24)]

    def pend_de(t):
        return [q["rot"] for q in tc.pendencias(fundo + [t], C, set())]

    vogal = tok("palavra", "é" * 200, 42.45, 62.99, True, "alongamento")
    diz("texto nao explica o tempo" in pend_de(vogal),
        "20,5s de uma vogal so viram pergunta, mesmo com 200 letras")

    # a mesma vogal, curta, e cacoete de verdade: 7 dos 8 alongamentos dos
    # 7 fluxos de work/ tem menos de 1,2s e nenhum deles pode entrar aqui.
    diz("texto nao explica o tempo" not in
        pend_de(dict(vogal, e=43.35)),
        "alongamento curto continua sendo so alongamento")

    # decisao acustica nao entra: ela ja mediu o tempo por conta propria.
    diz("texto nao explica o tempo" not in
        pend_de(dict(vogal, why="voz sem texto 20.5s", t="hesitação")),
        "corte decidido pela acustica nao e cobrado por texto")

    # nem decisao de gente: cortar 20s de fala e direito de quem ouviu.
    diz("texto nao explica o tempo" not in
        pend_de(dict(vogal, why="anotado: ela se perde aqui")),
        "anotacao humana nao vira pergunta")

    # e o texto que DA conta do tempo nao vira pergunta nenhuma: 20,5s de
    # uma tentativa longa transcrita inteira sao 20,5s explicados.
    diz("texto nao explica o tempo" not in
        pend_de(dict(vogal, t="palavra " * 32, why="tentativa")),
        "texto que explica o tempo nao vira pergunta")

    # `letras_uma_vez` e o que separa os dois casos, e nao pode ser
    # `letras_faladas`: aquela conta as 200 letras e devolve fala fluente.
    diz(tc.letras_uma_vez("é" * 200) == 1 and tc.letras_faladas("é" * 200) == 200,
        "uma vogal sustentada conta como um som, nao como 200 letras")


def teste_respostas_no_disco(video):
    """A pergunta tem de ser feita no caminho por onde todo mundo passa -
    nao num procedimento que alguem precisa lembrar."""
    d = tc.WORK / video
    if not (d / "stream.json").exists():
        return
    print(f"\n== pendencia cobrada pelo conferir | {video}")
    with contextlib.redirect_stdout(io.StringIO()):
        C, _, stream, dur, db, hop, anot, mont, _ = tc._base(video, "teste")
        iv = tc.intervalos(stream, C, dur, db, hop, anot, mont)
        pend = tc.pendencias(stream, C, tc.carrega_vocabulario(d))
        sem, _ = tc.pendencias_sem_resposta(pend, tc.carrega_respostas(d), iv)
        a = argparse.Namespace(video=video, em=None, ignorar_transcript=False)
        tc.cmd_conferir(a)
    rel = (d / "conferir.txt").read_text(encoding="utf-8")
    n = sum(1 for l in rel.splitlines()
            if "PENDENCIA SEM RESPOSTA" in l and not l.startswith("#"))
    diz(n == len(sem),
        f"o conferir cobra todas as pendencias em aberto ({n} no "
        f"relatorio, {len(sem)} em aberto)")


def teste_arrasto(video):
    """`cauda arrastada` deixou de ser pergunta e virou medida. Se a medida
    parar de sair, ninguem descobre: o corte continua saindo, so que com o
    arrasto inteiro dentro."""
    d = tc.WORK / video
    if not (d / "stream.json").exists():
        return
    print(f"\n== arrasto que sobra | {video}")
    with contextlib.redirect_stdout(io.StringIO()):
        C, _, stream, dur, db, hop, anot, mont, _ = tc._base(video, "teste")
        iv = tc.intervalos(stream, C, dur, db, hop, anot, mont)
    sobrando = []
    for t in stream:
        if t["cut"] or not t.get("cauda"):
            continue
        ca, cb = t["cauda"]
        vivo = sum(max(0.0, min(cb, b) - max(ca, a)) for a, b in iv)
        if vivo > 0.15:
            sobrando.append((t["s"], t["t"], vivo))
    rel = (d / "conferir.txt").read_text(encoding="utf-8")
    n = sum(1 for l in rel.splitlines()
            if "ARRASTO SOBREVIVE" in l and not l.startswith("#"))
    diz(n == len(sobrando),
        f"o conferir mede o arrasto que a poda nao levou "
        f"({n} no relatorio, {len(sobrando)} no corte)")
    # a poda que COUBE nao pode aparecer: se aparecesse, o alarme seria
    # papel de parede - 64 das 76 caudas dos 10 fluxos deixam 0,06-0,14s,
    # que e a folga que `_respeita_palavras` devolve por construcao.
    diz(len(sobrando) <= 5,
        f"o alarme nao dispara na folga de desenho ({len(sobrando)} de "
        f"{sum(1 for t in stream if t.get('cauda') and not t['cut'])} "
        f"caudas)")


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


def teste_naolido():
    """`??` e o programa dizendo que nao sabe o que foi dito ali - e um
    trecho que ele ja decidiu MANTER por causa disso. As passadas que vem
    depois decidem por TEXTO, e ali texto e o que nao existe; cortando,
    levavam junto a pendencia, porque a lista pula token cortado. O trecho
    sumia do video e da pergunta na mesma linha.

    Um caso de verdade: 669,1s do "as de espadas", cortado como
    `tentativa`. O wav2vec2 do `align`, decodificado livre, ouve
    "...sica, e isso e t..." ali."""
    print()
    print("== corte no escuro (protege_naolidos)")

    def guarda(why, texto="?? 1.1s"):
        f = [tok("hesitacao", texto, 1.0, 2.1, True, why)]
        tc.protege_naolidos(f)
        return f[0]["cut"]

    diz(guarda("tentativa") is False,
        "regra de texto nao corta o que ninguem leu")
    diz(guarda("retomada colapsada: ?? 1.1s") is False,
        "eco tambem nao")
    diz(guarda("anotado: ruido, corta") is True,
        "anotacao continua cortando - e gente, nao regra")
    diz(guarda("ela mandou cortar: corta isso") is True,
        "'ela mandou cortar' continua cortando, pelo mesmo motivo")
    diz(guarda("tentativa", texto="entao a gente") is True,
        "token com texto lido segue a regra: a guarda so vale para `??`")

    # o laco: protegido, ele volta a ser PERGUNTA. Sem isto a guarda so
    # trocaria "sumiu calado" por "ficou calado".
    f = [tok("palavra", "essa", 0.0, 0.9),
         tok("hesitacao", "?? 1.1s", 1.0, 2.1, True, "tentativa"),
         tok("palavra", "diferenca", 2.2, 3.0)]
    tc.protege_naolidos(f)
    pend = tc.pendencias(f, tc.cfg(), set())
    diz(any(q["rot"] == "som alto sem texto" for q in pend),
        "trecho protegido reaparece como pendencia no revisao.txt")


def main():
    alvos = sys.argv[1:] or CALIBRACAO
    teste_gaguejada()
    teste_naolido()
    teste_vocabulario()
    teste_respostas()
    teste_texto_explica_o_tempo()
    for v in alvos:
        teste_pendencias(v)
        teste_repeticao_audivel(v)
        teste_respostas_no_disco(v)
        teste_arrasto(v)
    print("\n" + ("tudo ok" if not falhas
                  else f"{len(falhas)} FALHA(S):\n  " + "\n  ".join(falhas)))
    sys.exit(1 if falhas else 0)


if __name__ == "__main__":
    main()
