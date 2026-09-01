#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""editor.py - editor de corte do tarocut, no navegador.

Por que navegador e nao Tk: o que faz este editor valer a pena e ouvir o
corte enquanto se corta, sem renderizar nada. Isso pede um motor de audio
que toque uma lista de trechos com emenda precisa e sem lacuna - a Web Audio
API faz isso de graca, agendando cada trecho num relogio de amostra. Em Tk
seria preciso escrever esse motor do zero.

O PONTO DELICADO deste arquivo e um so: o que se ouve tem que ser
exatamente o que vai sair no render. Por isso o navegador nunca calcula
corte nenhum. A cada clique ele manda o estado para ca, e a resposta sai da
mesma `tarocut.intervalos()` que o `render` usa - com o mesmo snap, a mesma
fusao de ilhas, o mesmo minimo. O navegador so desenha e toca a lista que
recebeu. Uma reimplementacao em JS seria mais rapida e mentiria em algum
canto, mais cedo ou mais tarde.

O que sai daqui e `work/<video>/edicao.json`, lido pelo `_base` do tarocut.
Depois de salvar, `render` produz exatamente o corte ouvido.
"""

import json
import subprocess
import sys
import threading
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import tarocut as tc

ROOT = Path(__file__).resolve().parent
PAGINA = ROOT / "editor.html"


# ------------------------------------------------------------------ sessao

class Sessao:
    """Um video carregado. Vive enquanto o servidor vive.

    O fluxo analisado fica aqui, em memoria, e e reaproveitado a cada
    recalculo: reler stream.json e refazer o envelope do wav a cada clique
    poria meio segundo entre o clique e o som."""

    def __init__(self, video):
        st = tc.WORK / Path(video).stem / "stream.json"
        if not st.exists():
            sys.exit(f"[editor] {Path(video).stem} ainda nao foi analisado. "
                     f"Rode 'tarocut.py run' antes.")
        (self.C, self.d, self.stream, self.dur, self.db, self.hop,
         self.anot, self.ed, self.selo) = tc._base(str(video), "editor",
                                                   aplicar_ed=False)
        self.meta = tc.jload(self.d / "meta.json")
        self.src = Path(self.meta["src"])
        self.video = self.src if self.src.exists() else Path(video)
        self.nome = Path(video).stem
        self.wav = self.d / "audio.wav"
        if not self.wav.exists():
            sys.exit(f"[editor] falta {self.wav}. Rode 'tarocut.py run' antes.")
        import wave
        with wave.open(str(self.wav), "rb") as w:
            self.sr = w.getframerate()
        # estado depois do transcript.md e ANTES do edicao.json: e contra
        # esta linha de base que o editor mede o que a pessoa mudou. Sem
        # ela, salvar gravaria como "decisao humana" tudo o que a analise
        # ja tinha proposto sozinha.
        self.base = [bool(t["cut"]) for t in self.stream]
        self.lock = threading.Lock()

    # ---------------------------------------------------------- documento

    def doc(self):
        # o `cut` sai de self.base, e nao de t["cut"]: o recalculo escreve
        # em self.stream a cada clique, e ler dali entregaria ao navegador
        # o ultimo estado experimentado como se fosse a proposta original -
        # tudo o que a pessoa tocou apareceria de novo como "decisao sua".
        toks = []
        for i, t in enumerate(self.stream):
            toks.append({"i": i, "k": t["k"], "t": t["t"],
                         "s": round(t["s"], 3), "e": round(t["e"], 3),
                         "cut": self.base[i], "why": t.get("why", ""),
                         "prot": bool(t.get("prot"))})
        # rele do disco: apagar ou editar o edicao.json a mao e recarregar
        # a pagina tem que refletir o arquivo, nao o que o servidor lembra
        self.ed = tc.carrega_edicao(self.d)
        ed = self.ed or {}
        # edicao de outra analise: os tempos continuam valendo (sao
        # absolutos), os indices de token nao. Descarta so o que caducou,
        # e avisa - salvar daqui regrava com o selo novo.
        aviso = ""
        if ed and ed.get("selo") and ed["selo"] != self.selo:
            aviso = ("o edicao.json e de uma analise anterior: os ajustes "
                     "de milissegundo foram mantidos, as decisoes de "
                     "palavra foram descartadas")
            ed = dict(ed, tokens={})
        return {
            "aviso": aviso,
            "nome": self.nome,
            "arquivo": self.src.name,
            "dur": self.dur,
            "sr": self.sr,
            "selo": self.selo,
            "fade": self.C["audio"]["fade"],
            "pausa_alvo": self.C["silencio"]["pausa_alvo"],
            "tokens": toks,
            "base": self.base,
            "blocos": tc.blocos(self.stream),
            "anot": self.anot,
            "edicao": {"tokens": ed.get("tokens") or {},
                       "cortes": ed.get("cortes") or [],
                       "restauros": ed.get("restauros") or []},
        }

    # ---------------------------------------------------------- recalculo

    def intervalos(self, cut, cortes, restauros):
        """A unica fonte de verdade do corte, para a tela e para o render."""
        with self.lock:
            for t, c in zip(self.stream, cut):
                t["cut"] = (c == "1")
            manual = {"cortes": tc._spans(cortes),
                      "restauros": tc._spans(restauros)}
            iv = tc.intervalos(self.stream, self.C, self.dur, self.db,
                               self.hop, self.anot, manual)
        mantido = sum(b - a for a, b in iv)
        return {"iv": iv, "mantido": mantido, "dur": self.dur,
                "emendas": max(0, len(iv) - 1)}

    # ------------------------------------------------------------- salvar

    def salva(self, cut, cortes, restauros):
        """Grava so a DIFERENCA em relacao a proposta automatica.

        Guardar os 899 tokens inteiros funcionaria, mas apagaria a
        distincao entre 'a analise decidiu' e 'eu decidi' - e essa
        distincao e o que deixa reanalisar o video sem perder o julgamento
        humano."""
        tokens = {}
        for i, (c, b) in enumerate(zip(cut, self.base)):
            atual = (c == "1")
            if atual != b:
                tokens[str(i)] = "cortar" if atual else "manter"
        ed = {"versao": 1, "video": self.src.name, "selo": self.selo,
              "tokens": tokens,
              "cortes": tc._spans(cortes),
              "restauros": tc._spans(restauros)}
        # Um edicao.json de OUTRA analise perde as decisoes de palavra ja
        # na carga (o `doc` avisa e descarta). Se o primeiro clique gravar
        # por cima, elas somem do disco tambem, e ninguem viu o que se
        # perdeu. Copia antes. Aconteceu de verdade: duas decisoes de
        # julgamento evaporaram no primeiro ajuste depois de reanalisar.
        p = self.d / "edicao.json"
        if p.exists():
            try:
                velho = tc.jload(p)
            except Exception:
                velho = {}
            if (velho.get("tokens") and velho.get("selo")
                    and velho["selo"] != self.selo):
                (self.d / "edicao-anterior.json").write_bytes(p.read_bytes())
                print(f"[editor] o edicao.json era de outra analise; "
                      f"copia em {self.d / 'edicao-anterior.json'}")
        tc.jdump(ed, self.d / "edicao.json")
        self.ed = ed
        return {"ok": True, "arquivo": str(self.d / "edicao.json"),
                "tokens": len(tokens), "cortes": len(ed["cortes"]),
                "restauros": len(ed["restauros"])}


# ------------------------------------------------------------- subprocesso

class Tarefa:
    """Roda `tarocut.py <cmd>` e guarda a saida para a pagina ler.

    Existe para fechar o ciclo sem sair do editor: salvou, renderizou,
    ouviu. O log vai para um deque porque o ffmpeg escreve uma linha de
    progresso por segundo e ninguem quer o historico inteiro."""

    def __init__(self):
        self.linhas = deque(maxlen=400)
        self.proc = None
        self.rc = None
        self.rotulo = ""

    @property
    def rodando(self):
        return self.proc is not None and self.proc.poll() is None

    def roda(self, argv, rotulo):
        if self.rodando:
            return False
        self.linhas.clear()
        self.rc = None
        self.rotulo = rotulo
        self.linhas.append("$ " + " ".join(argv[1:]))
        threading.Thread(target=self._run, args=(argv,), daemon=True).start()
        return True

    def _run(self, argv):
        try:
            self.proc = subprocess.Popen(
                argv, cwd=str(ROOT), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
        except OSError as e:
            self.linhas.append(f"[editor] nao consegui rodar: {e}")
            self.rc = 1
            return
        for ln in self.proc.stdout:
            ln = ln.rstrip("\r\n")
            if ln:
                self.linhas.append(ln)
        self.proc.wait()
        self.rc = self.proc.returncode


# ---------------------------------------------------------------- servidor

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "tarocut-editor"

    # o log de acesso do http.server polui o console com uma linha por
    # requisicao, e o editor faz uma por clique
    def log_message(self, fmt, *args):
        pass

    # ------------------------------------------------------------ helpers

    def _envia(self, corpo, tipo="application/json; charset=utf-8",
               codigo=200):
        if isinstance(corpo, (dict, list)):
            corpo = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
        elif isinstance(corpo, str):
            corpo = corpo.encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corpo)

    def _corpo(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    # --------------------------------------------------------------- GET

    def do_GET(self):
        rota = urlparse(self.path).path
        S, T = self.server.sessao, self.server.tarefa
        try:
            if rota in ("/", "/index.html"):
                return self._envia(PAGINA.read_text(encoding="utf-8"),
                                   "text/html; charset=utf-8")
            if rota == "/api/doc":
                return self._envia(S.doc())
            if rota == "/api/log":
                return self._envia({"linhas": list(T.linhas),
                                    "rodando": T.rodando, "rc": T.rc,
                                    "rotulo": T.rotulo})
            if rota == "/audio.wav":
                dados = S.wav.read_bytes()
                return self._envia(dados, "audio/wav")
            self._envia({"erro": "nao existe"}, codigo=404)
        except BrokenPipeError:
            pass
        except Exception as e:
            self._envia({"erro": repr(e)}, codigo=500)

    # -------------------------------------------------------------- POST

    def do_POST(self):
        rota = urlparse(self.path).path
        S, T = self.server.sessao, self.server.tarefa
        try:
            b = self._corpo()
            if rota == "/api/intervalos":
                return self._envia(S.intervalos(b["cut"], b["cortes"],
                                                b["restauros"]))
            if rota == "/api/salvar":
                return self._envia(S.salva(b["cut"], b["cortes"],
                                           b["restauros"]))
            if rota == "/api/rodar":
                cmd = b.get("cmd")
                if cmd not in ("render", "audio", "previa", "dry"):
                    return self._envia({"erro": "comando desconhecido"},
                                       codigo=400)
                argv = [sys.executable, str(ROOT / "tarocut.py")]
                argv += (["render", str(S.video), "--dry-run"] if cmd == "dry"
                         else [cmd, str(S.video)])
                ok = T.roda(argv, cmd)
                return self._envia({"ok": ok})
            if rota == "/api/cancelar":
                if T.rodando:
                    subprocess.run(["taskkill", "/F", "/T", "/PID",
                                    str(T.proc.pid)], capture_output=True)
                return self._envia({"ok": True})
            self._envia({"erro": "nao existe"}, codigo=404)
        except BrokenPipeError:
            pass
        except Exception as e:
            self._envia({"erro": repr(e)}, codigo=500)


def serve(video, porta=8731, abrir=True):
    sessao = Sessao(video)
    srv = ThreadingHTTPServer(("127.0.0.1", porta), Handler)
    srv.sessao, srv.tarefa = sessao, Tarefa()
    url = f"http://127.0.0.1:{porta}/"
    print(f"[editor] {sessao.nome}: {len(sessao.stream)} tokens, "
          f"{tc.hhmmss(sessao.dur)}")
    print(f"[editor] {url}   (Ctrl+C aqui fecha o editor)")
    if abrir:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[editor] fechado")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("uso: editor.py <video> [porta]")
    serve(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 8731)
