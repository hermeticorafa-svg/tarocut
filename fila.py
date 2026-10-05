#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A fila do /preparar: varios videos, UM de cada vez, cada um num agente
que comeca do zero. Quem usa e o `.claude/commands/fila.md`; isto aqui e a
parte que nao pode depender de memoria de conversa.

Existe por dois motivos, e os dois sao medicao:

- o /preparar pede janela limpa: o passe de julgamento e a parte cara de
  contexto, e empilhar o segundo video em cima do primeiro joga fora a
  economia que o `/revisar` foi desenhado para ter;
- esta maquina so aguenta um de cada vez. MEDIDO (08/09/2026) no "naipe de
  ouros", 27 min: o `transcribe` sozinho chega a 2,98 GB de commit, numa
  maquina de 7,9 GB em que a RAM livre caiu a 215 MB. Dois nao cabem.

Quem faz o que, e e a divisao que impede dois de rodarem juntos:

- `work/fila.json` so e escrito por este arquivo, e este arquivo so e
  chamado pela conversa onde se digitou `/fila`. O agente de cada video
  nunca toca na fila: recebe o caminho pronto no pedido.
- `proximo` recusa abrir um video enquanto outro estiver em `preparando`.
  Quem tira de `preparando` e o `fecha`, depois que o agente voltou.
- `fecha` nao acredita no relatorio do agente. Confere no disco se o que o
  /preparar entrega foi escrito DEPOIS que a rodada comecou, e conta no
  `conferir.txt` o que o passo 3 do /preparar exige zerado. Agente que diz
  "pronto" tendo parado no meio e o defeito de sempre desta casa por outra
  porta: o que nao aconteceu passando por feito, sem ninguem perguntar.

Comandos: poe, ver, proximo, fecha, devolve, tira, faltam.
"""
import argparse, difflib, re, subprocess, sys
from datetime import datetime
from pathlib import Path

import pastas
import tarocut as tc

FILA = tc.WORK / "fila.json"
# Onde mora o bruto de um nome sem caminho: a mesma pasta do passo 0 do
# /preparar, e a que a janela abre por padrao (gui.PASTA_PADRAO).
BRUTOS = tc.ROOT.parent
HORA = "%Y-%m-%d %H:%M:%S"
VIVOS = ("pendente", "preparando")
ROTULO = {"pendente": "na fila", "preparando": "preparando",
          "pronto": "pronto", "atencao": "atencao", "falhou": "falhou"}

# O que o /preparar entrega, NA ORDEM em que entrega: o primeiro que falta
# diz em que etapa a rodada parou. O transcript.md conta como novo mesmo num
# video que ja tinha passado pelo `lote`: o `draft` nao tem cache e o
# reescreve toda vez.
ENTREGAS = (
    ("passada mecanica", lambda d, src: d / "transcript.md"),
    ("passe de julgamento", lambda d, src: d / "entendimento.md"),
    ("conferir", lambda d, src: d / "conferir.txt"),
    ("audio", lambda d, src: pastas.onde(src, src.stem, pastas.AUDIO)),
)
# O que o passo 3 do /preparar exige zerado. O resto do conferir e achado
# para triar, nao pergunta sem dono.
EM_ABERTO = ("ANOTACAO NAO OBEDECIDA", "PENDENCIA SEM RESPOSTA",
             "PENDENCIA NAO OBEDECIDA")


def carrega():
    return tc.jload(FILA)["itens"] if FILA.exists() else []


def grava(itens):
    tc.jdump({"itens": itens}, FILA)


def agora():
    return datetime.now().strftime(HORA)


def nome(it):
    return Path(it["video"]).stem


def chave(p):
    return str(p).casefold()


def hhmm(s):
    return s[11:16] if s else "?"


def mmss(dur):
    return f"{int(dur) // 60}min{int(dur) % 60:02d}" if dur else "?"


def duracao(src):
    """Do meta.json, se o ingest ja passou; senao do ffprobe, que so le o
    cabecalho. Serve para quem decide a ordem, e para o agente saber se o
    video passa dos 15 min em que o `run` estoura a memoria."""
    try:
        return float(tc.jload(tc.WORK / src.stem / "meta.json")["dur"])
    except Exception:
        pass
    try:
        r = subprocess.run([tc.FFPROBE, "-v", "error", "-show_entries",
                            "format=duration", "-of", "csv=p=0", str(src)],
                           capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip())
    except Exception:
        return None


def julgado(src):
    """O arquivo que prova que o passe de julgamento ja passou por este
    video, ou None. `entendimento.md` e o primeiro que o /revisar escreve; o
    `anotacoes.json` cobre os de calibracao, julgados antes dele existir.

    Preparar de novo nao estraga nada - o /revisar so acrescenta -, mas e
    uma segunda camada de julgamento por cima da primeira. Decide quem
    pediu, com --refazer; a fila nao decide isso sozinha."""
    d = tc.WORK / src.stem
    for f in ("entendimento.md", "anotacoes.json"):
        if (d / f).exists():
            return d / f
    return None


def brutos():
    return sorted(p for p in BRUTOS.iterdir()
                  if p.is_file() and p.suffix.lower() in tc.EXTS)


def acha(texto):
    """(caminho, parecidos). Caminho com barra ou letra de unidade vale como
    veio, a regra do passo 0 do /preparar; nome solto procura na pasta dos
    brutos, com ou sem extensao. Os parecidos so existem quando nada casa:
    o bruto do "cavaleiro de copas" se chama "cavalero de copas.MOV"."""
    s = texto.strip().strip("\"'").strip()
    if re.search(r"[\\/]", s) or re.match(r"[A-Za-z]:", s):
        p = Path(s)
        return (p.resolve(), []) if p.is_file() else (None, [])
    bs = brutos()
    alvo = s.casefold()
    for p in bs:
        if alvo in (p.name.casefold(), p.stem.casefold()):
            return p, []
    por_nome = {p.stem.casefold(): p.name for p in bs}
    perto = difflib.get_close_matches(alvo, list(por_nome), n=3, cutoff=0.6)
    return None, [por_nome[k] for k in perto]


def em_aberto(p):
    n = {}
    for linha in p.read_text(encoding="utf-8").splitlines():
        if linha.startswith("#"):       # o cabecalho explica cada rotulo
            continue
        for r in EM_ABERTO:
            if r in linha:
                n[r] = n.get(r, 0) + 1
    return n


def confere(it):
    """(estado, nota) de um video que saiu de `preparando`, medido no disco."""
    src = Path(it["video"])
    d = tc.WORK / src.stem
    desde = datetime.strptime(it["comecou"], HORA).timestamp()
    for etapa, onde in ENTREGAS:
        p = onde(d, src)
        if not p.exists() or p.stat().st_mtime < desde:
            return "falhou", f"parou na etapa: {etapa} (sem {p.name} novo)"
    n = em_aberto(d / "conferir.txt")
    if n:
        return "atencao", ", ".join(f"{k} {r}" for r, k in n.items())
    return "pronto", "conferir sem nada em aberto"


def mostra(itens):
    if not itens:
        print("fila vazia")
        return
    for it in itens:
        if it["estado"] == "preparando":
            print(f"  preparando  {nome(it):36s} desde {hhmm(it['comecou'])}")
    pend = [it for it in itens if it["estado"] == "pendente"]
    for i, it in enumerate(pend, 1):
        print(f"  na fila {i:<3d} {nome(it):36s} {mmss(it.get('dur'))}")
    fim = sorted((it for it in itens if it["estado"] not in VIVOS),
                 key=lambda it: it.get("terminou", ""))
    for it in reversed(fim[-10:]):
        print(f"  {ROTULO[it['estado']]:11s} {nome(it):36s} "
              f"{hhmm(it.get('comecou'))}-{hhmm(it.get('terminou'))}  "
              f"{it.get('nota', '')}")
    if len(fim) > 10:
        print(f"  (mais {len(fim) - 10} terminados antes desses)")


# ---------------------------------------------------------------- comandos

def cmd_poe(a):
    itens = carrega()
    vivos = {chave(it["video"]) for it in itens if it["estado"] in VIVOS}
    novos = 0
    for texto in a.videos:
        src, perto = acha(texto)
        if src is None:
            dica = f" (parecidos: {', '.join(perto)})" if perto else ""
            print(f"  nao achei: {texto}{dica}")
            continue
        if chave(src) in vivos:
            print(f"  ja esta na fila: {src.stem}")
            continue
        prova = julgado(src)
        if prova and not a.refazer:
            quando = datetime.fromtimestamp(prova.stat().st_mtime)
            print(f"  pulei: {src.stem} - ja tem passe de julgamento "
                  f"({prova.name} de {quando:%d/%m}); para preparar de "
                  f"novo, peca com --refazer")
            continue
        it = {"video": str(src), "estado": "pendente", "entrou": agora(),
              "dur": duracao(src)}
        itens.append(it)
        vivos.add(chave(src))
        novos += 1
        print(f"  entrou: {src.stem} ({mmss(it['dur'])})")
    if novos:
        grava(itens)
    print()
    mostra(itens)


def cmd_ver(a):
    mostra(carrega())


def cmd_proximo(a):
    """Marca o primeiro da fila como `preparando` e diz qual e. Recusa se
    ja houver um: o mesmo erro de memoria que a fila existe para evitar."""
    itens = carrega()
    for it in itens:
        if it["estado"] == "preparando":
            sys.exit(f"[fila] {nome(it)} esta em 'preparando' desde "
                     f"{hhmm(it['comecou'])}. Feche (fecha) antes de abrir "
                     f"outro: dois ao mesmo tempo nao cabem na memoria.")
    mudou = False
    for it in itens:
        if it["estado"] != "pendente":
            continue
        if not Path(it["video"]).is_file():
            it.update(estado="falhou", comecou=agora(), terminou=agora(),
                      nota="o arquivo nao esta mais la")
            print(f"  pulei: {nome(it)} - o arquivo nao esta mais la")
            mudou = True
            continue
        it.update(estado="preparando", comecou=agora())
        grava(itens)
        print(f"video: {it['video']}")
        print(f"nome: {nome(it)}")
        print(f"duracao: {mmss(it.get('dur'))}")
        return
    if mudou:
        grava(itens)
    print("VAZIA")


def cmd_fecha(a):
    itens = carrega()
    for it in itens:
        if it["estado"] == "preparando":
            estado, nota = confere(it)
            it.update(estado=estado, terminou=agora(), nota=nota)
            grava(itens)
            print(f"{ROTULO[estado]}: {nome(it)} - {nota}")
            return
    print("nada em 'preparando'")


def cmd_devolve(a):
    """O agente nem chegou a comecar: o video volta ao lugar dele na fila,
    em vez de virar 'falhou' por arquivo que ninguem tentou escrever."""
    itens = carrega()
    for it in itens:
        if it["estado"] == "preparando":
            it["estado"] = "pendente"
            it.pop("comecou", None)
            grava(itens)
            print(f"  voltou para a fila: {nome(it)}")
            return
    print("nada em 'preparando'")


def cmd_tira(a):
    """Tira da fila o que ainda nao comecou. O que esta em `preparando` nao
    sai por aqui: parar um /preparar no meio deixa o julgamento pela
    metade, e isso e decisao de quem pediu, na conversa."""
    itens = carrega()
    if a.todos:
        alvos = None
    else:
        alvos = {Path(t.strip().strip("\"'")).stem.casefold()
                 for t in a.videos}
    fica = []
    for it in itens:
        if it["estado"] == "pendente" and (
                alvos is None or nome(it).casefold() in alvos):
            print(f"  saiu da fila: {nome(it)}")
            continue
        if it["estado"] == "preparando" and alvos and \
                nome(it).casefold() in alvos:
            print(f"  {nome(it)} ja esta preparando - nao saiu")
        fica.append(it)
    if len(fica) != len(itens):
        grava(fica)
    print()
    mostra(fica)


def cmd_faltam(a):
    vivos = {chave(it["video"]) for it in carrega() if it["estado"] in VIVOS}
    fs = [p for p in brutos() if not julgado(p) and chave(p) not in vivos]
    if not fs:
        print("nenhum bruto sem passe de julgamento fora da fila")
        return
    total = 0
    for p in fs:
        dur = duracao(p)
        total += dur or 0
        print(f"  {p.stem:40s} {mmss(dur)}")
    print(f"  {len(fs)} videos, {mmss(total)} de bruto")


def main():
    ap = argparse.ArgumentParser(prog="fila.py",
                                 description="a fila do /preparar")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("poe", help="poe videos no fim da fila")
    p.add_argument("videos", nargs="+")
    p.add_argument("--refazer", action="store_true",
                   help="aceita video que ja tem passe de julgamento")
    sub.add_parser("ver", help="mostra a fila")
    sub.add_parser("proximo", help="marca o primeiro como preparando")
    sub.add_parser("fecha", help="confere no disco o que estava preparando")
    sub.add_parser("devolve", help="o que estava preparando volta pra fila")
    p = sub.add_parser("tira", help="tira da fila o que nao comecou")
    p.add_argument("videos", nargs="*")
    p.add_argument("--todos", action="store_true")
    sub.add_parser("faltam", help="brutos sem passe de julgamento")
    a = ap.parse_args()
    if a.cmd == "tira" and not a.videos and not a.todos:
        ap.error("tira: diga quais videos, ou --todos")
    {"poe": cmd_poe, "ver": cmd_ver, "proximo": cmd_proximo,
     "fecha": cmd_fecha, "devolve": cmd_devolve, "tira": cmd_tira,
     "faltam": cmd_faltam}[a.cmd](a)


if __name__ == "__main__":
    main()
