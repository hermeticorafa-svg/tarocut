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


def jload_lacunas(d):
    """As lacunas medidas pelo analyze, direto do disco - a fonte contra a
    qual se confere o casamento por tempo que o editor faz."""
    return tc.jload(d / "stream.json").get("lacunas", [])


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

        # 4b. REABRIR VALE PARA QUALQUER TOKEN, NAO SO PARA PALAVRA.
        #     A anotacao entra no corte como TRECHO DE TEMPO, e quem escava
        #     token mantido de dentro de um trecho de tempo e o
        #     `_respeita_palavras`. Enquanto a lista dele fosse so
        #     `k == "palavra"`, reabrir uma `hesitacao` era um clique que
        #     nao fazia nada: o token voltava a `cut=False`, a tela pintava
        #     ele de volta, e o video continuava sem ele.
        #
        #     Aconteceu no "as de espadas": cinco tokens seguidos reabertos
        #     dentro da mesma anotacao, quatro palavras voltaram inteiras e
        #     a quinta - `hesitacao` de 6,06s - sobrou 0,06s, que era a
        #     folga vazando da palavra vizinha. Meio pedido obedecido e mais
        #     dificil de notar do que nenhum.
        #
        #     `hesitacao`, e nao `silencio`, porque e o caso que existe nos
        #     tres videos e o que carrega fala (o `resolve_suspeitos` faz um
        #     bloco so de varios segundos de audio); a regra no programa nao
        #     olha `k` nenhum, olha o `why` de `MAO_MANTEM`.
        mudo = next((i for i, t in enumerate(S.stream)
                     if t["k"] == "hesitacao" and t["cut"]
                     and str(t.get("why", "")).startswith("anotado")
                     and sum(max(0.0, min(t["e"], y) - max(t["s"], x))
                             for x, y in S.intervalos())
                     < 0.5 * (t["e"] - t["s"])), None)
        if mudo is None:
            print("  --    sem hesitacao anotada para reabrir, pulado")
        else:
            t = S.stream[mudo]
            larg = t["e"] - t["s"]
            r = S.acao({"acao": "alterna", "sel": {"i0": mudo,
                                                   "i1": mudo + 1}})
            vivo = sum(max(0.0, min(t["e"], y) - max(t["s"], x))
                       for x, y in S.intervalos())
            diz(vivo > 0.99 * larg,
                f"reabrir a hesitacao {t['t'][:18]!r} devolveu "
                f"{vivo:.2f}s de {larg:.2f}s")
            #  E o aviso tem de sumir junto: ele existe para dizer "voce
            #  mandou e nao saiu", e continuar aceso depois de sair seria a
            #  mesma mentira do outro lado.
            diz(not [a for a in r["avisos"]
                     if a.get("palavra") and abs(a["ini"] - t["s"]) < 0.01],
                "e o aviso de que ela continuava fora sumiu")
            #  O rotulo da lista "o que ouvir" escolhe entre NAO SAIU e NAO
            #  VOLTOU por este campo. Sem ele a tela cai no primeiro dos
            #  dois e diz o contrario do que aconteceu num restauro.
            diz(all("estado" in a for a in r["avisos"] if a.get("palavra")),
                "todo aviso de palavra diz QUAL pedido foi desobedecido")
            S.acao({"acao": "alterna", "sel": {"i0": mudo, "i1": mudo + 1}})

        # 5. VOZ SEM TEXTO CHEGA A TELA MARCADA COMO VOZ.
        #    Ate 03/09/2026 um `hesitacao` sem texto e uma pausa eram o
        #    mesmo tracinho cinza, com o mesmo title ("sem som aqui") - no
        #    IMG_0247, 26 de pausa e 34 de voz, indistinguiveis. Se esta
        #    linha cair, "parte do audio que nao vai para a transcricao"
        #    volta a ser invisivel, que e a queixa que originou tudo isto.
        pc = S.estado()["pecas"]["tokens"]
        mudos = [i for i, t in enumerate(S.stream)
                 if t["k"] == "hesitacao" and t["t"] == "hesitação"]
        diz(bool(mudos) and all(pc[i]["voz"] and not pc[i]["txt"]
                                for i in mudos),
            f"as {len(mudos)} hesitacoes sem texto saem marcadas como voz")
        #    Uma PAUSA tambem pode ser voz: quando a lacuna era `misto` e a
        #    leitura foi recusada, o analyze trata a recusa como silencio e
        #    o token sai rotulado pausa (e quando ela tem entre
        #    `pausa_intocavel` e `isolar_min` de duracao, nem chega a ser
        #    lida). Isso so vale se o casamento por tempo pegar a lacuna
        #    CERTA - uma tolerancia frouxa marcaria como voz uma pausa
        #    vizinha, e ai a marca passaria a mentir em vez de informar.
        lacs = {(round(l["s"], 2), round(l["e"], 2)): l
                for l in jload_lacunas(d)}
        erradas = [i for i, t in enumerate(S.stream)
                   if t["k"] == "silencio" and pc[i]["voz"]
                   and lacs.get((round(t["s"], 2), round(t["e"], 2)),
                                {}).get("tipo") not in ("fala", "misto")]
        pausas_voz = sum(1 for i, t in enumerate(S.stream)
                         if t["k"] == "silencio" and pc[i]["voz"])
        diz(not erradas,
            f"as {pausas_voz} pausas marcadas com voz casam com a lacuna "
            f"que o analyze mediu ({len(erradas)} erradas)")

        # 6. ESCREVER O QUE SE OUVE NUM VAO. E a outra metade da queixa:
        #    ver a fala, ouvir a fala e nao ter onde escrever. O que quebra
        #    calado aqui e o texto nao sobreviver ao disco (o
        #    `confere_correcoes` recusa e o editor descarta em silencio) ou
        #    a escrita MEXER NO CORTE - `correcoes.json` e sobre o que a
        #    palavra DIZ, nunca sobre se ela sai do video.
        if mudos:
            i = mudos[0]
            antes = json.dumps(S.intervalos())
            r = S.acao({"acao": "corrigir", "i": i, "texto": "teste ouvido"})
            diz(r["mudou"], f"vao de {S.stream[i]['s']:.2f}s aceitou texto")
            diz(json.dumps(S.intervalos()) == antes,
                "escrever nao mexeu num milissegundo do corte")
            pc2 = S.estado()["pecas"]["tokens"]
            diz(pc2[i]["txt"] == "teste ouvido" and not pc2[i]["vao"],
                "o vao virou palavra na tela")
            # o alarme que a propria escrita criaria, se ninguem cuidasse
            pend = tc.pendencias(S.stream, S.C, tc.carrega_vocabulario(d))
            diz(not any(q["i"] == i for q in pend),
                "o programa nao pergunta de volta o que voce escreveu")
            S2 = editor.Sessao(video)
            diz(S2.stream[i]["t"] == "teste ouvido",
                "o texto escrito sobreviveu ao disco")
            S.acao({"acao": "desfazer", "sel": sel})
            diz(S.stream[i]["t"] == "hesitação",
                "desfazer devolveu o rotulo original")

        # 7. PAUSA MUDA NAO ACEITA TEXTO. O contrario do item 6: onde nao
        #    ha voz nao ha o que transcrever, e liberar a escrita ali daria
        #    um jeito novo de por palavra onde nao houve palavra.
        muda = next((i for i, t in enumerate(S.stream)
                     if t["k"] == "silencio" and not S._voz[i]), None)
        if muda is not None:
            r = S.acao({"acao": "corrigir", "i": muda, "texto": "nao devia"})
            diz(not r["mudou"] and S.stream[muda]["t"] != "nao devia",
                "pausa sem voz recusa texto")

        # 8. O PAINEL DIZ O MESMO QUE O `conferir`. Duas implementacoes da
        #    mesma pergunta divergem, e a que diverge calada e sempre a que
        #    ninguem esta olhando - foi por isso que os achados sairam do
        #    `cmd_conferir` para uma funcao so.
        iv = S.intervalos()
        pend = tc.pendencias(S.stream, S.C, tc.carrega_vocabulario(d))
        todos, _ = tc.achados_corte(S.stream, S.C, iv, S.anot, S.montagem(),
                                    pend, tc.carrega_respostas(d), d)
        painel = S.achados(iv)
        # o painel tira o que o `avisos` ja diz melhor, e so isso
        cobertos = [x for x in todos if x[1].startswith("ANOTACAO")]
        diz(len(painel) + len(cobertos) == len(todos),
            f"o painel mostra os {len(painel)} achados do conferir "
            f"({len(cobertos)} ja estao nos avisos)")

        # 9. QUEM MANDOU CORTAR ESTA PALAVRA. O riscado do julgamento
        #    humano tem de cobrir exatamente os tokens que a anotacao
        #    corta - se as duas contas divergirem, a folha passa a pintar
        #    de azul uma palavra que a regra automatica tirou, ou (pior)
        #    de cinza uma que uma pessoa mandou sair.
        pc = S.estado()["pecas"]["tokens"]
        pela_tela = {i for i, p in enumerate(pc) if p["anot"]}
        pelo_corte = {i for i, t in enumerate(S.stream)
                      for a in S.anot
                      if not (t["e"] <= a["ini"] or t["s"] >= a["fim"])}
        diz(pela_tela == pelo_corte,
            f"{len(pela_tela)} tokens pintados como anotados, os mesmos "
            f"que a anotacao corta")
        if S.anot:
            motivos = {p["anot"] for p in pc if p["anot"]}
            diz(motivos <= {a["motivo"] for a in S.anot},
                "cada palavra anotada carrega o motivo que a pessoa escreveu")

        # 10. O RELOGIO DA PREVIA. Ele anota ouvindo o .m4a e digita
        #     "1:29"; a folha e a fonte. A traducao e a mesma do
        #     `conferir --em`, e o teste e o par: ida e volta caem no
        #     mesmo lugar. Se isto divergir, o editor leva ele para o
        #     lugar errado sem dizer nada - a familia de defeito que este
        #     programa mais teve.
        iv0 = S.intervalos()
        para_fonte, para_previa = tc._mapa_previa(iv0)
        mantido0 = sum(b - a for a, b in iv0)
        erradas = []
        for frac in (0.1, 0.35, 0.6, 0.9):
            t = mantido0 * frac
            r = S.em_previa(t)
            if abs(para_previa(r["fonte"]) - t) > 0.01:
                erradas.append(t)
            tk = S.stream[r["i"]]
            if not (tk["e"] > r["fonte"] - 1e-6):
                erradas.append(t)
        diz(not erradas,
            f"quatro tempos do .m4a viraram fonte e voltaram iguais "
            f"({len(erradas)} erradas)")
        diz(S._m4a.name == f"{video}_previa.m4a"
            and S._m4a.parent == S.src.parent,
            f"o editor procura a previa onde o `audio` grava ({S._m4a.name})")

        # 11. .m4a QUE NAO E MAIS ESTE CORTE. Mesmo numero da fase 0 do
        #     `/anotar` (0,15 s): abaixo disso e o padding do AAC, acima e
        #     edicao. Sem este aviso, o relogio da tela continuaria
        #     respondendo com a maior seriedade sobre um arquivo que nao
        #     existe mais - errado, e em silencio.
        #     Aponta para um arquivo que EXISTE (o audio.wav ao lado) e
        #     semeia o cache com a duracao: assim se mede a regra sem
        #     gastar um ffprobe, e sem depender de haver .m4a no disco -
        #     nenhum dos tres videos de calibracao tem um. O caminho do
        #     ffprobe esta medido a mao nos dois .m4a que existem ("7 de
        #     espadas" +0,02 s, "as de espadas" +0,05 s).
        S._m4a = S.wav
        S._m4a_chave = (S.wav.stat().st_mtime_ns, S.wav.stat().st_size)
        S._m4a_dur = mantido0 + 0.07           # o padding medido do AAC
        certo = S.previa_no_disco(mantido0)
        S._m4a_dur = mantido0 + 0.40           # uma anotacao entrou depois
        caduco = S.previa_no_disco(mantido0)
        diz(certo["tem"] and not certo["velho"] and caduco["velho"],
            "0,07 s de padding passa, 0,40 s de diferenca vira aviso")

        # 12. O QUE A TELA MOSTROU E O QUE O RENDER VAI VER. Se esta linha
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
