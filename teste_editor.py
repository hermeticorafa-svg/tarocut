#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""teste_editor.py - as propriedades do editor que quebram em silencio.

Nao e suite de teste: e a lista curta do que o editor promete e ninguem
percebe quando deixa de valer. Um erro aqui nao aparece na tela nem no
ouvido - o corte continua saindo, so que diferente do que se pediu.

    venv\\Scripts\\python.exe teste_editor.py IMG_0247

Sem argumento, roda nos tres videos de calibracao. Ele mexe no
`montagem.json` e no `correcoes.json` do video (todo gesto que muda algo
grava os dois) e devolve os dois arquivos como estavam no fim, inclusive
quando falha.
"""

import json
import sys
from pathlib import Path

import editor
import tarocut as tc

CALIBRACAO = ["IMG_0246", "IMG_0247", "IMG_0248"]


def roda(video):
    d = tc.WORK / video
    mont = d / "montagem.json"
    corr = d / "correcoes.json"
    guarda = mont.read_bytes() if mont.exists() else None
    guarda_corr = corr.read_bytes() if corr.exists() else None
    falhas = []

    def diz(ok, txt):
        print(("  ok    " if ok else "  FALHA ") + txt)
        if not ok:
            falhas.append(f"{video}: {txt}")

    try:
        mont.unlink(missing_ok=True)
        corr.unlink(missing_ok=True)
        S = editor.Sessao(video)
        base = json.dumps(S.intervalos())
        toks = S.estado()["pecas"]["tokens"]

        # uma palavra inteira, mantida, longe das pontas
        alvo = next(i for i, t in enumerate(toks)
                    if t["txt"] and t["vivo"] == [[0.0, 1.0]] and t["s"] > 100)
        sel = {"i0": alvo, "i1": alvo + 1}
        t = S.stream[alvo]
        print(f"\n== {video} | palavra de teste {t['t']!r} "
              f"({t['s']:.3f}-{t['e']:.3f})")

        # 1. O MAIS DESFAZ O MENOS, EXATO. E a promessa do par de botoes, e
        #    a que mais custou: sem ela, cinco menos e cinco mais deixavam
        #    fala cortada de fora e traziam silencio no lugar.
        for lado in ("esq", "dir"):
            r = S.acao({"acao": "menos", "lado": lado, "sel": sel,
                        "unidade": 0.1})
            diz(r["mudou"] and json.dumps(S.intervalos()) != base,
                f"menos {lado} mexeu no corte")
            diz(r["gestos"][lado]["mais"] is not None,
                f"mais {lado} acendeu sozinho depois do menos")
            S.acao({"acao": "mais", "lado": lado, "sel": sel, "unidade": 0.1})
            diz(json.dumps(S.intervalos()) == base,
                f"mais {lado} desfez o menos, exato")

        for _ in range(5):
            S.acao({"acao": "menos", "lado": "dir", "sel": sel,
                    "unidade": 0.1})
        tirou = (sum(b - a for a, b in json.loads(base))
                 - sum(b - a for a, b in S.intervalos()))
        diz(tirou > 0.35, f"cinco x 100 ms tiraram {tirou*1000:.0f} ms")
        for _ in range(5):
            S.acao({"acao": "mais", "lado": "dir", "sel": sel,
                    "unidade": 0.1})
        diz(json.dumps(S.intervalos()) == base, "cinco mais devolveram tudo")

        # 2. OCULTAR A PALAVRA TIRA A PALAVRA - OU DIZ QUE NAO TIROU.
        #    `folga_palavra` recua a emenda 0,06s de cada palavra mantida
        #    vizinha, entao uma palavra cercada de fala sobra ~0,12s de
        #    borda por construcao, e uma palavra MAIS CURTA que isso sobra
        #    inteira (o `de` de 100,19s do IMG_0248). Nao se conserta
        #    baixando a folga - 0,06 e erro de fronteira medido. O que nao
        #    pode e sumir calado: ou saiu, ou tem aviso na tela.
        r = S.acao({"acao": "alterna", "sel": sel})
        iv = S.intervalos()
        vivo = sum(max(0.0, min(t["e"], b) - max(t["s"], a)) for a, b in iv)
        frac = vivo / (t["e"] - t["s"])
        avisou = any(a.get("palavra") == t["t"] for a in r["avisos"])
        diz(frac < 0.5 or avisou,
            f"{t['t']!r} sobra {vivo*1000:.0f} ms ({frac:.0%})"
            + (" e a tela avisa" if avisou else ", e o meio saiu"))
        diz(mont.exists(), "montagem.json gravado sozinho, sem pedir")
        S.acao({"acao": "alterna", "sel": sel})
        diz(json.dumps(S.intervalos()) == base, "alterna de novo devolveu")
        diz(not json.loads(mont.read_text(encoding="utf-8"))["tokens"],
            "voltar a proposta automatica nao deixa decisao no arquivo")

        S.acao({"acao": "menos", "lado": "dir", "sel": sel, "unidade": 0.25})
        S.acao({"acao": "alterna", "sel": sel})
        S.acao({"acao": "desfazer", "sel": sel})
        S.acao({"acao": "desfazer", "sel": sel})
        diz(json.dumps(S.intervalos()) == base, "dois desfazer voltaram")

        # 3. O CURSOR ENTRE LETRAS VIRA TEMPO DENTRO DA PALAVRA.
        n = len(t["t"])
        a0, _, _, _ = S.selecao({"i": alvo, "k": 0, "n": n})
        b0, _, _, _ = S.selecao({"i": alvo, "k": n, "n": n})
        m0, _, _, _ = S.selecao({"i": alvo, "k": n // 2, "n": n})
        diz(abs(a0 - t["s"]) < 1e-6 and abs(b0 - t["e"]) < 1e-6
            and a0 < m0 < b0, "letra 0 e letra n caem nas bordas da palavra")

        # 4. REABRIR UMA ANOTACAO NAO E BUG - E TEM DE APARECER ASSIM.
        #    `ANOTACAO NAO OBEDECIDA` e o alarme mais serio deste programa
        #    (o `/revisar` so termina com zero delas). Se ele disparasse em
        #    todo ajuste feito depois do passe de julgamento, viraria papel
        #    de parede - que e o mesmo que apagar o alarme.
        oculto = next((i for i, x in enumerate(toks)
                       if x["txt"] and not x["vivo"]
                       and x["why"].startswith("anotado")), None)
        if oculto is None:
            print("  --    sem palavra anotada para reabrir, pulado")
        else:
            r = S.acao({"acao": "alterna", "sel": {"i0": oculto,
                                                   "i1": oculto + 1}})
            av = [a for a in r["avisos"] if not a.get("palavra")]
            diz(bool(av) and all(a["mao"] for a in av),
                f"reabrir {S.stream[oculto]['t']!r} sai como decisao sua, "
                f"nao como anotacao perdida")
            S.acao({"acao": "alterna", "sel": {"i0": oculto,
                                               "i1": oculto + 1}})

        # 5. O QUE A TELA MOSTROU E O QUE O RENDER VAI VER. Se esta linha
        #    cair, o editor virou uma segunda opiniao sobre o corte - que e
        #    exatamente o que ele nao pode ser.
        S.acao({"acao": "menos", "lado": "dir", "sel": sel, "unidade": 0.25})
        da_tela = S.intervalos()
        C, _, stream, dur, db, hop, anot, m, _ = tc._base(video, "teste")
        diz(json.dumps(da_tela) == json.dumps(
                tc.intervalos(stream, C, dur, db, hop, anot, m)),
            f"{len(da_tela)} trechos iguais, relidos do disco pelo render")
    finally:
        mont.unlink(missing_ok=True)
        if guarda is not None:
            mont.write_bytes(guarda)
        (d / "montagem-anterior.json").unlink(missing_ok=True)
        corr.unlink(missing_ok=True)
        if guarda_corr is not None:
            corr.write_bytes(guarda_corr)
        (d / "correcoes-anterior.json").unlink(missing_ok=True)
    return falhas


if __name__ == "__main__":
    alvos = sys.argv[1:] or CALIBRACAO
    todas = []
    for v in alvos:
        if not (tc.WORK / Path(v).stem / "stream.json").exists():
            print(f"== {v}: sem stream.json, pulado")
            continue
        todas += roda(Path(v).stem)
    print()
    if todas:
        for f in todas:
            print("FALHA " + f)
        sys.exit(1)
    print("tudo ok")
