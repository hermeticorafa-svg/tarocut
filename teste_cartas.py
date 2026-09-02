#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""teste_cartas.py - os casos de borda de `_agrupa_spans_cartas()` que nao
dependem de video nenhum.

So existe UM video de teste real (IMG_0301) e ele nao repete carta
secundaria - nao da pra confiar so nele para pegar erro de fusao/
persistencia. Este arquivo inventa `mencoes` com timestamps fabricados
e confere a regra pedida pelo Rafael em 02/09 direto na funcao, sem
passar por ingest/transcribe/render.

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

print()
if falhas:
    for f in falhas:
        print("FALHA " + f)
    sys.exit(1)
print("tudo ok")
