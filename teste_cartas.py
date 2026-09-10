#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""teste_cartas.py - os casos de borda de `_agrupa_spans_cartas()` e
`_agrupa_cartas_enumeracao()` que nao dependem de video nenhum.

So existe UM video de teste real para cada modo (IMG_0301 para carta
principal, IMG_0246 para --sem-principal) e nenhum dos dois cobre toda
combinacao de borda (fusao/persistencia num, sessao isolada e reinicio de
pilha no outro) - nao da pra confiar so neles. Este arquivo inventa
`mencoes` com timestamps fabricados e confere a regra pedida pelo Rafael
em 02/09 direto na funcao, sem passar por ingest/transcribe/render.

    venv\\Scripts\\python.exe teste_cartas.py

Usa `iv` de um unico trecho enorme (sem corte nenhum) para que tempo de
FONTE e tempo de SAIDA sejam o mesmo numero - o que se testa aqui e o
agrupamento, nao `_tempo_saida()`.
"""

import sys

import tarocut as tc

IV_SEM_CORTE = [(0.0, 1_000_000.0)]

CC_TODO = tc.cfg()
CC = CC_TODO["cartas"]
ATRASO, PERSIST = CC["atraso"], CC["persistencia"]

falhas = []


def diz(ok, txt):
    print(("  ok    " if ok else "  FALHA ") + txt)
    if not ok:
        falhas.append(txt)


def menc(carta, s, e):
    return {"carta": carta, "s": s, "e": e}


def aprox(a, b, tol=1e-9):
    return abs(a - b) < tol


print(f"== config: atraso={ATRASO}s persistencia={PERSIST}s")

# 1. Duas mencoes da MESMA carta dentro da janela de persistencia: uma
#    exibicao continua, nao duas com fade no meio.
gap_curto = PERSIST - 1.0
m = [menc("A", 10.0, 10.5), menc("A", 10.5 + gap_curto, 10.5 + gap_curto + 0.5)]
spans = tc._agrupa_spans_cartas(m, IV_SEM_CORTE, CC)
diz(len(spans) == 1, f"1: duas mencoes coladas (gap {gap_curto}s) viram 1 span "
    f"(veio {len(spans)})")
if len(spans) == 1:
    s = spans[0]
    diz(aprox(s["ini"], 10.5 + ATRASO), f"1: entrada = fim da 1a mencao + atraso "
        f"(esperado {10.5 + ATRASO:.3f}, veio {s['ini']:.3f})")
    fim_ult = 10.5 + gap_curto + 0.5
    diz(aprox(s["fim"], fim_ult + PERSIST), f"1: saida = fim da ULTIMA mencao + "
        f"persistencia (esperado {fim_ult + PERSIST:.3f}, veio {s['fim']:.3f})")

# 2. Mencao isolada: aparece atraso depois do fim, some persistencia
#    depois do mesmo fim (nenhuma outra mencao para reiniciar o relogio).
m = [menc("B", 30.0, 30.4)]
spans = tc._agrupa_spans_cartas(m, IV_SEM_CORTE, CC)
diz(len(spans) == 1, f"2: mencao isolada gera 1 span (veio {len(spans)})")
if len(spans) == 1:
    s = spans[0]
    diz(aprox(s["ini"], 30.4 + ATRASO), f"2: entrada (esperado {30.4 + ATRASO:.3f}, "
        f"veio {s['ini']:.3f})")
    diz(aprox(s["fim"], 30.4 + PERSIST), f"2: saida = fim + persistencia (esperado "
        f"{30.4 + PERSIST:.3f}, veio {s['fim']:.3f})")

# 3. Duas cartas diferentes coladas: a que comecou primeiro cede o rabo
#    para a que esta sendo falada agora (regra existente, nao mexida).
m = [menc("C", 50.0, 50.3), menc("D", 51.0, 51.2)]
spans = tc._agrupa_spans_cartas(m, IV_SEM_CORTE, CC)
diz(len(spans) == 2, f"3: duas cartas diferentes geram 2 spans (veio {len(spans)})")
if len(spans) == 2:
    c, d = spans
    diz(c["carta"] == "C" and d["carta"] == "D", "3: ordem C antes de D")
    diz(aprox(c["fim"], d["ini"]), f"3: C cede o rabo exatamente onde D comeca "
        f"(C.fim={c['fim']:.3f}, D.ini={d['ini']:.3f})")
    diz(c["fim"] > c["ini"], "3: mesmo cedendo, C ainda aparece por um tempo positivo")

# 4. Sequencia longa da MESMA carta, cada mencao mais perto que a
#    persistencia da anterior: relogio reinicia a cada uma, span final
#    vai ate persistencia depois da ULTIMA.
gap = PERSIST - 1.8
t = 70.0
mencoes_seq = []
for _ in range(5):
    mencoes_seq.append(menc("E", t, t + 0.3))
    t += 0.3 + gap
spans = tc._agrupa_spans_cartas(mencoes_seq, IV_SEM_CORTE, CC)
diz(len(spans) == 1, f"4: sequencia longa colada vira 1 span so (veio {len(spans)})")
if len(spans) == 1:
    s = spans[0]
    primeira_fim = mencoes_seq[0]["e"]
    ultima_fim = mencoes_seq[-1]["e"]
    diz(aprox(s["ini"], primeira_fim + ATRASO), f"4: entrada vem da PRIMEIRA mencao "
        f"(esperado {primeira_fim + ATRASO:.3f}, veio {s['ini']:.3f})")
    diz(aprox(s["fim"], ultima_fim + PERSIST), f"4: saida vem da ULTIMA mencao, nao "
        f"da primeira (esperado {ultima_fim + PERSIST:.3f}, veio {s['fim']:.3f})")

# 5. Controle: mesma carta, mas gap MAIOR que a persistencia - tem que
#    separar em dois spans, nao fundir.
gap_longo = PERSIST + 1.0
m = [menc("F", 90.0, 90.3), menc("F", 90.3 + gap_longo, 90.3 + gap_longo + 0.3)]
spans = tc._agrupa_spans_cartas(m, IV_SEM_CORTE, CC)
diz(len(spans) == 2, f"5: gap maior que a persistencia NAO funde (veio "
    f"{len(spans)} span(s), esperado 2)")

# ---- _agrupa_cartas_enumeracao (--sem-principal, layout 4) ---------------
# Mesma razao do bloco acima: so ha UM video de teste real (IMG_0246) e ele
# nao cobre os casos de borda (sessao isolada de 1 carta, gap grande entre
# sessoes) que fazem o agrupamento errar calado.

CE = tc.cfg()["cartas"]["enumeracao"]
FADE_SAIDA = CE["fade_saida"]

print(f"\n== enumeracao: atraso={ATRASO}s persistencia={PERSIST}s "
      f"fade_saida={FADE_SAIDA}s")

# 6. Cinco mencoes coladas (o caso medido no IMG_0246: "rainha, rei,
#    cavaleiro, cavaleira, pajens" em sequencia) alternam de lado e
#    empilham 3 na esquerda (a 1a, 3a, 5a), 2 na direita (a 2a, 4a).
t = 87.11
nomes = ["Rainha", "Rei", "Cavaleiro", "Cavaleira", "Pajens"]
m = []
for nome in nomes:
    m.append(menc(nome, t, t + 0.7))
    t += 0.7 + 0.3  # gap curto entre mencoes, bem dentro da persistencia
eventos, sessoes = tc._agrupa_cartas_enumeracao(m, IV_SEM_CORTE, CC, CE)
diz(len(eventos) == 5, f"6: 5 mencoes coladas viram 5 eventos (veio {len(eventos)})")
if len(eventos) == 5:
    lados_esperados = ["esquerda", "direita", "esquerda", "direita", "esquerda"]
    profs_esperadas = [0, 0, 1, 1, 2]
    diz([e["lado"] for e in eventos] == lados_esperados,
        f"6: alterna esquerda/direita a cada mencao nova "
        f"(veio {[e['lado'] for e in eventos]})")
    diz([e["prof"] for e in eventos] == profs_esperadas,
        f"6: empilha (profundidade sobe 1 a cada volta ao mesmo lado) "
        f"(veio {[e['prof'] for e in eventos]})")
    diz([e["carta"] for e in eventos] == nomes,
        "6: cada evento guarda a carta certa, na ordem da fala")
diz(len(sessoes) == 1, f"6: as 5 mencoes formam 1 sessao so (veio {len(sessoes)})")

# 7. Saida em duas fases: a pilha da DIREITA some primeiro (comeca a sumir
#    `persistencia` depois da ULTIMA mencao da sessao), a da ESQUERDA so
#    depois que a direita ja sumiu de vez (+`fade_saida`).
if len(eventos) == 5:
    ultima_fim = m[-1]["e"]
    dir_ini_esperado = ultima_fim + PERSIST
    dir_fim_esperado = dir_ini_esperado + FADE_SAIDA
    esq_ini_esperado = dir_fim_esperado
    esq_fim_esperado = esq_ini_esperado + FADE_SAIDA
    dirs = [e for e in eventos if e["lado"] == "direita"]
    esqs = [e for e in eventos if e["lado"] == "esquerda"]
    diz(all(aprox(e["fade_ini"], dir_ini_esperado) for e in dirs),
        f"7: direita comeca a sumir em fim_ultima+persistencia "
        f"(esperado {dir_ini_esperado:.3f})")
    diz(all(aprox(e["fim"], dir_fim_esperado) for e in dirs),
        f"7: direita termina de sumir {FADE_SAIDA}s depois de comecar "
        f"(esperado {dir_fim_esperado:.3f})")
    diz(all(aprox(e["fade_ini"], esq_ini_esperado) for e in esqs),
        f"7: esquerda so comeca a sumir quando a direita ja sumiu de vez "
        f"(esperado {esq_ini_esperado:.3f})")
    diz(all(aprox(e["fim"], esq_fim_esperado) for e in esqs),
        f"7: esquerda termina de sumir {FADE_SAIDA}s depois (esperado "
        f"{esq_fim_esperado:.3f})")

# 8. Mencao isolada: 1 evento so, esquerda/profundidade 0 - nao ha
#    segunda mencao para ir para a direita.
m = [menc("Torre", 200.0, 200.5)]
eventos, sessoes = tc._agrupa_cartas_enumeracao(m, IV_SEM_CORTE, CC, CE)
diz(len(eventos) == 1, f"8: mencao isolada gera 1 evento (veio {len(eventos)})")
if len(eventos) == 1:
    diz(eventos[0]["lado"] == "esquerda" and eventos[0]["prof"] == 0,
        f"8: mencao isolada vai para esquerda/prof 0 (veio "
        f"{eventos[0]['lado']}/{eventos[0]['prof']})")

# 9. Duas sessoes bem separadas (gap MAIOR que persistencia): a segunda
#    reinicia do zero (esquerda/prof 0 outra vez), nao continua a
#    profundidade onde a primeira parou.
gap_longo = PERSIST + 5.0
m = [menc("A", 10.0, 10.3), menc("B", 10.3 + gap_longo, 10.3 + gap_longo + 0.3),
     menc("C", 10.3 + gap_longo + 1.0, 10.3 + gap_longo + 1.3)]
eventos, sessoes = tc._agrupa_cartas_enumeracao(m, IV_SEM_CORTE, CC, CE)
diz(len(sessoes) == 2, f"9: gap maior que a persistencia separa em 2 sessoes "
    f"(veio {len(sessoes)})")
if len(eventos) == 3:
    diz(eventos[0]["lado"] == "esquerda" and eventos[0]["prof"] == 0,
        "9: 1a sessao comeca esquerda/prof 0")
    diz(eventos[1]["lado"] == "esquerda" and eventos[1]["prof"] == 0,
        f"9: 2a sessao REINICIA em esquerda/prof 0, nao continua de onde "
        f"a 1a parou (veio {eventos[1]['lado']}/{eventos[1]['prof']})")
    diz(eventos[2]["lado"] == "direita" and eventos[2]["prof"] == 0,
        "9: 2a mencao da 2a sessao vai para direita/prof 0")

# 10. _agrupa_camadas: duas sessoes que colocam a MESMA carta
#     na MESMA posicao de pilha (esquerda/prof 0) viram 1 camada so, com
#     2 ocorrencias - nao 2 camadas (e o que faz o overlay caro por
#     camada nao dobrar de custo so porque a carta se repete).
gap_longo = PERSIST + 5.0
m = [menc("Rainha de Copas", 10.0, 10.3),
     menc("Rainha de Copas", 10.3 + gap_longo, 10.3 + gap_longo + 0.3)]
eventos, _ = tc._agrupa_cartas_enumeracao(m, IV_SEM_CORTE, CC, CE)
camadas = tc._agrupa_camadas(eventos)
diz(len(eventos) == 2, f"10: 2 sessoes isoladas geram 2 eventos (veio {len(eventos)})")
diz(len(camadas) == 1, f"10: mesma carta na mesma posicao vira 1 camada so "
    f"(veio {len(camadas)})")
if len(camadas) == 1:
    diz(len(camadas[0]["ocorrencias"]) == 2,
        f"10: a camada guarda as 2 ocorrencias, cada uma com seu proprio "
        f"fade (veio {len(camadas[0]['ocorrencias'])})")

# 11. Controle: duas cartas DIFERENTES na mesma posicao (esquerda/prof 0,
#     sessoes diferentes) viram 2 camadas - a posicao repete, a carta nao.
m = [menc("Rainha de Copas", 10.0, 10.3),
     menc("Rei de Copas", 10.3 + gap_longo, 10.3 + gap_longo + 0.3)]
eventos, _ = tc._agrupa_cartas_enumeracao(m, IV_SEM_CORTE, CC, CE)
camadas = tc._agrupa_camadas(eventos)
diz(len(camadas) == 2, f"11: cartas diferentes na mesma posicao NAO fundem "
    f"(veio {len(camadas)} camada(s), esperado 2)")

# 12. Repeticao DENTRO da mesma sessao (pedido do Rafael em 02/09: nao
#     pode ter a mesma carta na tela duas vezes) - "Rainha, Rei, Rainha,
#     Cavaleiro" coladas: a 3a mencao (Rainha de novo) e ignorada, entao
#     so 3 eventos saem, e o Cavaleiro (a 4a fala, mas o 3o EVENTO) vai
#     para o lado que a CONTAGEM DE EVENTOS manda (esquerda), nao o lado
#     que a posicao na fala mandaria (direita, se a repetida contasse).
t = 200.0
m = [menc("Rainha", t, t + 0.5), menc("Rei", t + 1.0, t + 1.5),
     menc("Rainha", t + 2.0, t + 2.5), menc("Cavaleiro", t + 3.0, t + 3.5)]
eventos, _ = tc._agrupa_cartas_enumeracao(m, IV_SEM_CORTE, CC, CE)
diz(len(eventos) == 3, f"12: mencao repetida na mesma sessao NAO gera "
    f"evento (veio {len(eventos)}, esperado 3)")
if len(eventos) == 3:
    diz([e["carta"] for e in eventos] == ["Rainha", "Rei", "Cavaleiro"],
        f"12: a carta repetida some da lista, as outras mantem a ordem "
        f"(veio {[e['carta'] for e in eventos]})")
    diz(eventos[2]["lado"] == "esquerda" and eventos[2]["prof"] == 1,
        f"12: o Cavaleiro (3o evento de verdade) vai pra esquerda/prof 1, "
        f"nao direita (a mencao ignorada nao consome lado nem "
        f"profundidade) (veio {eventos[2]['lado']}/{eventos[2]['prof']})")


# ---- roteiro escrito a mao (o "modo cartas" do editor) -------------------
# O outro autor das cartas. `cmd_cartas` adivinha pela fala; aqui quem
# escreve e ele, e o que quebra calado e diferente: uma linha que o parser
# nao entende e some, uma pilha que se sobrepoe, um tempo que caiu nos
# segundos finais onde nao ha carta. Nenhuma dessas aparece na previa como
# erro - aparece como carta que nao esta la, e ninguem conta as cartas que
# faltam.

DUR = 200.0                       # duracao do CORTE (relogio da saida)
ENTRA, RABO = CC["principal_entra"], CC["rabo_final"]
FADE = CC["fade"]
NOMES = tc.catalogo_cartas()

print(f"\n== roteiro a mao: principal_entra={ENTRA}s rabo_final={RABO}s "
      f"fade={FADE}s ({len(NOMES)} cartas no catalogo)")

# 13. As 78 cartas do baralho, e o autocomplete da tela sai desta lista.
diz(len(NOMES) == 78, f"13: o catalogo tem as 78 cartas (veio {len(NOMES)})")

# 14. A carta principal, sozinha: esquerda, fundo da pilha, do
#     `principal_entra` ao `rabo_final`. E o unico evento que nao vem de
#     uma linha escrita.
ev, er, no = tc.parse_roteiro("", "2 de Espadas", DUR, CC_TODO, NOMES)
diz(len(ev) == 1 and not er, f"14: principal sozinha gera 1 evento sem erro "
    f"(veio {len(ev)} evento(s), {len(er)} erro(s))")
if len(ev) == 1:
    p = ev[0]
    diz(p["lado"] == "esquerda" and p["prof"] == 0,
        f"14: a principal e a ancora - esquerda, fundo da pilha (veio "
        f"{p['lado']}/{p['prof']})")
    diz(aprox(p["ini"], ENTRA, 1e-6),
        f"14: entra em {ENTRA}s (veio {p['ini']})")
    diz(aprox(p["fade_ini"], DUR - RABO, 1e-6),
        f"14: comeca a sumir {RABO}s antes do fim (esperado {DUR - RABO}, "
        f"veio {p['fade_ini']})")

# 15. Sem tempo declarado, qualquer carta vai ate o mesmo lugar: os
#     `rabo_final` segundos finais sao sem carta, e isso vale para todas.
ev, er, no = tc.parse_roteiro("[0:30] A Torre; direita", "", DUR, CC_TODO, NOMES)
diz(len(ev) == 1 and aprox(ev[0]["fade_ini"], DUR - RABO, 1e-6),
    f"15: carta sem tempo vai ate o rabo do video (esperado {DUR - RABO})")

# 16. Com tempo, `Xs` e o tempo VISIVEL - o fade comeca depois dele.
ev, er, no = tc.parse_roteiro("[0:30] A Torre; direita; 12s", "", DUR, CC_TODO, NOMES)
diz(len(ev) == 1 and aprox(ev[0]["fade_ini"], 42.0, 1e-6),
    f"16: '12s' conta a partir do tempo escrito (esperado 42,0, veio "
    f"{ev[0]['fade_ini'] if ev else '-'})")
diz(len(ev) == 1 and aprox(ev[0]["fim"], 42.0 + FADE, 1e-6),
    f"16: e o fade de saida vem DEPOIS dos 12s, nao dentro deles")

# 17. Tempo que passa do rabo e aparado - com NOTA, nunca calado. Encurtar
#     o pedido de alguem em silencio e o defeito que este programa persegue.
ev, er, no = tc.parse_roteiro("[0:30] A Torre; direita; 400s", "", DUR, CC_TODO, NOMES)
diz(len(ev) == 1 and aprox(ev[0]["fade_ini"], DUR - RABO, 1e-6),
    "17: tempo longo demais e aparado no rabo do video")
diz(len(no) == 1 and no[0]["linha"] == 1,
    f"17: e a aparagem vira NOTA na linha (veio {len(no)} nota(s) "
    f"na linha {no[0]['linha'] if no else '-'})")

# 18. A pilha se CONTA, nao se digita: a 2a carta do mesmo lado empilha
#     sobre a 1a enquanto ela estiver viva.
ev, er, no = tc.parse_roteiro(
    "[0:30] A Torre; esquerda\n[0:40] A Morte; esquerda", "", DUR, CC_TODO, NOMES)
diz([e["prof"] for e in ev] == [0, 1],
    f"18: segunda carta do mesmo lado empilha (veio {[e['prof'] for e in ev]})")

# 19. E DESCE quando a de baixo sai. Sem isso, um video comprido faria a
#     pilha marchar para o centro a cada carta nova.
ev, er, no = tc.parse_roteiro(
    "[0:30] A Torre; esquerda; 5s\n[1:00] A Morte; esquerda", "", DUR, CC_TODO, NOMES)
diz([e["prof"] for e in ev] == [0, 0],
    f"19: a pilha desce quando a de baixo ja saiu (veio "
    f"{[e['prof'] for e in ev]})")

# 20. Com principal, a carta a esquerda empilha SOBRE ela - a principal
#     ocupa o fundo o video inteiro.
ev, er, no = tc.parse_roteiro("[0:30] A Torre; esquerda", "A Morte", DUR,
                               CC_TODO, NOMES)
diz([e["prof"] for e in ev] == [0, 1] and ev[0]["carta"] == "A Morte",
    f"20: com principal, a carta da esquerda empilha sobre ela (veio "
    f"{[(e['carta'], e['prof']) for e in ev]})")

# 21. Carta a direita = layout 3 (Mariana ao centro). A janela de
#     reenquadramento sai das cartas da DIREITA e de mais nada.
ev, er, no = tc.parse_roteiro(
    "[0:30] A Torre; esquerda\n[1:00] A Morte; direita; 10s", "", DUR,
    CC_TODO, NOMES)
jan = tc.janelas_reenquadra(ev, CC["junta_janela"])
diz(len(jan) == 1 and aprox(jan[0][0], 60.0, 1e-6),
    f"21: so a carta da direita abre janela de reenquadramento (veio {jan})")

# 22. Duas janelas coladas viram uma: sem isso o enquadramento piscaria
#     entre uma carta e a seguinte.
ev, er, no = tc.parse_roteiro(
    "[0:30] A Torre; direita; 10s\n[0:41] A Morte; direita; 10s", "", DUR,
    CC_TODO, NOMES)
jan = tc.janelas_reenquadra(ev, CC["junta_janela"])
diz(len(jan) == 1, f"22: duas cartas a direita quase coladas dao UMA janela "
    f"de reenquadramento (veio {len(jan)})")

# 23. A ordem do overlay e por PROFUNDIDADE, e nao por chegada: e o que
#     mantem "a mais funda por cima" quando a pilha desce (caso 19).
ev, er, no = tc.parse_roteiro(
    "[0:30] A Torre; esquerda\n[0:35] A Morte; esquerda; 5s\n"
    "[1:00] O Diabo; esquerda", "", DUR, CC_TODO, NOMES)
cam = tc._agrupa_camadas(ev)
diz([c["prof"] for c in cam] == sorted(c["prof"] for c in cam),
    f"23: as camadas saem em ordem de profundidade (veio "
    f"{[(c['carta'], c['prof']) for c in cam]})")

# ---- o que o roteiro RECUSA ---------------------------------------------
# Linha que o programa nao entende nao pode virar carta que nao aparece:
# o export recusa enquanto houver erro, e o editor os mostra na linha.

# 24. Nome de carta que nao existe.
ev, er, no = tc.parse_roteiro("[0:30] Rainha de Bastoes; direita", "", DUR,
                               CC_TODO, NOMES)
diz(not ev and len(er) == 1 and er[0]["linha"] == 1,
    f"24: carta fora do baralho vira erro na linha, nao evento "
    f"(veio {len(ev)} evento(s), {len(er)} erro(s))")

# 25. Linha sem tempo.
ev, er, no = tc.parse_roteiro("A Torre; direita", "", DUR, CC_TODO, NOMES)
diz(not ev and len(er) == 1, "25: linha sem tempo vira erro")

# 26. Tempo dentro dos segundos finais - a carta nunca apareceria.
ev, er, no = tc.parse_roteiro("[3:15] A Torre; direita", "", DUR, CC_TODO, NOMES)
diz(not ev and len(er) == 1,
    f"26: tempo dentro do rabo do video vira erro (veio {len(er)} erro(s))")

# 27. A MESMA carta duas vezes na tela ao mesmo tempo (a regra que o modo
#     enumeracao ja tinha): duas copias da mesma ilustracao deslocadas de
#     1% parecem defeito de render, nao empilhamento.
ev, er, no = tc.parse_roteiro(
    "[0:30] A Torre; esquerda\n[0:40] A Torre; direita", "", DUR, CC_TODO, NOMES)
diz(len(ev) == 1 and len(er) == 1,
    f"27: a mesma carta sobreposta consigo mesma vira erro "
    f"(veio {len(ev)} evento(s), {len(er)} erro(s))")

# 28. A mesma carta DEPOIS que a primeira saiu e legitima.
ev, er, no = tc.parse_roteiro(
    "[0:30] A Torre; esquerda; 5s\n[1:00] A Torre; direita", "", DUR,
    CC_TODO, NOMES)
diz(len(ev) == 2 and not er,
    f"28: a mesma carta de novo, depois que a primeira saiu, e legitima "
    f"(veio {len(ev)} evento(s), {len(er)} erro(s))")

# 29. Comentario e linha vazia nao viram nada nem erram.
ev, er, no = tc.parse_roteiro("# so um lembrete\n\n   \n[0:30] A Torre; direita",
                               "", DUR, CC_TODO, NOMES)
diz(len(ev) == 1 and not er, "29: comentario e linha vazia sao ignorados")

# 30. Os erros saem em ordem de LINHA - eles vao para a margem do editor,
#     ao lado da linha de que falam, e a checagem da pilha corre depois da
#     leitura de todas as linhas.
ev, er, no = tc.parse_roteiro(
    "[0:30] A Torre; esquerda\n[0:35] A Torre; direita\n[9:99] Nada; direita",
    "", DUR, CC_TODO, NOMES)
diz([e["linha"] for e in er] == sorted(e["linha"] for e in er),
    f"30: os erros saem em ordem de linha (veio {[e['linha'] for e in er]})")

# 31. O tempo se le nos formatos que ele escreve, e num lugar so (o mesmo
#     `le_tempo` do `conferir --em`).
diz(aprox(tc.le_tempo("1:29"), 89.0) and aprox(tc.le_tempo("01:29,5".replace(",", ".")), 89.5)
    and aprox(tc.le_tempo("0:01:29"), 89.0) and aprox(tc.le_tempo("89"), 89.0),
    "31: le_tempo entende 1:29, 01:29.5, 0:01:29 e 89")
diz(tc.le_tempo("banana") is None and tc.le_tempo("") is None,
    "31: le_tempo devolve None no que nao e tempo, em vez de chutar")


# ---- legenda ------------------------------------------------------------
# O .srt sai do MESMO corte que o video. O que quebra calado aqui: uma
# palavra que sobreviveu ao corte e nao entrou no cartao (legenda que diz
# menos que o audio), um cartao que atravessa uma emenda com o tempo da
# fonte, uma edicao a mao que se perde quando o corte muda.

CL = CC_TODO["legenda"]
print(f"\n== legenda: max {CL['max_caracteres']} caracteres, "
      f"{CL['min_segundos']}-{CL['max_segundos']}s por cartao")


def tok(t, s, e, k="palavra"):
    return {"k": k, "t": t, "s": s, "e": e, "cut": False}


# 32. O basico: o texto que sobra vira cartao, no relogio da SAIDA.
st = [tok("Uma", 0.0, 0.4), tok("frase", 0.4, 0.9), tok("curta.", 0.9, 1.4)]
cenas = tc.monta_legendas(st, [(0.0, 1.4)], CC_TODO)
diz(len(cenas) == 1 and cenas[0]["txt"] == "Uma frase curta.",
    f"32: as palavras viram um cartao (veio {[c['txt'] for c in cenas]})")

# 33. O corte anda o relogio: uma palavra depois de um trecho cortado sai
#     no tempo da SAIDA, nao no da fonte. Sem isso a legenda inteira
#     escorrega depois da primeira emenda.
st = [tok("Antes", 0.0, 0.5), tok("depois", 10.0, 10.5)]
cenas = tc.monta_legendas(st, [(0.0, 0.5), (10.0, 10.5)], CC_TODO)
diz(len(cenas) == 1 and aprox(cenas[0]["ini"], 0.0, 1e-6)
    and aprox(cenas[0]["fim"], 1.0, 1e-6),
    f"33: o cartao sai no relogio da saida, nao no da fonte "
    f"(veio {cenas[0]['ini']:.2f}-{cenas[0]['fim']:.2f}, esperado 0,00-1,00)")

# 34. Palavra cortada pela METADE nao entra - e a mesma regra da metade do
#     token com que o editor decide o que um arrasto pegou.
st = [tok("fica", 0.0, 1.0), tok("sai", 1.0, 2.0)]
cenas = tc.monta_legendas(st, [(0.0, 1.2)], CC_TODO)
diz(len(cenas) == 1 and cenas[0]["txt"] == "fica",
    f"34: palavra que sobrevive menos da metade nao entra na legenda "
    f"(veio {[c['txt'] for c in cenas]})")

# 35. Pausa longa fecha o cartao mesmo com espaco de sobra.
st = [tok("Antes", 0.0, 0.5), tok("depois", 3.0, 3.5)]
cenas = tc.monta_legendas(st, [(0.0, 4.0)], CC_TODO)
diz(len(cenas) == 2, f"35: a pausa longa fecha o cartao (veio {len(cenas)})")

# 36. Cartao nao passa do teto de caracteres.
st = [tok("palavra", i * 0.3, i * 0.3 + 0.25) for i in range(40)]
cenas = tc.monta_legendas(st, [(0.0, 40 * 0.3)], CC_TODO)
diz(all(len(c["txt"]) <= CL["max_caracteres"] for c in cenas),
    f"36: nenhum cartao passa de {CL['max_caracteres']} caracteres "
    f"(maior veio com {max(len(c['txt']) for c in cenas)})")
diz(all(c["fim"] - c["ini"] >= CL["min_segundos"] - 1e-6
        or c is cenas[-1] for c in cenas),
    "36: nenhum cartao pisca abaixo do minimo")

# 37. Rotulo do programa nao e fala: "hesitação" e "?? 1.2s" nao entram na
#     legenda. Sao etiqueta, e ninguem leu nada ali.
st = [tok("Isto", 0.0, 0.4), tok("hesitação", 0.4, 1.0, "hesitacao"),
      tok("?? 1.2s", 1.0, 2.2, "hesitacao"), tok("fica.", 2.2, 2.6)]
cenas = tc.monta_legendas(st, [(0.0, 2.6)], CC_TODO)
todo = " ".join(c["txt"] for c in cenas)
diz("hesita" not in todo and "??" not in todo and "Isto" in todo
    and "fica." in todo,
    f"37: rotulo do programa nao vira legenda (veio "
    f"{[c['txt'] for c in cenas]})")
# ...e os 1,8s de audio ilegivel entre as duas palavras separam os cartoes,
# como qualquer outra pausa: juntar "Isto" e "fica." num cartao so faria a
# legenda afirmar uma frase que ninguem sabe se foi dita assim.
diz(len(cenas) == 2, f"37: e o buraco entre elas quebra o cartao "
    f"(veio {len(cenas)})")

# 38. Fala RECUPERADA de uma lacuna entra: e fala, e esta no video.
st = [tok("Isto", 0.0, 0.4), tok("tambem conta", 0.4, 1.4, "hesitacao")]
cenas = tc.monta_legendas(st, [(0.0, 1.4)], CC_TODO)
diz(len(cenas) == 1 and "tambem conta" in cenas[0]["txt"],
    f"38: fala recuperada de lacuna entra na legenda (veio "
    f"{[c['txt'] for c in cenas]})")

# 39. A edicao a mao casa pelo TEMPO DE FONTE, e nao pelo numero do
#     cartao: um corte novo renumera todos os de baixo e a legenda escrita
#     para um passaria a valer para outro.
st = [tok("Primeiro", 0.0, 0.5), tok("cartao.", 0.5, 1.2),
      tok("Segundo", 4.0, 4.6), tok("cartao.", 4.6, 5.2)]
iv = [(0.0, 6.0)]
cenas = tc.monta_legendas(st, iv, CC_TODO)
alvo = cenas[-1]
mao = [{"s": alvo["s"], "e": alvo["e"], "orig": alvo["auto"],
        "novo": "escrito a mao"}]
cenas2 = tc.monta_legendas(st, iv, CC_TODO)
orfas = tc.aplica_legendas(cenas2, mao)
diz(not orfas and cenas2[-1]["txt"] == "escrito a mao" and cenas2[-1]["mao"],
    f"39: a edicao a mao volta no cartao certo (veio "
    f"{cenas2[-1]['txt']!r}, {len(orfas)} orfa(s))")
diz(cenas2[0]["txt"] == cenas2[0]["auto"] and not cenas2[0]["mao"],
    "39: e nao vaza para o cartao vizinho")

# 40. Edicao que nao acha mais cartao nenhum volta como ORFA. Sumir com o
#     que alguem escreveu, calado, e o defeito de sempre.
orfas = tc.aplica_legendas(tc.monta_legendas(st, iv, CC_TODO),
                           [{"s": 999.0, "e": 999.5, "orig": "x", "novo": "y"}])
diz(len(orfas) == 1, f"40: edicao sem cartao correspondente volta como orfa "
    f"(veio {len(orfas)})")

# 41. O .srt: numeracao de 1, virgula no milissegundo, no maximo 2 linhas.
diz(tc.srt_tempo(0) == "00:00:00,000" and tc.srt_tempo(89.5) == "00:01:29,500"
    and tc.srt_tempo(3723.004) == "01:02:03,004",
    f"41: o tempo do .srt sai no formato do formato (veio "
    f"{tc.srt_tempo(89.5)})")
longo = "palavra " * 12
q = tc.quebra_legenda(longo, CL["max_linha"])
diz(q.count(chr(10)) == 1 and all(len(x) <= CL["max_linha"] + 8
                                   for x in q.split(chr(10))),
    f"41: texto largo quebra em DUAS linhas, nao tres (veio "
    f"{q.count(chr(10)) + 1} linha(s))")
diz(tc.quebra_legenda("curto", CL["max_linha"]) == "curto",
    "41: texto que cabe nao ganha quebra")

# ---- audio do cartao ------------------------------------------------------
# O botao de tocar de um cartao nao pode parar no fim do proprio texto: o
# achado do Rafael (09/09) foi o modo cartas "pulando" audio que o modo onda
# tocava direto - um restauro de milissegundo colado a uma emenda, ou uma
# palavra cujo tempo o alinhador errou, sobrevivem ao corte sem que NENHUMA
# palavra os reivindique (a regra da metade do token exclui o texto, nao o
# som). Ver o docstring de `amplia_audio_legendas` para os dois casos MEDIDOS
# ("3 de espadas", "4 de espadas") que motivaram isto.

print("\n== audio do cartao: o botao de tocar alcanca mais que o texto")

# 42. Mesmo ponto de partida do teste 34: "sai" sobrevive so 20% e nao entra
#     na legenda. Sem cartao vizinho, o unico cartao tem de tocar da ponta a
#     ponta do video - e la que aqueles 0,2s moram.
st = [tok("fica", 0.0, 1.0), tok("sai", 1.0, 2.0)]
cenas = tc.monta_legendas(st, [(0.0, 1.2)], CC_TODO)
tc.amplia_audio_legendas(cenas, 2.0)
diz(len(cenas) == 1 and cenas[0]["txt"] == "fica",
    "42: mesmo ponto de partida do teste 34 - 'sai' nao entra na legenda")
diz(aprox(cenas[0]["audio_s"], 0.0) and aprox(cenas[0]["audio_e"], 2.0),
    f"42: sem cartao vizinho, o unico cartao toca da ponta a ponta do "
    f"video (veio {cenas[0]['audio_s']:.2f}-{cenas[0]['audio_e']:.2f})")

# 43. Dois cartoes com um RESTO entre eles: a hesitacao do meio sobrevive so
#     10% (abaixo dos 50% que dao entrada na legenda) - a mesma forma do
#     'elaborar,' seguido de 'e elabora.' no "4 de espadas" (34%, tambem
#     abaixo do piso). Os dois cartoes de TEXTO tem de repartir esse resto
#     entre si sem sobra e sem buraco.
st = [tok("Um", 0.0, 0.5), tok("dois", 0.5, 1.0),
      tok("resto", 1.0, 10.0, "hesitacao"), tok("Depois", 12.0, 12.5),
      tok("disso.", 12.5, 13.0)]
iv = [(0.0, 1.0), (8.0, 8.9), (12.0, 13.0)]
cenas = tc.monta_legendas(st, iv, CC_TODO)
diz(len(cenas) == 2 and cenas[0]["txt"] == "Um dois"
    and cenas[1]["txt"] == "Depois disso.",
    f"43: a hesitacao do meio (10% de sobrevida) nao vira cartao, e o "
    f"buraco de saida que ela deixa quebra o cartao em dois "
    f"(veio {[c['txt'] for c in cenas]})")
tc.amplia_audio_legendas(cenas, 13.0)
diz(aprox(cenas[0]["audio_s"], 0.0) and aprox(cenas[1]["audio_e"], 13.0),
    "43: as pontas tocam da borda do video, nao da propria palavra")
diz(aprox(cenas[0]["audio_e"], cenas[1]["audio_s"]),
    f"43: o fim de um cartao E o comeco do proximo, byte a byte - sem "
    f"isso o resto da hesitacao (8,0-8,9s) nao tem botao de tocar em "
    f"lugar nenhum (veio {cenas[0]['audio_e']:.3f} != "
    f"{cenas[1]['audio_s']:.3f})")


def _recorta_teste(ini, fim, iv):
    total = 0.0
    for a, b in iv:
        x, y = max(a, ini), min(b, fim)
        if y > x:
            total += y - x
    return total


total_iv = sum(b - a for a, b in iv)
total_cartoes = sum(_recorta_teste(c["audio_s"], c["audio_e"], iv) for c in cenas)
diz(aprox(total_iv, total_cartoes),
    f"43: cartao por cartao, em ordem, toca `iv` inteiro uma vez cada "
    f"pedaco - nada falta e nada dobra (iv tem {total_iv:.3f}s, os "
    f"cartoes somam {total_cartoes:.3f}s)")

# ---- midia avulsa e tela cheia no roteiro (09/09/2026) -------------------
# A mesma sintaxe do roteiro, para foto/grafico que nao e carta do baralho.
# Midia nao entra no catalogo_midias() real (nao ha arquivo em midias/ so
# para o teste) - `parse_roteiro` aceita a lista por parametro, do mesmo
# jeito que ja aceita `nomes` fabricado; ver a lista MIDIAS logo abaixo.

MIDIAS = ["grafico.png", "mapa-astral.jpg", "outro.png"]
FADE_FORA = CC["midia"]["fade_cartas"]
FADE_POP = tc.FADE_POP

print(f"\n== midia/tela cheia: fade_cartas={FADE_FORA}s fade_pop={FADE_POP}s "
      f"({len(MIDIAS)} midia(s) de teste)")

# 44. Midia comum entra na pilha da direita, sem lado escrito - ela nunca
#     aceita lado, ponto 2 do pedido do Rafael.
ev, er, no = tc.parse_roteiro("[0:30] grafico.png; 12s", "", DUR, CC_TODO,
                               NOMES, MIDIAS)
diz(len(ev) == 1 and not er, f"44: midia comum gera 1 evento sem erro "
    f"(veio {len(ev)} evento(s), {len(er)} erro(s))")
if len(ev) == 1:
    e = ev[0]
    diz(e["lado"] == "direita" and e["tipo"] == "midia",
        f"44: vai para a direita, tipo midia (veio {e['lado']}/{e['tipo']})")
    diz(aprox(e["fade_ini"], 42.0, 1e-6),
        f"44: 'Xs' conta do tempo escrito, igual carta (esperado 42.0, "
        f"veio {e['fade_ini']})")

# 45. Midia RECUSA lado escrito - 'direita'/'esquerda' nao estao no
#     vocabulario dela (so 'full' e o tempo).
ev, er, no = tc.parse_roteiro("[0:30] grafico.png; direita", "", DUR,
                               CC_TODO, NOMES, MIDIAS)
diz(not ev and len(er) == 1,
    f"45: midia com lado escrito vira erro, nao evento (veio {len(ev)} "
    f"evento(s), {len(er)} erro(s))")

# 46. Midia empilha com carta do mesmo lado - MESMO mecanismo de pilha,
#     midia e carta na mesma posicao (direita) contam juntas.
ev, er, no = tc.parse_roteiro(
    "[0:30] A Torre; direita\n[0:40] grafico.png", "", DUR, CC_TODO,
    NOMES, MIDIAS)
diz([e["prof"] for e in ev] == [0, 1],
    f"46: midia empilha sobre carta do mesmo lado (veio "
    f"{[e['prof'] for e in ev]})")

# 47. 'full' exige Xs - sem duracao, erro (nao "fica ate o rabo_final",
#     que esconderia carta por acidente pelo video quase inteiro).
ev, er, no = tc.parse_roteiro("[0:30] mapa-astral.jpg; full", "", DUR,
                               CC_TODO, NOMES, MIDIAS)
diz(not ev and len(er) == 1,
    f"47: 'full' sem tempo vira erro (veio {len(er)} erro(s))")

# 48. 'full' basico: tipo, janela e pop de entrada/saida (FADE_POP, nao o
#     fade macio da carta - quem fadeia macio sao as cartas em volta).
ev, er, no = tc.parse_roteiro("[0:30] mapa-astral.jpg; full; 8s", "", DUR,
                               CC_TODO, NOMES, MIDIAS)
diz(len(ev) == 1 and not er, f"48: 'full' basico gera 1 evento sem erro "
    f"(veio {len(ev)} evento(s), {len(er)} erro(s))")
if len(ev) == 1:
    e = ev[0]
    diz(e["tipo"] == "full", f"48: tipo == full (veio {e['tipo']})")
    diz(aprox(e["ini"], 30.0) and aprox(e["fade_ini"], 38.0),
        f"48: fica em tela dos 30s aos 38s (veio {e['ini']}-{e['fade_ini']})")
    diz(aprox(e["fade_dur"], FADE_POP) and aprox(e["fade_in"], FADE_POP),
        f"48: entra/sai em pop curto (FADE_POP={FADE_POP}), nao fade macio "
        f"(veio fade_dur={e['fade_dur']}, fade_in={e['fade_in']})")

# 49. Duas imagens full que se cobrem no tempo - recusa a segunda.
ev, er, no = tc.parse_roteiro(
    "[0:30] mapa-astral.jpg; full; 20s\n[0:40] grafico.png; full; 5s", "",
    DUR, CC_TODO, NOMES, MIDIAS)
diz(len(ev) == 1 and len(er) == 1,
    f"49: duas imagens full sobrepostas - so a 1a sobrevive (veio "
    f"{len(ev)} evento(s), {len(er)} erro(s))")

# 50. Duas imagens full em sequencia (sem se cobrir) - as duas legitimas.
ev, er, no = tc.parse_roteiro(
    "[0:30] mapa-astral.jpg; full; 5s\n[1:00] grafico.png; full; 5s", "",
    DUR, CC_TODO, NOMES, MIDIAS)
diz(len(ev) == 2 and not er,
    f"50: duas imagens full em sequencia sao legitimas (veio {len(ev)} "
    f"evento(s), {len(er)} erro(s))")

# 51. O CORACAO do pedido: uma imagem full apaga o que esta na pilha, com
#     fade de `fade_cartas` segundos ANTES dela comecar e DEPOIS que ela
#     acaba - nao importa quando a carta mesma ia sumir. 'A Torre' entra
#     aos 10s sem tempo declarado (ia ate o rabo do video, 190s); a imagem
#     full fica em tela dos 30s aos 38s.
ev, er, no = tc.parse_roteiro(
    "[0:10] A Torre; esquerda\n[0:30] mapa-astral.jpg; full; 8s", "", DUR,
    CC_TODO, NOMES, MIDIAS)
torres = [e for e in ev if e["carta"] == "A Torre"]
diz(len(torres) == 2, f"51: 'A Torre' vira DOIS pedacos - antes e depois "
    f"do buraco (veio {len(torres)})")
if len(torres) == 2:
    antes, depois = torres
    diz(aprox(antes["ini"], 10.0) and aprox(antes["fade_ini"], 28.0)
        and aprox(antes["fim"], 30.0),
        f"51: o pedaco de ANTES some exatamente onde a imagem full comeca "
        f"(esperado ini=10.0 fade_ini=28.0 fim=30.0, veio ini={antes['ini']} "
        f"fade_ini={antes['fade_ini']} fim={antes['fim']})")
    # a imagem full acaba (com o proprio pop) em 38.0+FADE_POP - e e ALI,
    # nao `fade_fora` segundos depois, que o pedaco de DEPOIS renasce: o
    # fade de entrada COMECA na hora, e leva `fade_fora` segundos para
    # completar.
    volta_em = 38.0 + FADE_POP
    diz(aprox(depois["ini"], volta_em, 1e-6) and aprox(depois["fade_in"], FADE_FORA),
        f"51: o pedaco de DEPOIS renasce assim que a imagem full acaba, "
        f"com fade de entrada de {FADE_FORA}s (esperado ini={volta_em:.3f}, "
        f"veio ini={depois['ini']} fade_in={depois['fade_in']})")
    diz(aprox(depois["fade_ini"], 190.0, 1e-6),
        f"51: e o fade de saida dela continua marcado para {190.0}s, o "
        f"mesmo tempo de antes do buraco (veio {depois['fade_ini']})")
    diz(aprox(depois["fim"], 190.0 + CC["fade"], 1e-6),
        f"51: e mantem a MESMA duracao de saida que tinha antes do corte "
        f"(esperado fim={190.0 + CC['fade']:.3f}, veio {depois['fim']})")

# 52. Tempo de 'full' que passa do rabo_final e aparado, com NOTA - mesma
#     regra da carta (ninguem encurta pedido em silencio).
ev, er, no = tc.parse_roteiro("[1:00] mapa-astral.jpg; full; 400s", "", DUR,
                               CC_TODO, NOMES, MIDIAS)
diz(len(ev) == 1 and aprox(ev[0]["fade_ini"], DUR - RABO, 1e-6),
    "52: tempo de 'full' longo demais e aparado no rabo do video")
diz(len(no) == 1 and no[0]["linha"] == 1,
    f"52: e a aparagem vira NOTA na linha (veio {len(no)} nota(s))")

# 53. Nome que nao bate em carta nem em midia - mensagem cobre as duas
#     listas, e nunca vira evento.
ev, er, no = tc.parse_roteiro("[0:30] nao-existe.png; 5s", "", DUR, CC_TODO,
                               NOMES, MIDIAS)
diz(not ev and len(er) == 1,
    f"53: nome fora dos dois catalogos vira erro (veio {len(er)} erro(s))")

# 54. `_agrupa_camadas` NAO cria camada para evento tipo 'full' - ela nao
#     tem lado nem profundidade, e quem desenha e outra parte do grafo.
ev, er, no = tc.parse_roteiro(
    "[0:10] A Torre; esquerda; 5s\n[0:30] mapa-astral.jpg; full; 8s", "",
    DUR, CC_TODO, NOMES, MIDIAS)
cam = tc._agrupa_camadas(ev)
diz(all(c["tipo"] != "full" for c in cam) and len(cam) == 1,
    f"54: a camada da pilha nao inclui a imagem full (veio {len(cam)} "
    f"camada(s), tipos {[c['tipo'] for c in cam]})")

# 55. `janelas_reenquadra` ignora a imagem full (lado != direita) - ela
#     cobre o quadro sozinha, nao pede o crop da Mariana centralizada.
jan = tc.janelas_reenquadra(ev, CC["junta_janela"])
diz(len(jan) == 0, f"55: imagem full nao abre janela de reenquadramento "
    f"(veio {len(jan)})")

# ---- a cor da imagem que entra no video -------------------------------
# A promessa de 09/09/2026: imagem sRGB posta num video HLG passa pela
# conversao de cor, e num video SDR nao passa por nada. Quebra CALADA nos
# dois sentidos - o arquivo sai, o ffmpeg nao reclama, e so o olho de
# quem conhece a imagem original percebe (foi assim que o defeito viveu
# ate o Rafael ver a carta "gritando" no "3 de espadas").
print()
print("== cor da imagem no overlay")

RESUMO = {"cx_p05": 0.4, "cx_p95": 0.6, "topo_min": 0.1, "alt_med": 0.3,
          "n": 100}
CAMADAS = [{"lado": "esquerda", "prof": 0, "carta": "A Torre", "tipo": "carta",
            "ocorrencias": [{"ini": 3.0, "fim": 18.0, "fade_in": 0.4,
                             "fade_ini": 17.6, "fade_dur": 0.4}]}]
CHEIOS = [{"ini": 5.0, "fim": 9.0, "fade_in": 0.05, "fade_ini": 8.95,
           "fade_dur": 0.05}]
CARTAS_1 = {"principal": "A Torre", "fade_entrada": 2.0,
            "secundarias": [{"ini": 3.0, "fim": 8.0, "carta": "O Sol"}]}


def ramos_img(g):
    """So os ramos que comecam numa ENTRADA DE IMAGEM ([1:v], [2:v]...).
    O ramo do video e [0:v], e e justamente o que nao pode mudar."""
    return [l for l in g.splitlines()
            if l.startswith("[") and l[1:2].isdigit() and not l.startswith("[0:")]


def grafos(hlg):
    """Os dois construtores, com as quatro entradas de imagem que
    existem: principal e secundaria (layouts 1/3), pilha e tela cheia."""
    from pathlib import Path
    return {
        "grafo_cartas": tc.grafo_cartas(
            IV_SEM_CORTE, CC_TODO, CARTAS_1, RESUMO, 3840, 2160,
            [("O Sol", Path("s.jpg"))], hlg=hlg),
        "grafo_cartas_pilhas": tc.grafo_cartas_pilhas(
            IV_SEM_CORTE, CC_TODO, CAMADAS, [(3.0, 18.0)], RESUMO, 3840, 2160,
            [Path("a.jpg")], cheios=CHEIOS, imgs_full=[Path("b.jpg")], hlg=hlg),
    }


# 56. Saida HLG: TODA entrada de imagem converte. Uma so que escape sai
#     gritando ao lado das outras, que e pior que todas erradas juntas.
for nome, g in grafos(True).items():
    r = ramos_img(g)
    n = sum(1 for l in r if "zscale=" in l)
    diz(r and n == len(r), f"56: {nome}: as {len(r)} imagens tem a cor "
        f"convertida (converteram {n})")

# 57. Saida SDR: NENHUMA converte - la a imagem ja esta no espaco de cor
#     do arquivo, e mexer estragaria o que estava certo.
for nome, g in grafos(False).items():
    r = ramos_img(g)
    diz(not any("zscale=" in l for l in r),
        f"57: {nome}: em SDR nenhuma das {len(r)} imagens e convertida")

# 58. O ramo do VIDEO nunca e tocado, nos dois modos: o pedido do Rafael
#     foi consertar a imagem SEM mexer na cor do video.
for nome in ("grafo_cartas", "grafo_cartas_pilhas"):
    com, sem = grafos(True)[nome], grafos(False)[nome]
    v_com = [l for l in com.splitlines() if l not in ramos_img(com)]
    v_sem = [l for l in sem.splitlines() if l not in ramos_img(sem)]
    diz(v_com == v_sem, f"58: {nome}: o grafo do video sai identico com e "
        f"sem a conversao ({len(v_com)} linhas)")

# 59. Sao dois botoes independentes, e a promessa e mexer em UM: converte
#     as PRIMARIAS e nao converte a CURVA. Ja quebrou nos dois sentidos -
#     sem conversao nenhuma a imagem sai gritante, com a curva convertida
#     junto sai acinzentada -, entao os dois lados sao cobrados aqui.
cor = tc._img_para_hlg(True)
diz("tin=iec61966-2-1" in cor,
    "59: a entrada e lida como sRGB de verdade, nao bt709")
diz("p=bt2020" in cor and "m=bt2020nc" in cor and "r=tv" in cor,
    "59: as primarias saem convertidas para BT.2020 (e a matriz e a faixa "
    "do arquivo, fechadas aqui)")
diz("t=iec61966-2-1" in cor and "arib-std-b67" not in cor,
    "59: e a CURVA nao e convertida - a saida continua na curva sRGB")
diz("npl" not in cor,
    "59: sem `npl`: ele so vale em HLG/PQ, e converter curva acabou")

# 59b. E o numero apagado nao volta pela porta de tras: `npl_grafico` era
#      o ponto livre da conversao de curva, e sem ela nao decide mais nada.
diz("npl_grafico" not in CC,
    "59b: `npl_grafico` nao voltou ao config.toml")

# 60. `saida_hlg` e quem decide as DUAS coisas - os tags do arquivo e a
#     conversao da imagem. Duas respostas diferentes seriam o arquivo
#     dizendo "isto e HLG" sobre uma imagem que nao e.
diz(tc.saida_hlg({"vcodec": "hevc"}) and not tc.saida_hlg({"vcodec": "h264"}),
    "60: saida_hlg responde hevc=HLG, h264=SDR")
diz(not tc.saida_hlg({}), "60: e sem vcodec nenhum nao inventa HLG")

print()
if falhas:
    for f in falhas:
        print("FALHA " + f)
    sys.exit(1)
print("tudo ok")
