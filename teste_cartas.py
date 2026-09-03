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

CC = tc.cfg()["cartas"]
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

# 10. _agrupa_camadas_enumeracao: duas sessoes que colocam a MESMA carta
#     na MESMA posicao de pilha (esquerda/prof 0) viram 1 camada so, com
#     2 ocorrencias - nao 2 camadas (e o que faz o overlay caro por
#     camada nao dobrar de custo so porque a carta se repete).
gap_longo = PERSIST + 5.0
m = [menc("Rainha de Copas", 10.0, 10.3),
     menc("Rainha de Copas", 10.3 + gap_longo, 10.3 + gap_longo + 0.3)]
eventos, _ = tc._agrupa_cartas_enumeracao(m, IV_SEM_CORTE, CC, CE)
camadas = tc._agrupa_camadas_enumeracao(eventos)
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
camadas = tc._agrupa_camadas_enumeracao(eventos)
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

print()
if falhas:
    for f in falhas:
        print("FALHA " + f)
    sys.exit(1)
print("tudo ok")
