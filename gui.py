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
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work"
PY = ROOT / "venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = Path(sys.executable)

EXTS = {".mov", ".mp4", ".mkv", ".m4v", ".avi"}
# as saidas do proprio tarocut vivem na mesma pasta dos brutos: nao sao entrada
IGNORAR = ("_cortado", "_tarocut", "_previa")

PASTA_PADRAO = ROOT.parent


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
    """Devolve (etapa, duracao, corte, marcas) lendo work/<video>/."""
    d = WORK / Path(video).stem
    etapa, dur, corte, marcas = "-", "", "", ""
    if not d.is_dir():
        return etapa, dur, corte, marcas

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
    if (d / "edicao.json").exists():
        marcas += "✂ "               # editado no editor grafico
    if (d / "anotacoes.json").exists():
        marcas += "★ "               # passe de julgamento feito
    if (Path(video).parent / f"{Path(video).stem}_previa.m4a").exists():
        marcas += "♪ "               # ja existe previa em audio
    if (d / f"{Path(video).stem}_cortado.mov").exists():
        etapa = "renderizado"
        marcas += "▶ "
    c = d / "cuts.json"
    if c.exists():
        try:
            J = json.loads(c.read_text(encoding="utf-8"))
            corte = f"{100 * (1 - J['dur_final'] / J['dur_original']):.0f}%"
        except Exception:
            pass
    return etapa, dur, corte, marcas.strip()


def videos_de(pasta):
    try:
        vs = [p for p in Path(pasta).iterdir()
              if p.is_file() and p.suffix.lower() in EXTS
              and not any(x in p.stem.lower() for x in IGNORAR)]
    except OSError:
        return []
    return sorted(vs, key=lambda p: p.name.lower())


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

        # ---- lista
        quadro = ttk.Frame(self, padding=(10, 0))
        quadro.grid(row=1, column=0, sticky="nsew")
        quadro.columnconfigure(0, weight=1)
        quadro.rowconfigure(0, weight=1)
        cols = ("dur", "etapa", "corte", "marcas")
        self.lista = ttk.Treeview(quadro, columns=cols, show="tree headings",
                                  selectmode="browse", height=12)
        self.lista.heading("#0", text="Vídeo")
        self.lista.heading("dur", text="Duração")
        self.lista.heading("etapa", text="Etapa")
        self.lista.heading("corte", text="Corte")
        self.lista.heading("marcas", text="")
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
        self.lista.column("marcas", width=larg("✎ ★ ♪ ▶"), anchor="w",
                          stretch=False)
        self.lista.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(quadro, orient="vertical", command=self.lista.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.lista.configure(yscrollcommand=sb.set)
        self.lista.bind("<Double-1>", lambda e: self.abre_transcript())

        ttk.Label(quadro, foreground="#666",
                  text="✎ transcript editado     "
                       "✂ editado no editor     "
                       "★ anotações do Claude     "
                       "♪ prévia em áudio     "
                       "▶ renderizado"
                  ).grid(row=1, column=0, sticky="w", pady=(4, 0))

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
        bt("2. Editor", self.abre_editor)
        bt("Abrir transcript", self.abre_transcript)
        bt("Copiar /revisar", self.copia_revisar)
        sep()
        bt("3. Ouvir prévia",
           lambda: self.roda(["audio"], "prévia em áudio"))
        bt("Ver no mpv", lambda: self.roda(["previa"], "prévia em vídeo"))
        sep()
        bt("Simular", lambda: self.roda(["render", "--dry-run"],
                                        "simulação"))
        bt("4. Renderizar", lambda: self.roda(["render"], "render"))

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

    def recarrega(self):
        sel = self.selecionado()
        self.lista.delete(*self.lista.get_children())
        for v in videos_de(self.pasta.get()):
            etapa, dur, corte, marcas = estado(v)
            self.lista.insert("", "end", iid=str(v), text=v.name,
                              values=(dur, etapa, corte, marcas))
        if sel and self.lista.exists(str(sel)):
            self.lista.selection_set(str(sel))
            self.lista.see(str(sel))
        if not self.job:
            n = len(self.lista.get_children())
            self.status.set(f"{n} vídeo(s) em {self.pasta.get()}")

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
        if args[0] in ("audio", "previa", "render") \
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
        a aba, e o Job existe para comandos que comecam e terminam. Trava-
        lo aqui deixaria a janela inutil enquanto se edita."""
        v = self.exige_video()
        if not v:
            return
        if not (WORK / v.stem / "stream.json").exists():
            messagebox.showinfo("tarocut",
                                "Esse vídeo ainda não foi analisado. "
                                "Rode '1. Analisar' primeiro.")
            return
        pyw = PY.with_name("pythonw.exe")
        subprocess.Popen([str(pyw if pyw.exists() else PY),
                          str(ROOT / "tarocut.py"), "editor", str(v)],
                         cwd=str(ROOT),
                         creationflags=getattr(subprocess,
                                               "CREATE_NO_WINDOW", 0))
        self.status.set(f"editor aberto no navegador: {v.name}")

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
        for b in self.botoes:
            b.configure(state="disabled" if rodando else "normal")
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
