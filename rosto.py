#!/usr/bin/env python3
"""rosto.py - onde fica o rosto da Mariana ao longo do video.

Por que existe
--------------
"Adicionar cartas" precisa reenquadrar a Mariana: empurra-la para a
direita quando so a carta principal esta em tela, para o centro quando
uma segunda carta aparece. Isso exige saber ONDE o rosto dela esta na
imagem - a camera e fixa (tripe), mas "fixa" nao e "parada": ela se
inclina, gesticula, sai e volta do enquadramento. Melhor medir do que
supor um crop fixo no escuro (mesmo principio do config.toml: numero e
medicao, nao gosto).

Detector: YuNet (`cv2.FaceDetectorYN`), rodando sobre frames amostrados
em baixa resolucao - a camera nao se move quadro a quadro, entao nao ha
razao para decodificar 4K60 so para achar uma cara. Os frames vem do
ffmpeg externo (nao do VideoCapture do OpenCV): e o mesmo decodificador
que o resto do tarocut ja usa para HEVC 10-bit, e o `VideoCapture`
embutido no wheel do opencv-python nao tem garantia de decodificar isso
certo. O modelo (`models/face_detection_yunet_2023mar.onnx`) fica
commitado no repo: sem chamada de rede depois do `pip install
opencv-python-headless`, do mesmo jeito que o `align` roda 100% local
depois do primeiro download do wav2vec2.

Standalone como alinhar.py/lexico.py/baralho.py: tecnica isolada, nao
precisa do resto do pipeline.
"""
import json, os, shutil, subprocess, sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent
MODELO = ROOT / "models" / "face_detection_yunet_2023mar.onnx"


def _find_bin(name):
    p = shutil.which(name)
    if p:
        return p
    base = Path(os.environ.get("LOCALAPPDATA", ""))
    for cand in base.glob(f"Microsoft/WinGet/Packages/Gyan.FFmpeg*/**/bin/{name}.exe"):
        return str(cand)
    sys.exit(f"[rosto] {name} nao encontrado no PATH")


FFMPEG = _find_bin("ffmpeg")

# 1 quadro/s basta: a camera e de tripe, o rosto nao pula de posicao entre
# um segundo e o seguinte. Amostrar mais so custaria tempo de decodificacao.
TAXA = 1.0
LARGURA_AMOSTRA = 640  # decodificar em 4K so para achar uma cara e desperdicio


def amostra_frames(video, saida_dir, taxa=TAXA, largura=LARGURA_AMOSTRA):
    """Extrai frames em baixa resolucao via ffmpeg, um a cada 1/taxa
    segundos. Devolve a lista de arquivos, na ordem (frame N = tempo
    (N-1)/taxa, e como o filtro `fps` do ffmpeg funciona)."""
    saida_dir.mkdir(parents=True, exist_ok=True)
    for f in saida_dir.glob("f_*.jpg"):
        f.unlink()
    # Sem -hwaccel isto decodifica 4K60 HEVC 10-bit em software - o mesmo
    # gargalo documentado no render (README): ~9 min para 5,5 min de video.
    # QSV e o default do projeto (config.toml [render].gpu), mas so ajuda
    # se `fps` vier ANTES de `hwdownload`: fps descarta 59 a cada 60 quadros
    # ainda em memoria de GPU, e so os que sobram pagam o hwdownload+scale
    # em CPU. Na ordem errada (hwdownload antes de fps) o programa baixa os
    # 19680 quadros do 1fps que sobra - MEDIDO: 9m10s contra 1m25s no
    # IMG_0301 (5,5 min), so trocando a ordem dos dois filtros.
    saida = [str(saida_dir / "f_%05d.jpg")]
    filtro_hw = f"fps={taxa},hwdownload,format=p010le,scale={largura}:-2"
    filtro_cpu = f"fps={taxa},scale={largura}:-2"
    r = subprocess.run([FFMPEG, "-y", "-v", "error", "-hwaccel", "qsv",
                        "-i", str(video), "-vf", filtro_hw,
                        "-q:v", "3"] + saida)
    if r.returncode != 0:
        r = subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(video),
                            "-vf", filtro_cpu, "-q:v", "3"] + saida)
    if r.returncode != 0:
        sys.exit("[rosto] ffmpeg falhou ao amostrar frames")
    return sorted(saida_dir.glob("f_*.jpg"))


def _detector(w, h):
    return cv2.FaceDetectorYN.create(str(MODELO), "", (w, h),
                                      score_threshold=0.6, nms_threshold=0.3)


def detecta(frames, taxa=TAXA, log=lambda s: print(s, flush=True)):
    """Roda o YuNet em cada frame amostrado. Devolve uma lista por
    instante: {"t", "achou", "cx", "cy", "w", "h", "score"} - cx/cy/w/h
    em FRACAO do quadro (0-1), para nao depender da resolucao da amostra
    nem da original: quem for montar o crop multiplica pela resolucao que
    quiser na hora."""
    if not frames:
        return []
    det = None
    amostras = []
    for i, f in enumerate(frames):
        img = cv2.imread(str(f))
        if img is None:
            continue
        h, w = img.shape[:2]
        if det is None:
            det = _detector(w, h)
        _, faces = det.detect(img)
        t = i / taxa
        if faces is None or len(faces) == 0:
            amostras.append({"t": t, "achou": False})
            continue
        # uma pessoa em quadro: a face de maior score e a dela. Ordena e
        # pega a primeira - o YuNet ja devolve por score, mas nao documenta
        # a ordem como garantida, entao ordena explicito.
        fa = max(faces, key=lambda r: r[-1])
        x, y, bw, bh, score = (float(v) for v in
                                (fa[0], fa[1], fa[2], fa[3], fa[-1]))
        amostras.append({
            "t": t, "achou": True,
            "cx": (x + bw / 2) / w, "cy": (y + bh / 2) / h,
            "w": bw / w, "h": bh / h, "score": score,
        })
        if log and (i + 1) % 50 == 0:
            log(f"[rosto] {i + 1}/{len(frames)} frames")
    return amostras


def suaviza(amostras, janela=5):
    """Mediana movel sobre cx/cy dos instantes com deteccao, com os
    instantes sem deteccao preenchidos por interpolacao linear entre os
    vizinhos validos. Mediana (nao media) porque um falso positivo
    isolado - um quadro do carro no fundo, por exemplo - nao pode arrastar
    a janela inteira; ele fica sozinho e a mediana o ignora."""
    validos = [a for a in amostras if a["achou"]]
    if not validos:
        return []
    ts = [a["t"] for a in validos]
    cxs = [a["cx"] for a in validos]
    cys = [a["cy"] for a in validos]

    def mediana_local(serie, k):
        n = len(serie)
        out = []
        for i in range(n):
            a, b = max(0, i - k // 2), min(n, i + k // 2 + 1)
            out.append(float(np.median(serie[a:b])))
        return out

    cxs_s = mediana_local(cxs, janela)
    cys_s = mediana_local(cys, janela)

    todos_t = [a["t"] for a in amostras]
    cx_i = np.interp(todos_t, ts, cxs_s)
    cy_i = np.interp(todos_t, ts, cys_s)
    return [{"t": t, "cx": float(x), "cy": float(y)}
            for t, x, y in zip(todos_t, cx_i, cy_i)]


def resumo(amostras, suaves):
    """Onde o rosto andou, em fracao do quadro.

    `topo_min` e `alt_med` vieram depois (09/09/2026) e existem por um
    motivo so: sem a ALTURA da caixa do rosto nao da para reenquadrar sem
    cortar a cabeca. O `cy` e o CENTRO do rosto, e uma janela montada em
    volta dele passa a lamina no meio da testa - foi o que aconteceu na
    primeira previa do layout 3 que de fato reenquadrou (ver
    `tarocut._janela_layout3`). `topo_min` e o ponto mais alto que a cabeca
    alcanca no video inteiro; e dele que a folga se mede.

    Percentil 5, e nao o minimo: uma deteccao ruim isolada - meia cara no
    canto do quadro, um rosto no cartaz da parede - subiria o topo do
    enquadramento do video inteiro. A `suaviza` protege `cx`/`cy` com
    mediana movel pela mesma razao, mas ela joga fora `w`/`h` no caminho,
    entao aqui a defesa e o percentil sobre as amostras cruas."""
    cobertura = sum(1 for a in amostras if a["achou"]) / max(1, len(amostras))
    if not suaves:
        return {"cobertura": cobertura}
    cxs = [s["cx"] for s in suaves]
    cys = [s["cy"] for s in suaves]
    achou = [a for a in amostras if a["achou"]]
    extra = {}
    if achou:
        topos = [a["cy"] - a["h"] / 2 for a in achou]
        extra = {"topo_min": float(np.percentile(topos, 5)),
                 "alt_med": float(np.median([a["h"] for a in achou]))}
    return {
        "cobertura": cobertura,
        "cx_min": min(cxs), "cx_max": max(cxs),
        "cy_min": min(cys), "cy_max": max(cys),
        # ONDE ELA FICA, que nao e o meio de onde ela ANDA: o enquadramento
        # se centra nisto (ver `tarocut._janela_layout3`). Ela se inclina de
        # vez em quando, e o meio do vaivem puxa o quadro para o lado da
        # inclinacao - a mediana fica onde ela passa a maior parte da aula.
        "cx_med": float(np.median(cxs)), "cy_med": float(np.median(cys)),
        # p5/p95, e nao min/max: e contra ELES que o enquadramento
        # decide, e um segundo de inclinacao maxima nao pode mandar no
        # quadro do video inteiro. O min/max continua ali porque e o que
        # diz o tamanho do vaivem.
        "cx_p05": float(np.percentile(cxs, 5)),
        "cx_p95": float(np.percentile(cxs, 95)),
        **extra,
    }


def rastreia(video, work_dir, log=lambda s: print(s, flush=True)):
    """Pipeline completo: amostra, detecta, suaviza, resume. Grava
    `<work_dir>/rosto.json` e devolve o mesmo dict."""
    frames_dir = work_dir / "_rosto_frames"
    log(f"[rosto] amostrando 1 quadro/s de {Path(video).name}...")
    frames = amostra_frames(video, frames_dir)
    log(f"[rosto] {len(frames)} quadros; detectando rosto...")
    amostras = detecta(frames, log=log)
    suaves = suaviza(amostras)
    res = resumo(amostras, suaves)
    log(f"[rosto] cobertura {res['cobertura']*100:.0f}%  "
        f"cx=[{res.get('cx_min', 0):.2f},{res.get('cx_max', 0):.2f}]  "
        f"cy=[{res.get('cy_min', 0):.2f},{res.get('cy_max', 0):.2f}]")
    out = {"amostras": amostras, "suavizado": suaves, "resumo": res}
    (work_dir / "rosto.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    shutil.rmtree(frames_dir, ignore_errors=True)
    return out


def amostras_anotadas(video, out_dir, n=6, taxa=TAXA):
    """Para conferencia visual (nao usado no pipeline): extrai `n` frames
    espacados ao longo do video, desenha o retangulo detectado, salva em
    `out_dir`. Roda a deteccao de novo, em resolucao mais alta (960px),
    so nesses poucos frames - o retangulo fica mais facil de julgar."""
    frames_dir = out_dir / "_amostra_full"
    frames = amostra_frames(video, frames_dir, taxa=taxa, largura=960)
    if not frames:
        return []
    passo = max(1, len(frames) // n)
    escolhidos = frames[::passo][:n]
    det = None
    saidas = []
    for f in escolhidos:
        img = cv2.imread(str(f))
        h, w = img.shape[:2]
        if det is None:
            det = _detector(w, h)
        _, faces = det.detect(img)
        if faces is not None and len(faces):
            fa = max(faces, key=lambda r: r[-1])
            x, y, bw, bh = [int(v) for v in fa[:4]]
            cv2.rectangle(img, (x, y), (x + bw, y + bh), (0, 0, 255), 2)
        idx = int(f.stem.split("_")[1])
        t = (idx - 1) / taxa
        m, s = divmod(t, 60)
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / f"rosto_{int(m):02d}m{s:05.2f}s.jpg".replace(":", "")
        cv2.imwrite(str(dest), img)
        saidas.append(dest)
    shutil.rmtree(frames_dir, ignore_errors=True)
    return saidas


if __name__ == "__main__":
    # `--amostras` existe porque `amostras_anotadas` era codigo morto: uma
    # ferramenta util (foi ela que gerou work/IMG_0301/rosto_amostras/)
    # sem nenhum jeito de rodar sem editar o arquivo. Ou se apaga, ou se
    # liga na linha de comando - apagar era jogar fora a unica maneira de
    # ver o que o detector esta vendo.
    if len(sys.argv) not in (2, 3) or (len(sys.argv) == 3
                                       and sys.argv[2] != "--amostras"):
        sys.exit("uso: rosto.py <video> [--amostras]")
    video = Path(sys.argv[1])
    wdir = ROOT / "work" / video.stem
    wdir.mkdir(parents=True, exist_ok=True)
    if len(sys.argv) == 3:
        saidas = amostras_anotadas(video, wdir / "rosto_amostras")
        for x in saidas:
            print(f"[rosto] {x}")
    else:
        rastreia(video, wdir)
