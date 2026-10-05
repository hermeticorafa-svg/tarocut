#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Janela do tarocut: a mesma coisa que a linha de comando, com botao.

Nao tem logica de edicao aqui dentro. A GUI lista os videos da pasta, le o
estado de cada um em work/<video>/ e chama `tarocut.py <comando> <video>`
num subprocesso, mostrando a saida ao vivo. Se o CLI mudar, isto continua
valendo; se a GUI quebrar, o CLI continua inteiro.
"""

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox

import pastas

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work"
PY = ROOT / "venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = Path(sys.executable)

EXTS = {".mov", ".mp4", ".mkv", ".m4v", ".avi"}
# O que o tarocut entrega mora em export/ desde 25/09/2026 (pastas.py). Isto
# fica para o que sobrou solto na pasta dos brutos de antes disso, ou saiu
# por um --out: saida nao e entrada. O `_final` faltava, e o
# "2 de espadas_final.mov" aparecia na lista como video a analisar.
IGNORAR = ("_cortado", "_tarocut", "_previa", "_final")

PASTA_PADRAO = ROOT.parent

# O que a lista mostra: a pasta escolhida (os brutos, como sempre foi) ou uma
# das pastas de export/ dentro dela. Os brutos sao o padrao e a janela sempre
# abre neles - e so ali que os botoes de etapa tem video em que agir.
BRUTOS = "brutos"
MODOS = ((BRUTOS, "Vídeos brutos"), (pastas.PREVIAS, "Prévias"),
         (pastas.FINAIS, "Finais"))
COLS_BRUTOS = ("dur", "etapa", "corte", "marcas")
COLS_SAIDAS = ("tipo", "quando", "tamanho")
LEGENDA_BRUTOS = ("✎ transcript editado     ★ anotações do Claude     "
                  "✂ ajustado no editor     ♪ prévia em áudio     "
                  "▶ renderizado")
LEGENDA_SAIDAS = ("duplo clique abre o arquivo     os botões de etapa "
                  "valem em Vídeos brutos")


# ----------------------------------------------------------------- estado

def hhmmss(t):
    t = int(t)
    return f"{t // 3600:d}:{t // 60 % 60:02d}:{t % 60:02d}"


def alinhado(p):
    """Le so a cauda do words.json: o arquivo tem megabytes e a chave
    'alinhado' e escrita depois da lista de palavras."""
    try:
        with open(p, "rb") as f:
            f.seek(max(0, p.stat().st_size - 400))
            return b'"alinhado": true' in f.read()
    except OSError:
        return False


def estado(video):
    """Devolve (etapa, duracao, corte, marcas, pronto) lendo work/<video>/."""
    d = WORK / Path(video).stem
    etapa, dur, corte, marcas = "-", "", "", ""
    if not d.is_dir():
        return etapa, dur, corte, marcas, False

    meta = d / "meta.json"
    if meta.exists():
        etapa = "audio extraido"
        try:
            dur = hhmmss(json.loads(meta.read_text(encoding="utf-8"))["dur"])
        except Exception:
            pass
    w = d / "words.json"
    if w.exists():
        etapa = "alinhado" if alinhado(w) else "transcrito"
    st = d / "stream.json"
    if st.exists():
        etapa = "analisado"
    md = d / "transcript.md"
    if md.exists():
        etapa = "pronto para revisar"
        try:
            if md.stat().st_mtime > st.stat().st_mtime + 2:
                marcas += "✎ "       # transcript editado depois do draft
        except OSError:
            pass
    if (d / "anotacoes.json").exists():
        marcas += "★ "               # passe de julgamento feito
    if (d / "montagem.json").exists():
        marcas += "✂ "               # ajustado no editor
    v = Path(video)
    if pastas.onde(v, v.stem, pastas.AUDIO).exists():
        marcas += "♪ "               # ja existe previa em audio
    # o `render` e o `exportar` entregam o mesmo video pronto, com e sem
    # carta, e os dois caem em export/finais
    if any(pastas.onde(v, v.stem, s).exists()
           for s in (pastas.FINAL, pastas.CORTADO)):
        etapa = "renderizado"
        marcas += "▶ "
    c = d / "cuts.json"
    if c.exists():
        try:
            J = json.loads(c.read_text(encoding="utf-8"))
            corte = f"{100 * (1 - J['dur_final'] / J['dur_original']):.0f}%"
        except Exception:
            pass
    # o checkbox "pronto para exportacao" do modo cartas do editor. Mesmo
    # arquivo, mesma chave dos dois lados - nao ha logica de edicao aqui,
    # so leitura do que o editor ja gravou.
    pronto = False
    rot = d / "cartas-roteiro.json"
    if rot.exists():
        try:
            pronto = bool(json.loads(rot.read_text(encoding="utf-8"))
                          .get("pronto"))
        except Exception:
            pass
    return etapa, dur, corte, marcas.strip(), pronto


def videos_de(pasta):
    try:
        vs = [p for p in Path(pasta).iterdir()
              if p.is_file() and p.suffix.lower() in EXTS
              and not any(x in p.stem.lower() for x in IGNORAR)]
    except OSError:
        return []
    return sorted(vs, key=lambda p: p.name.lower())


def saidas_de(pasta):
    """Os arquivos de uma pasta de export/, por nome - o .mov e o .srt do
    mesmo video ficam um embaixo do outro. Tudo o que estiver la, e nao so
    o que o tarocut sabe gerar: a lista mostra a pasta como ela esta."""
    try:
        fs = [p for p in Path(pasta).iterdir()
              if p.is_file() and p.name.lower() != "desktop.ini"]
    except OSError:
        return []
    return sorted(fs, key=lambda p: p.name.lower())


def tamanho(n):
    if n < 1e6:
        return f"{max(1, round(n / 1e3))} KB"
    if n < 1e9:
        return f"{n / 1e6:.0f} MB"
    return f"{n / 1e9:.1f} GB".replace(".", ",")


def descreve(p):
    """(tipo, quando, tamanho) de um arquivo entregue."""
    try:
        st = p.stat()
    except OSError:
        return "", "", ""
    ext = p.suffix.lower()
    tipo = ("vídeo" if ext in EXTS else "áudio" if ext in (".m4a", ".wav")
            else "legenda" if ext == ".srt" else ext.lstrip("."))
    return (tipo, time.strftime("%d/%m/%Y %H:%M", time.localtime(st.st_mtime)),
            tamanho(st.st_size))


# ------------------------------------------------------------ subprocesso

TEMPO = re.compile(r"time=(\d+):(\d\d):(\d\d(?:\.\d+)?)")   # ffmpeg -stats
SEGS = re.compile(r"\(t=(\d+(?:\.\d+)?)s\)")                # transcribe


class Job:
    """Roda um comando do tarocut e entrega a saida linha a linha.

    Le em blocos e quebra em \\r tambem: a barra de progresso do ffmpeg
    reescreve a mesma linha, e so em \\n ela apareceria de uma vez no fim."""

    def __init__(self, argv, fila, cwd):
        self.argv, self.fila, self.cwd = argv, fila, cwd
        self.proc = None
        self.cancelado = False

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            self.proc = subprocess.Popen(
                self.argv, cwd=str(self.cwd), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, creationflags=flags)
        except OSError as e:
            self.fila.put(("linha", f"[gui] nao consegui rodar: {e}", False))
            self.fila.put(("fim", 1, False))
            return

        buf = b""
        acabou = False
        while not acabou:
            bloco = self.proc.stdout.read1(4096)
            acabou = not bloco
            buf += bloco
            buf = self._quebra(buf, acabou)
        if buf:
            self.fila.put(("linha", buf.decode("utf-8", "replace"), False))
        self.proc.wait()
        self.fila.put(("fim", self.proc.returncode, self.cancelado))

    def _quebra(self, buf, acabou):
        """Corta o buffer em linhas. So o \\r sozinho e progresso: no pipe do
        Windows o fim de linha normal vem como \\r\\n, e trata-lo como
        progresso apagaria a linha anterior a cada linha nova."""
        while True:
            m = re.search(rb"[\r\n]", buf)
            if not m:
                return buf
            cr = m.group() == b"\r"
            fim = m.end()
            if cr and fim == len(buf) and not acabou:
                return buf          # pode ser \r\n partido entre dois blocos
            if cr and buf[fim:fim + 1] == b"\n":
                cr, fim = False, fim + 1
            linha, buf = buf[:m.start()], buf[fim:]
            self.fila.put(("linha", linha.decode("utf-8", "replace").rstrip(),
                           cr))

    def cancelar(self):
        if not self.proc or self.proc.poll() is not None:
            return
        self.cancelado = True
        # /T: o tarocut chama ffmpeg; matar so o python deixaria o filho vivo
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                       capture_output=True)


# ----------------------------------------------------------------- janela

class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("tarocut")
        # Nada de tamanho fixo em pixel: com a tela em 125% o Tk aumenta a
        # fonte e nao a janela, e o conteudo sai cortado. Medimos a fonte
        # (self.em) e, no fim, a janela pede o tamanho que o conteudo quer.
        self.em = tkfont.nametofont("TkDefaultFont").measure("0")

        self.pasta = tk.StringVar(value=str(PASTA_PADRAO))
        self.modo = tk.StringVar(value=BRUTOS)
        self.forcar = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="pronto")
        self.fila = queue.Queue()
        self.job = None
        self.dur_atual = None

        self._monta()
        self.recarrega()
        self._dimensiona()
        self.after(60, self._drena)
        self.protocol("WM_DELETE_WINDOW", self._fechar)

    def _dimensiona(self):
        """Tamanho vindo do conteudo, nao de um numero chutado.

        O Treeview mente sobre a largura que quer (pede 200 px fixos, nao a
        soma das colunas), entao somamos as colunas na mao e comparamos com
        a barra de botoes, que e a outra coisa larga da janela."""
        self.update_idletasks()
        em = self.em
        colunas = sum(int(self.lista.column(c, "width"))
                      for c in ("#0", "dur", "etapa", "corte", "marcas"))
        filhos = self.bar.winfo_children()
        botoes = sum(w.winfo_reqwidth() + 2 * em for w in filhos) + 4 * em
        larg = max(self.winfo_reqwidth(), colunas + 5 * em, botoes)
        larg = min(larg, self.winfo_screenwidth() - 6 * em)
        alt = min(self.winfo_reqheight() + 6 * em,
                  self.winfo_screenheight() - 10 * em)
        self.geometry(f"{int(larg)}x{int(alt)}")
        # nao deixa estreitar: e na largura que a barra de botoes quebra
        self.minsize(int(larg), int(alt * 0.6))

    # ------------------------------------------------------------ layout

    def _monta(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=3)
        self.rowconfigure(3, weight=2)

        topo = ttk.Frame(self, padding=(10, 10, 10, 4))
        topo.grid(row=0, column=0, sticky="ew")
        topo.columnconfigure(1, weight=1)
        ttk.Label(topo, text="Pasta:").grid(row=0, column=0, padx=(0, 6))
        ttk.Entry(topo, textvariable=self.pasta).grid(row=0, column=1,
                                                      sticky="ew")
        ttk.Button(topo, text="Escolher...", command=self.escolhe_pasta
                   ).grid(row=0, column=2, padx=6)
        ttk.Button(topo, text="Atualizar", command=self.recarrega
                   ).grid(row=0, column=3)
        ttk.Label(topo, text="Mostrar:").grid(row=1, column=0, padx=(0, 6),
                                              pady=(6, 0), sticky="w")
        modos = ttk.Frame(topo)
        modos.grid(row=1, column=1, columnspan=3, sticky="ew", pady=(6, 0))
        for valor, texto in MODOS:
            ttk.Radiobutton(modos, text=texto, value=valor,
                            variable=self.modo, command=self.troca_modo
                            ).pack(side="left", padx=(0, 12))
        ttk.Button(modos, text="Abrir pasta", command=self.abre_pasta
                   ).pack(side="right")

        # ---- lista
        quadro = ttk.Frame(self, padding=(10, 0))
        quadro.grid(row=1, column=0, sticky="nsew")
        quadro.columnconfigure(0, weight=1)
        quadro.rowconfigure(0, weight=1)
        # Um Treeview so para os dois jeitos de olhar: as colunas de um e do
        # outro existem juntas, e `displaycolumns` escolhe quais aparecem.
        self.lista = ttk.Treeview(quadro, columns=COLS_BRUTOS + COLS_SAIDAS,
                                  displaycolumns=COLS_BRUTOS,
                                  show="tree headings",
                                  selectmode="browse", height=12)
        self.lista.heading("#0", text="Vídeo")
        self.lista.heading("dur", text="Duração")
        self.lista.heading("etapa", text="Etapa")
        self.lista.heading("corte", text="Corte")
        self.lista.heading("marcas", text="")
        self.lista.heading("tipo", text="Tipo")
        self.lista.heading("quando", text="Gerado em")
        self.lista.heading("tamanho", text="Tamanho")
        # largura de cada coluna medida no texto mais largo que ela mostra,
        # que e o que sobrevive a mudanca de fonte e de escala da tela
        f = tkfont.nametofont("TkDefaultFont")

        def larg(amostra):
            return f.measure(amostra) + 3 * self.em

        self.lista.column("#0", width=larg("IMG_0246.MOV" + 8 * "0"),
                          stretch=True)
        self.lista.column("dur", width=larg("0:00:00"), anchor="center",
                          stretch=False)
        self.lista.column("etapa", width=larg("pronto para revisar"),
                          anchor="w", stretch=False)
        self.lista.column("corte", width=larg("100%"), anchor="center",
                          stretch=False)
        # as cinco marcas juntas: a amostra tinha quatro, e o `▶` saia
        # cortado justo no video que tem tudo feito
        self.lista.column("marcas", width=larg("✎ ★ ✂ ♪ ▶"), anchor="w",
                          stretch=False)
        self.lista.column("tipo", width=larg("legenda"), anchor="w",
                          stretch=False)
        self.lista.column("tamanho", width=larg("0000 MB"), anchor="e",
                          stretch=False)
        # O Treeview nao redistribui largura quando `displaycolumns` muda: as
        # colunas das saidas somando menos que as dos brutos deixavam uma
        # faixa vazia na direita. A data fica com a diferenca.
        def soma(cols):
            return sum(int(self.lista.column(c, "width")) for c in cols)
        self.lista.column("quando", anchor="center", stretch=False, width=max(
            larg("00/00/0000 00:00"),
            soma(COLS_BRUTOS) - soma(("tipo", "tamanho"))))
        # mesmo verde claro que o editor pinta no checkbox do modo cartas -
        # o mesmo alerta visual dos dois lados.
        self.lista.tag_configure("pronto", background="#d9f2d4")
        self.lista.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(quadro, orient="vertical", command=self.lista.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.lista.configure(yscrollcommand=sb.set)
        self.lista.bind("<Double-1>", lambda e: self.duplo_clique())

        self.legenda = ttk.Label(quadro, foreground="#666",
                                 text=LEGENDA_BRUTOS)
        self.legenda.grid(row=1, column=0, sticky="w", pady=(4, 0))

        # ---- botoes
        bar = self.bar = ttk.Frame(self, padding=(10, 8))
        bar.grid(row=2, column=0, sticky="ew")
        self.botoes = []

        def bt(texto, cmd):
            b = ttk.Button(bar, text=texto, command=cmd)
            b.pack(side="left", padx=(0, 6))
            self.botoes.append(b)
            return b

        def sep():
            ttk.Separator(bar, orient="vertical").pack(side="left", fill="y",
                                                       padx=8)

        bt("1. Analisar", self.analisar)
        bt("Copiar /revisar", self.copia_revisar)
        # `conferir` e a unica coisa aqui que le a geometria do corte sem
        # gastar minuto nenhum, e fora da janela ele nao existia - era o
        # passo que o README manda dar sempre depois do julgamento.
        bt("2. Conferir corte",
           lambda: self.roda(["conferir"], "conferência do corte"))
        bt("Abrir transcript", self.abre_transcript)
        sep()
        bt("3. Editor", self.abre_editor)
        bt("Ouvir prévia",
           lambda: self.roda(["audio"], "prévia em áudio"))
        bt("Ver no mpv", lambda: self.roda(["previa"], "prévia em vídeo"))
        sep()
        bt("Simular", lambda: self.roda(["render", "--dry-run"],
                                        "simulação"))
        bt("4. Renderizar", lambda: self.roda(["render"], "render"))
        # O outro fim de linha, o que passa pelas cartas: mesmo corte,
        # mesmo audio, mais o overlay do roteiro escrito no modo cartas
        # do editor e o .srt ao lado. Com roteiro vazio ele e o
        # "Renderizar" com legenda - por isso os dois ficam aqui, e nao
        # um no lugar do outro.
        bt("Prévia com cartas",
           lambda: self.roda(["exportar", "--previa"], "prévia com cartas"))
        bt("Exportar com cartas",
           lambda: self.roda(["exportar"], "export com cartas"))

        self.b_cancela = ttk.Button(bar, text="Cancelar",
                                    command=self.cancelar, state="disabled")
        self.b_cancela.pack(side="right")
        ttk.Checkbutton(bar, text="refazer do zero", variable=self.forcar
                        ).pack(side="right", padx=10)

        # ---- log
        low = ttk.Frame(self, padding=(10, 0, 10, 4))
        low.grid(row=3, column=0, sticky="nsew")
        low.columnconfigure(0, weight=1)
        low.rowconfigure(0, weight=1)
        self.log = tk.Text(low, height=12, wrap="none", background="#101216",
                           foreground="#d6d9de", insertbackground="#d6d9de",
                           font=("Consolas", 10), state="disabled",
                           borderwidth=0, padx=8, pady=6)
        self.log.grid(row=0, column=0, sticky="nsew")
        lsb = ttk.Scrollbar(low, orient="vertical", command=self.log.yview)
        lsb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=lsb.set)
        self.log.tag_configure("etapa", foreground="#7fd1b9")
        self.log.tag_configure("erro", foreground="#ff8f8f")
        self.log.tag_configure("gui", foreground="#8fb6ff")

        # ---- rodape
        pe = ttk.Frame(self, padding=(10, 0, 10, 10))
        pe.grid(row=4, column=0, sticky="ew")
        pe.columnconfigure(1, weight=1)
        self.barra = ttk.Progressbar(pe, length=200, mode="determinate")
        self.barra.grid(row=0, column=0, padx=(0, 10))
        ttk.Label(pe, textvariable=self.status).grid(row=0, column=1,
                                                     sticky="w")
        ttk.Button(pe, text="config.toml", command=self.abre_config
                   ).grid(row=0, column=2, padx=4)
        ttk.Button(pe, text="Pasta de trabalho", command=self.abre_work
                   ).grid(row=0, column=3)

    # ------------------------------------------------------------- lista

    def escolhe_pasta(self):
        p = filedialog.askdirectory(initialdir=self.pasta.get(),
                                    title="Pasta com os videos")
        if p:
            self.pasta.set(p)
            self.recarrega()

    def brutos(self):
        return self.modo.get() == BRUTOS

    def pasta_mostrada(self):
        """A pasta que a lista mostra agora: a escolhida, ou uma das de
        export/ dentro dela - pela mesma conta com que o tarocut grava."""
        if self.brutos():
            return Path(self.pasta.get())
        return pastas.pasta(self.pasta.get(), self.modo.get())

    def troca_modo(self):
        """So muda o que se olha. Nada no disco muda."""
        b = self.brutos()
        self.lista.configure(displaycolumns=COLS_BRUTOS if b else COLS_SAIDAS)
        self.lista.heading("#0", text="Vídeo" if b else "Arquivo")
        self.legenda.configure(text=LEGENDA_BRUTOS if b else LEGENDA_SAIDAS)
        self.recarrega()
        self._travar(bool(self.job))

    def recarrega(self):
        sel = self.selecionado()
        self.lista.delete(*self.lista.get_children())
        onde = self.pasta_mostrada()
        if self.brutos():
            for v in videos_de(onde):
                etapa, dur, corte, marcas, pronto = estado(v)
                self.lista.insert("", "end", iid=str(v), text=v.name,
                                  values=(dur, etapa, corte, marcas),
                                  tags=("pronto",) if pronto else ())
        else:
            for f in saidas_de(onde):
                self.lista.insert("", "end", iid=str(f), text=f.name,
                                  values=("", "", "", "") + descreve(f))
        if sel and self.lista.exists(str(sel)):
            self.lista.selection_set(str(sel))
            self.lista.see(str(sel))
        if not self.job:
            n = len(self.lista.get_children())
            if self.brutos():
                self.status.set(f"{n} vídeo(s) em {onde}")
            elif onde.is_dir():
                self.status.set(f"{n} arquivo(s) em {onde}")
            else:
                self.status.set(f"{onde} ainda não existe: nasce com o "
                                f"primeiro arquivo que sair para ela")

    def selecionado(self):
        s = self.lista.selection()
        return Path(s[0]) if s else None

    def exige_video(self):
        v = self.selecionado()
        if not v:
            messagebox.showinfo("tarocut", "Escolha um vídeo na lista.")
        return v

    # ------------------------------------------------------------- acoes

    def analisar(self):
        args = ["run"] + (["--force"] if self.forcar.get() else [])
        self.roda(args, "análise (transcrever, alinhar, propor cortes)")

    def roda(self, args, rotulo):
        v = self.exige_video()
        if not v:
            return
        if self.job:
            messagebox.showinfo("tarocut",
                                "Já tem coisa rodando. Espere ou cancele.")
            return
        d = WORK / v.stem
        if args[0] in ("audio", "previa", "render", "conferir") \
                and not (d / "stream.json").exists():
            messagebox.showinfo("tarocut",
                                "Esse vídeo ainda não foi analisado. "
                                "Rode '1. Analisar' primeiro.")
            return

        self.dur_atual = None
        try:
            self.dur_atual = json.loads(
                (d / "meta.json").read_text(encoding="utf-8"))["dur"]
        except Exception:
            pass

        argv = [str(PY), str(ROOT / "tarocut.py"), args[0], str(v)] + args[1:]
        self._escreve("\n$ tarocut.py " + " ".join(argv[2:]) + "\n", "gui")
        self.job = Job(argv, self.fila, ROOT)
        self.job.start()
        self._travar(True)
        self.status.set(f"{rotulo}: {v.name}")
        self.barra.configure(mode="indeterminate")
        self.barra.start(60)

    def cancelar(self):
        if self.job:
            self.job.cancelar()
            self.status.set("cancelando...")

    def abre_editor(self):
        """O editor nao passa pelo Job: ele fica no ar ate a pessoa fechar
        a aba, e o Job existe para comandos que comecam e terminam. Trava-lo
        aqui deixaria a janela inutil enquanto se edita.

        Mas a saida dele vem para o log daqui, e e por isso que ele nao roda
        escondido. O editor RECUSA abrir quando o transcript.md nao confere
        com a analise - e recusa de proposito. Sem esta bomba, essa recusa
        morria num console invisivel e o unico sintoma era o navegador nao
        abrir, sem uma linha dizendo por que."""
        v = self.exige_video()
        if not v:
            return
        if not (WORK / v.stem / "stream.json").exists():
            messagebox.showinfo("tarocut",
                                "Esse vídeo ainda não foi analisado. "
                                "Rode '1. Analisar' primeiro.")
            return
        argv = [str(PY), str(ROOT / "tarocut.py"), "editor", str(v)]
        self._escreve(f"\n$ tarocut.py editor {v.name}\n", "gui")
        try:
            proc = subprocess.Popen(
                argv, cwd=str(ROOT),
                env=dict(os.environ, PYTHONIOENCODING="utf-8",
                         PYTHONUNBUFFERED="1"),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True,
                encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            messagebox.showinfo("tarocut", f"não consegui abrir o editor: {e}")
            return

        def bomba():
            for ln in proc.stdout:
                ln = ln.rstrip()
                if ln:
                    self.fila.put(("linha", ln, False))
            rc = proc.wait()
            self.fila.put(("linha", f"[editor] fechado (código {rc})", False))

        threading.Thread(target=bomba, daemon=True).start()
        self.status.set(f"editor aberto no navegador: {v.name}")

    def duplo_clique(self):
        if self.brutos():
            self.abre_transcript()
        else:
            self.abre_arquivo()

    def abre_arquivo(self):
        """A previa ou o final, no programa que o Windows usa para ele."""
        f = self.selecionado()
        if not f:
            return
        try:
            os.startfile(str(f))
        except OSError as e:
            messagebox.showinfo("tarocut", f"não consegui abrir {f.name}: {e}")

    def abre_pasta(self):
        p = self.pasta_mostrada()
        if not p.is_dir():
            # nao cria: a pasta escolhida pode ser qualquer uma, e um clique
            # para OLHAR nao deixa export/ vazio espalhado por ai
            messagebox.showinfo("tarocut", f"{p} ainda não existe: ela nasce "
                                           f"com o primeiro arquivo que sair "
                                           f"para ela.")
            return
        os.startfile(str(p))

    def abre_transcript(self):
        v = self.exige_video()
        if not v:
            return
        p = WORK / v.stem / "transcript.md"
        if not p.exists():
            messagebox.showinfo("tarocut",
                                "Ainda não existe transcript.md para esse "
                                "vídeo. Rode '1. Analisar'.")
            return
        os.startfile(str(p))

    def copia_revisar(self):
        """O passe de julgamento roda numa janela limpa do Claude Code (o
        custo cresce com o tamanho da conversa), entao a GUI so entrega o
        comando pronto para colar."""
        v = self.exige_video()
        if not v:
            return
        self.clipboard_clear()
        self.clipboard_append(f"/revisar {v}")
        self.status.set("comando /revisar copiado — cole numa janela "
                        "nova do Claude Code")

    def abre_config(self):
        os.startfile(str(ROOT / "config.toml"))

    def abre_work(self):
        v = self.selecionado()
        d = WORK / v.stem if v and (WORK / v.stem).is_dir() else WORK
        d.mkdir(parents=True, exist_ok=True)
        os.startfile(str(d))

    # -------------------------------------------------------------- saida

    def _travar(self, rodando):
        # Fora dos brutos a lista mostra arquivos entregues, nao videos: um
        # botao de etapa ali rodaria sobre um .m4a, e o "ainda nao foi
        # analisado" que voltaria seria mentira sobre o video de verdade.
        livre = not rodando and self.brutos()
        for b in self.botoes:
            b.configure(state="normal" if livre else "disabled")
        self.b_cancela.configure(state="normal" if rodando else "disabled")

    def _escreve(self, texto, tag=None, sobrescreve=False):
        self.log.configure(state="normal")
        # linha vinda de \r: substitui a anterior em vez de empilhar
        if sobrescreve and int(self.log.index("end-1c").split(".")[0]) > 1:
            self.log.delete("end-1c-1l linestart", "end-1c")
        self.log.insert("end", texto, tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _progresso(self, linha):
        if not self.dur_atual:
            return
        t = None
        m = TEMPO.search(linha)
        if m:
            t = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
        else:
            m = SEGS.search(linha)
            if m:
                t = float(m[1])
        if t is None:
            return
        if self.barra["mode"] != "determinate":
            self.barra.stop()
            self.barra.configure(mode="determinate", maximum=100)
        self.barra["value"] = max(0, min(100, 100 * t / self.dur_atual))

    def _drena(self):
        try:
            while True:
                tipo, a, b = self.fila.get_nowait()
                if tipo == "linha":
                    tag = ("erro" if re.search(
                               r"falhou|erro|Error|Traceback", a) else
                           "etapa" if a.startswith("[") else None)
                    self._escreve(a + "\n", tag, sobrescreve=b)
                    self._progresso(a)
                else:
                    self._fim(a, b)
        except queue.Empty:
            pass
        self.after(60, self._drena)

    def _fim(self, rc, cancelado):
        self.job = None
        self._travar(False)
        self.barra.stop()
        self.barra.configure(mode="determinate")
        self.barra["value"] = 0 if rc else 100
        self.recarrega()          # antes do status: recarrega escreve nele
        if cancelado:
            self.status.set("cancelado")
            self._escreve("[gui] cancelado\n", "gui")
        elif rc:
            self.status.set(f"terminou com erro (código {rc}) — veja o log")
        else:
            self.status.set("pronto")

    def _fechar(self):
        if self.job and not messagebox.askokcancel(
                "tarocut", "Tem coisa rodando. Fechar mesmo assim?"):
            return
        if self.job:
            self.job.cancelar()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
