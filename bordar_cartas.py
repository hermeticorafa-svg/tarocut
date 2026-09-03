#!/usr/bin/env python3
"""Gera baralho completo/tarodeyala-borda/: copia de tarodeyala/ com 1px de
borda preta em cada carta, para o overlay de `cartas` (tarocut.py) - ver
CLAUDE.md, secao "Adicionar cartas".

Por que existe
--------------
`_catalogo_img()` (tarocut.py) le desta pasta pro OVERLAY, nao de
`baralho completo/tarodeyala/`: aquela e a fonte que `baralho.catalogo()`
usa pra casar mencao na fala (nome de arquivo -> chave falada) e nao pode
mudar por um motivo cosmetico. A borda so ajuda a carta se destacar de
fundo de video claro; por isso vive numa copia, nunca no original.

PIL nao esta no venv (confirmado - nao adicionar so pra isto). ffmpeg ja e
dependencia do projeto; `pad=iw+2:ih+2:1:1:black` poe 1px preto de cada
lado sem reamostrar a imagem.

Rode de novo sempre que baralho completo/tarodeyala/ mudar (arquivo novo,
substituido etc.) - so regenera o que esta desatualizado:

    venv\\Scripts\\python.exe bordar_cartas.py
"""
import os, shutil, subprocess, sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import baralho

DESTINO = baralho.BARALHO.parent / "tarodeyala-borda"


def _find_ffmpeg():
    # Mesma busca de tarocut._find_bin, duplicada: este script fica
    # standalone (nao importa tarocut.py) pela mesma razao de baralho.py -
    # roda sem carregar o resto do pipeline.
    p = shutil.which("ffmpeg")
    if p:
        return p
    base = Path(os.environ.get("LOCALAPPDATA", ""))
    for cand in base.glob("Microsoft/WinGet/Packages/Gyan.FFmpeg*/**/bin/ffmpeg.exe"):
        return str(cand)
    sys.exit("[bordar_cartas] ffmpeg nao encontrado no PATH")


def main():
    ffmpeg = _find_ffmpeg()
    DESTINO.mkdir(exist_ok=True)
    fontes = sorted(baralho.BARALHO.glob("*.jpg"))
    if not fontes:
        sys.exit(f"[bordar_cartas] nenhum .jpg em {baralho.BARALHO}")

    feitas = puladas = 0
    for src in fontes:
        dst = DESTINO / src.name
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            puladas += 1
            continue
        r = subprocess.run([ffmpeg, "-y", "-v", "error", "-i", str(src),
                            "-vf", "pad=iw+2:ih+2:1:1:black", str(dst)])
        if r.returncode != 0:
            sys.exit(f"[bordar_cartas] ffmpeg falhou em {src.name}")
        feitas += 1

    print(f"[bordar_cartas] {feitas} carta(s) geradas, {puladas} ja em dia "
          f"-> {DESTINO}")


if __name__ == "__main__":
    main()
