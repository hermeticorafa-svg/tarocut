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

Foi reescrito em 08/09/2026 junto com o editor. As promessas dos botoes de
mais e menos sairam com eles; a garantia por tras de cada uma - **nunca
sumir decisao em silencio** - continua valendo, e cada uma ganhou aqui o
equivalente do modelo novo:

    "mais desfaz menos, exato"  ->  "arrastar de volta desfaz exato"
    "ocultar tira a palavra OU avisa"  ->  "ocultar tira a palavra", ponto
    "o cursor de letra vira tempo"  ->  texto e onda dao a MESMA selecao
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


def vivo_de(iv, a, b):
    return sum(max(0.0, min(b, y) - max(a, x)) for x, y in iv)


def amostra_palavras(S, quantas=26):
    """As palavras contra as quais a garantia se mede.

    As MAIS CURTAS na frente, de proposito: sao elas que a geometria nunca
    conseguiu tirar. `minimo` (0,28 s) mais as duas folgas de
    `folga_palavra` (0,06) anulam o corte de toda palavra menor que 0,40 s,
    e essas sao quase todas as palavras curtas do portugues falado - "de",
    "que", "e", "um", "no", "ou", justamente as que aparecem numa gaguejada.
    O resto da amostra e espalhado pelo video para nao medir so o caso
    facil."""
    ok = [i for i, t in enumerate(S.stream)
          if t["k"] == "palavra" and not S.base[i][0] and t["e"] - t["s"] > 0.02]
    curtas = sorted(ok, key=lambda i: S.stream[i]["e"] - S.stream[i]["s"])
    passo = max(1, len(ok) // 12)
    return sorted(set(curtas[:quantas // 2] + ok[::passo][:quantas // 2]))


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

        # 1. ARRASTAR DE VOLTA DESFAZ EXATO. Herdeira da promessa mais cara
        #    do editor de botoes ("mais desfaz menos"), e agora ela e
        #    aritmetica em vez de heuristica: o arrasto NOMEIA o trecho, e
        #    `_corta`/`_restaura` mantem os dois conjuntos disjuntos com
        #    `subtrai_spans`. Nao ha mais "qual pedaco este botao devolve".
        a, b = t["s"], t["e"]
        r = S.acao({"acao": "cortar_tempo", "ini": a, "fim": b, "sel": sel})
        diz(r["mudou"] and json.dumps(S.intervalos()) != base,
            "arrastar sobre o mantido tirou o trecho")
        S.acao({"acao": "restaurar_tempo", "ini": a, "fim": b, "sel": sel})
        diz(json.dumps(S.intervalos()) == base,
            "arrastar de volta devolveu, exato")

        meio = (a + b) / 2
        for k in range(5):
            x = a + (meio - a) * k / 5
            S.acao({"acao": "cortar_tempo", "ini": x,
                    "fim": a + (meio - a) * (k + 1) / 5, "sel": sel})
        tirou = (sum(y - x for x, y in json.loads(base))
                 - sum(y - x for x, y in S.intervalos()))
        diz(abs(tirou - (meio - a)) < 0.005,
            f"cinco arrastes seguidos tiraram {tirou*1000:.0f} ms "
            f"de {(meio-a)*1000:.0f} ms pedidos")
        S.acao({"acao": "restaurar_tempo", "ini": a, "fim": meio, "sel": sel})
        diz(json.dumps(S.intervalos()) == base,
            "um arrasto de volta desfez os cinco")

        # 2. OCULTAR TIRA A PALAVRA. PONTO.
        #    Era "ou avisa que nao tirou", e o aviso estava certo e ninguem
        #    lia. MEDIDO antes desta mudanca, ocultando cada palavra mantida
        #    uma de cada vez: a palavra sumia de verdade em 7 de 306
        #    (IMG_0246), 21 de 660 (IMG_0247) e 20 de 742 (IMG_0248), e
        #    sobrevivia INTEIRA em 168, 343 e 391. Depois: 306/306, 660/660
        #    e 742/742, zero excecoes.
        #
        #    A amostra comeca pelas mais curtas justamente porque eram elas
        #    que nunca saiam - uma amostra "tipica" passaria sem ver nada.
        sobrou = []
        amostra = amostra_palavras(S)
        for i in amostra:
            tk = S.stream[i]
            S.acao({"acao": "ocultar", "sel": {"i0": i, "i1": i + 1}})
            v = vivo_de(S.intervalos(), tk["s"], tk["e"])
            if v > 0.02:
                sobrou.append((round(v, 3), tk["t"]))
            S.acao({"acao": "restaurar", "sel": {"i0": i, "i1": i + 1}})
        menor = min(S.stream[i]["e"] - S.stream[i]["s"] for i in amostra)
        diz(not sobrou,
            f"as {len(amostra)} palavras da amostra (a menor tem "
            f"{menor*1000:.0f} ms) sumiram inteiras" +
            (f" - sobraram {sobrou[:4]}" if sobrou else ""))
        diz(json.dumps(S.intervalos()) == base,
            "e restaurar cada uma devolveu o corte de partida")

        # 2b. VOLTAR A PROPOSTA AUTOMATICA NAO DEIXA RASTRO EM NENHUMA DAS
        #     DUAS CAMADAS. A decisao de palavra desce para o tempo, entao
        #     desfaze-la tem de subir de volta: um trecho de tempo esquecido
        #     ali manteria a palavra cortada depois de restaurada, que e a
        #     promessa quebrada do outro lado.
        S.acao({"acao": "alterna", "sel": sel})
        diz(mont.exists(), "montagem.json gravado sozinho, sem pedir")
        S.acao({"acao": "alterna", "sel": sel})
        m = json.loads(mont.read_text(encoding="utf-8"))
        diz(not m["tokens"] and not m["cortes"] and not m["restauros"],
            "voltar a proposta nao deixa decisao nem trecho no arquivo")
        diz(json.dumps(S.intervalos()) == base, "alterna de novo devolveu")

        S.acao({"acao": "cortar_tempo", "ini": a, "fim": meio, "sel": sel})
        S.acao({"acao": "alterna", "sel": sel})
        S.acao({"acao": "desfazer", "sel": sel})
        S.acao({"acao": "desfazer", "sel": sel})
        diz(json.dumps(S.intervalos()) == base, "dois desfazer voltaram")

        # 3. TEXTO E ONDA DAO A MESMA SELECAO. E o pedido central dele -
        #    "as palavras e o tempo tem que ser intercambiaveis" - e o lugar
        #    onde ele quebraria calado: a tela mandaria tempo, o servidor
        #    responderia outra palavra, e a folha acenderia o lugar errado
        #    sem nada indicar isso.
        erradas = []
        for i in amostra[:10]:
            por_palavra = S.selecao({"i0": i, "i1": i + 1})
            por_tempo = S.selecao({"ini": por_palavra[0], "fim": por_palavra[1]})
            if por_tempo != por_palavra:
                erradas.append((i, por_palavra, por_tempo))
        diz(not erradas,
            f"dez selecoes de texto e de onda coincidem ({len(erradas)} nao)")
        #    E um clique de largura zero num vao pega a palavra seguinte, a
        #    mesma escolha que clicar no tracinho da folha faz.
        vao = next((i for i in range(1, len(S.stream))
                    if S.stream[i - 1]["e"] < S.stream[i]["s"] - 0.2), None)
        if vao is not None:
            meiovao = (S.stream[vao - 1]["e"] + S.stream[vao]["s"]) / 2
            _, _, j0, j1 = S.selecao({"ini": meiovao, "fim": meiovao})
            diz(j0 == vao and j1 == vao + 1,
                "clique no meio de um vao pega a palavra seguinte")

        # 4. OCULTAR NA ONDA RISCA A PALAVRA NO TEXTO. A outra metade do
        #    pedido: "se eu ocultar a faixa de audio, o texto some". Sai por
        #    construcao - o riscado vem dos trechos MANTIDOS -, e e essa
        #    construcao que esta linha protege.
        S.acao({"acao": "cortar_tempo", "ini": t["s"], "fim": t["e"],
                "sel": sel})
        pc = S.estado()["pecas"]["tokens"]
        diz(not pc[alvo]["vivo"],
            f"tirar {t['t']!r} na onda riscou a palavra inteira no texto")
        S.acao({"acao": "desfazer", "sel": sel})

        # 5. RESTAURAR VALE PARA QUALQUER TOKEN, INTEIRO.
        #    A anotacao entra no corte como TRECHO DE TEMPO, e enquanto o
        #    `_respeita_palavras` so defendesse `k == "palavra"`, restaurar
        #    uma `hesitacao` era um clique que nao fazia nada. Aconteceu no
        #    "as de espadas": cinco tokens seguidos reabertos dentro da mesma
        #    anotacao, quatro palavras voltaram inteiras e a quinta -
        #    `hesitacao` de 6,06 s - sobrou 0,06 s, que era a folga vazando
        #    da palavra vizinha. Meio pedido obedecido e mais dificil de
        #    notar do que nenhum.
        iv0 = S.intervalos()
        for tipo in ("palavra", "hesitacao", "silencio"):
            i = next((k for k, tk in enumerate(S.stream)
                      if tk["k"] == tipo and tk["cut"]
                      and tk["e"] - tk["s"] > 0.1
                      and vivo_de(iv0, tk["s"], tk["e"])
                      < 0.5 * (tk["e"] - tk["s"])), None)
            if i is None:
                print(f"  --    sem {tipo} cortado para restaurar, pulado")
                continue
            tk = S.stream[i]
            larg = tk["e"] - tk["s"]
            r = S.acao({"acao": "restaurar", "sel": {"i0": i, "i1": i + 1}})
            v = vivo_de(S.intervalos(), tk["s"], tk["e"])
            diz(v > larg - 0.02,
                f"restaurar o {tipo} {tk['t'][:16]!r} devolveu "
                f"{v:.2f}s de {larg:.2f}s")
            #  E o aviso de "voce mandou e nao voltou" tem de sumir junto:
            #  continuar aceso depois de voltar e a mesma mentira do outro
            #  lado.
            diz(not [x for x in r["avisos"]
                     if x.get("palavra") and abs(x["ini"] - tk["s"]) < 0.01],
                f"e o aviso sobre o {tipo} sumiu junto")
            diz(all("estado" in x for x in r["avisos"] if x.get("palavra")),
                "todo aviso de palavra diz QUAL pedido foi desobedecido")
            S.acao({"acao": "desfazer", "sel": sel})

        # 6. REABRIR UMA ANOTACAO NAO E BUG - E TEM DE APARECER ASSIM.
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
            r = S.acao({"acao": "restaurar",
                        "sel": {"i0": oculto, "i1": oculto + 1}})
            av = [x for x in r["avisos"] if not x.get("palavra")]
            diz(bool(av) and all(x["mao"] for x in av),
                f"reabrir {S.stream[oculto]['t']!r} sai como decisao sua, "
                f"nao como anotacao perdida")
            S.acao({"acao": "desfazer", "sel": sel})

        # 7. VOZ SEM TEXTO CHEGA A TELA MARCADA COMO VOZ.
        #    Ate 03/09/2026 um `hesitacao` sem texto e uma pausa eram o mesmo
        #    tracinho cinza, com o mesmo title ("sem som aqui") - no IMG_0247,
        #    26 de pausa e 34 de voz, indistinguiveis. Se esta linha cair,
        #    "parte do audio que nao vai para a transcricao" volta a ser
        #    invisivel, que e a queixa que originou tudo isto.
        pc = S.estado()["pecas"]["tokens"]
        mudos = [i for i, tk in enumerate(S.stream)
                 if tk["k"] == "hesitacao" and tk["t"] == "hesitação"]
        diz(bool(mudos) and all(pc[i]["voz"] and not pc[i]["txt"]
                                for i in mudos),
            f"as {len(mudos)} hesitacoes sem texto saem marcadas como voz")
        #    Uma PAUSA tambem pode ser voz: quando a lacuna era `misto` e a
        #    leitura foi recusada, o analyze trata a recusa como silencio e o
        #    token sai rotulado pausa. Isso so vale se o casamento por tempo
        #    pegar a lacuna CERTA - uma tolerancia frouxa marcaria como voz
        #    uma pausa vizinha, e ai a marca passaria a mentir em vez de
        #    informar.
        lacs = {(round(l["s"], 2), round(l["e"], 2)): l
                for l in jload_lacunas(d)}
        erradas = [i for i, tk in enumerate(S.stream)
                   if tk["k"] == "silencio" and pc[i]["voz"]
                   and lacs.get((round(tk["s"], 2), round(tk["e"], 2)),
                                {}).get("tipo") not in ("fala", "misto")]
        pausas_voz = sum(1 for i, tk in enumerate(S.stream)
                         if tk["k"] == "silencio" and pc[i]["voz"])
        diz(not erradas,
            f"as {pausas_voz} pausas marcadas com voz casam com a lacuna "
            f"que o analyze mediu ({len(erradas)} erradas)")

        # 8. ESCREVER O QUE SE OUVE NUM VAO. E a outra metade da queixa: ver
        #    a fala, ouvir a fala e nao ter onde escrever. O que quebra calado
        #    aqui e o texto nao sobreviver ao disco (o `confere_correcoes`
        #    recusa e o editor descarta em silencio) ou a escrita MEXER NO
        #    CORTE - `correcoes.json` e sobre o que a palavra DIZ, nunca sobre
        #    se ela sai do video.
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

        # 9. PAUSA MUDA NAO ACEITA TEXTO. O contrario do item 8: onde nao ha
        #    voz nao ha o que transcrever, e liberar a escrita ali daria um
        #    jeito novo de por palavra onde nao houve palavra.
        muda = next((i for i, tk in enumerate(S.stream)
                     if tk["k"] == "silencio" and not S._voz[i]), None)
        if muda is not None:
            r = S.acao({"acao": "corrigir", "i": muda, "texto": "nao devia"})
            diz(not r["mudou"] and S.stream[muda]["t"] != "nao devia",
                "pausa sem voz recusa texto")

        # 10. O PAINEL DIZ O MESMO QUE O `conferir`. Duas implementacoes da
        #     mesma pergunta divergem, e a que diverge calada e sempre a que
        #     ninguem esta olhando - foi por isso que os achados sairam do
        #     `cmd_conferir` para uma funcao so.
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

        # 11. QUEM MANDOU CORTAR ESTA PALAVRA. O riscado do julgamento humano
        #     tem de cobrir exatamente os tokens que a anotacao corta - se as
        #     duas contas divergirem, a folha (e agora tambem a faixa de
        #     palavras da onda) passa a pintar de azul uma palavra que a regra
        #     automatica tirou, ou (pior) de cinza uma que uma pessoa mandou
        #     sair.
        pc = S.estado()["pecas"]["tokens"]
        pela_tela = {i for i, p in enumerate(pc) if p["anot"]}
        pelo_corte = {i for i, tk in enumerate(S.stream)
                      for x in S.anot
                      if not (tk["e"] <= x["ini"] or tk["s"] >= x["fim"])}
        diz(pela_tela == pelo_corte,
            f"{len(pela_tela)} tokens pintados como anotados, os mesmos "
            f"que a anotacao corta")
        if S.anot:
            motivos = {p["anot"] for p in pc if p["anot"]}
            diz(motivos <= {x["motivo"] for x in S.anot},
                "cada palavra anotada carrega o motivo que a pessoa escreveu")

        # 12. O RELOGIO DA PREVIA, NOS DOIS SENTIDOS. Ele anota ouvindo o
        #     .m4a e digita "1:29"; a folha e a fonte. Se isto divergir, o
        #     editor leva ele para o lugar errado sem dizer nada.
        iv0 = S.intervalos()
        para_fonte, para_previa = tc._mapa_previa(iv0)
        mantido0 = sum(b - a for a, b in iv0)
        erradas = []
        for frac in (0.1, 0.35, 0.6, 0.9):
            x = mantido0 * frac
            r = S.em_previa(x)
            if abs(para_previa(r["fonte"]) - x) > 0.01:
                erradas.append(x)
            tk = S.stream[r["i"]]
            if not (tk["e"] > r["fonte"] - 1e-6):
                erradas.append(x)
        diz(not erradas,
            f"quatro tempos do .m4a viraram fonte e voltaram iguais "
            f"({len(erradas)} erradas)")
        #     A regua da onda le a MESMA tabela, mandada pronta em `ivp`. Uma
        #     segunda soma corrida feita no navegador "so para desenhar" seria
        #     a mesma conta dita duas vezes, e a que diverge calada e sempre a
        #     que ninguem esta olhando.
        ivp = S.estado()["ivp"]
        diz(len(ivp) == len(iv0)
            and all(abs(p - para_previa(a)) < 0.002
                    for p, (a, _) in zip(ivp, iv0)),
            f"a tabela dos dois relogios que vai para a tela ({len(ivp)} "
            f"entradas) e a mesma do `_mapa_previa`")
        diz(S._m4a.name == f"{video}_previa.m4a"
            and S._m4a.parent == S.src.parent,
            f"o editor procura a previa onde o `audio` grava ({S._m4a.name})")

        # 13. .m4a QUE NAO E MAIS ESTE CORTE. Mesmo numero da fase 0 do
        #     `/anotar` (0,15 s): abaixo disso e o padding do AAC, acima e
        #     edicao. Sem este aviso, o relogio da tela continuaria
        #     respondendo com a maior seriedade sobre um arquivo que nao
        #     existe mais - errado, e em silencio.
        #     Aponta para um arquivo que EXISTE (o audio.wav ao lado) e semeia
        #     o cache com a duracao: assim se mede a regra sem gastar um
        #     ffprobe, e sem depender de haver .m4a no disco - nenhum dos tres
        #     videos de calibracao tem um.
        S._m4a = S.wav
        S._m4a_chave = (S.wav.stat().st_mtime_ns, S.wav.stat().st_size)
        S._m4a_dur = mantido0 + 0.07           # o padding medido do AAC
        certo = S.previa_no_disco(mantido0)
        S._m4a_dur = mantido0 + 0.40           # uma anotacao entrou depois
        caduco = S.previa_no_disco(mantido0)
        diz(certo["tem"] and not certo["velho"] and caduco["velho"],
            "0,07 s de padding passa, 0,40 s de diferenca vira aviso")

        # 14. RESTAURAR ARRASTANDO RESPONDE A PENDENCIA. `pendencias` marca
        #     "ja respondida a mao" pelo `why` de `MAO_MANTEM`, que so a
        #     camada de PALAVRA escreve. Com a onda, "trouxe de volta
        #     arrastando" e a mesma decisao humana, dita em tempo - e sem
        #     isto a pendencia ficaria acesa para sempre depois de respondida
        #     pelo gesto mais direto que o editor oferece.
        S.acao({"acao": "desfazer", "sel": sel})
        iv = S.intervalos()
        pend = tc.pendencias(S.stream, S.C, tc.carrega_vocabulario(d))
        resp = tc.carrega_respostas(d)
        sem, _ = tc.pendencias_sem_resposta(pend, resp, iv)
        alvoq = next((q for q in sem
                      if q["falta"] == "PENDENCIA SEM RESPOSTA"
                      and q["y"] - q["x"] > 0.1), None)
        if alvoq is None:
            print("  --    sem pendencia em aberto para responder, pulado")
        else:
            S.acao({"acao": "restaurar_tempo", "ini": alvoq["x"],
                    "fim": alvoq["y"], "sel": sel})
            iv2 = S.intervalos()
            mao = tc.spans_da_mao(S.stream, S.montagem(),
                                  S.C["corte"]["folga_palavra"])
            sem2, _ = tc.pendencias_sem_resposta(
                tc.pendencias(S.stream, S.C, tc.carrega_vocabulario(d)),
                resp, iv2, mao)
            diz(not any(abs(q["s"] - alvoq["s"]) < 0.01 for q in sem2),
                f"restaurar {alvoq['x']:.2f}-{alvoq['y']:.2f} arrastando "
                f"responde a pendencia {alvoq['rot']!r}")
            #  E nao responde as OUTRAS: uma cobranca que se cala sozinha
            #  por causa de um gesto noutro lugar seria pior que nao existir.
            sobra = {round(q["s"], 2) for q in sem2}
            deviam = {round(q["s"], 2) for q in sem
                      if abs(q["s"] - alvoq["s"]) >= 0.01}
            diz(deviam <= sobra,
                f"as outras {len(deviam)} pendencias continuam cobradas")
            S.acao({"acao": "desfazer", "sel": sel})

        # 15. O QUE A TELA MOSTROU E O QUE O RENDER VAI VER. Se esta linha
        #     cair, o editor virou uma segunda opiniao sobre o corte - que e
        #     exatamente o que ele nao pode ser.
        S.acao({"acao": "ocultar", "sel": sel})
        S.acao({"acao": "cortar_tempo", "ini": a - 0.25, "fim": a, "sel": sel})
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
