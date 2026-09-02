#!/usr/bin/env python3
"""tarocut - pre-editor de video dirigido por transcricao.

Pipeline:
  ingest      video -> meta.json + audio.wav (16k mono)
  transcribe  audio -> words.json  (Whisper: o TEXTO)
  align       words.json -> fronteiras de palavra precisas (wav2vec2 CTC).
              O Whisper acerta o texto e erra os tempos; sem esta etapa o
              corte come o "s" final e parte palavra no meio.
  analyze     words + energia -> stream.json  (fluxo fundido: o que o
              Whisper transcreveu MAIS o que ele engoliu)
  draft       stream -> transcript.md editavel
  editor      ajusta o corte pela transcricao, no navegador
  previa      toca o corte no mpv, sem renderizar
  render      aplica os cortes e normaliza o volume
"""
import argparse, bisect, json, os, re, shutil, subprocess, sys, unicodedata, wave
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import tomllib

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work"

ABRE, FECHA = "⟦", "⟧"      # marcado para CORTE
KABRE, KFECHA = "⟨", "⟩"    # resgatado para MANTER

# ---------------------------------------------------------------- utilidades

def _find_bin(name):
    p = shutil.which(name)
    if p:
        return p
    base = Path(os.environ.get("LOCALAPPDATA", ""))
    for cand in base.glob(f"Microsoft/WinGet/Packages/Gyan.FFmpeg*/**/bin/{name}.exe"):
        return str(cand)
    sys.exit(f"[erro] {name} nao encontrado no PATH")

FFMPEG = _find_bin("ffmpeg")
FFPROBE = _find_bin("ffprobe")


def cfg():
    with open(ROOT / "config.toml", "rb") as f:
        return tomllib.load(f)


def sem_acento(s):
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def chave(s):
    """Chave sem acento - para alinhamento e comparacao de repeticao."""
    return sem_acento(s).lower().strip(" .,!?;:\"'()[]…-")


def chave_ac(s):
    """Chave COM acento - para cacoetes, onde 'e' e 'é' sao coisas
    diferentes. Cortar a conjuncao 'e' destroi a frase."""
    return s.lower().strip(" .,!?;:\"'()[]…-")


# ===========================================================================
# CONCEITOS UNICOS
#
# Tudo que responde a uma mesma pergunta mora aqui, uma vez so. Este bloco
# existe porque a mesma regra chegou a viver em dois e tres lugares com
# versoes ligeiramente diferentes, e o defeito sempre aparecia pelo caminho
# que nao tinha recebido a correcao: a densidade de fala estava em
# `resolve_suspeitos` e faltava em `eh_hesitacao`; o piso de ruido era
# calculado com a configuracao aqui e com 12.0 no braco em alinhar.py.
#
# REGRA: ao precisar de uma destas respostas, CHAMAR a funcao. Nunca
# reimplementar no lugar de uso, nem "so desta vez".
# ===========================================================================

RE_ALONGAMENTO = re.compile(r"(.)\1{2,}")


def eh_alongada(txt):
    """Tem letra repetida 3+ vezes? ('queeee', 'ééé', 'naaao')"""
    return bool(RE_ALONGAMENTO.search(sem_acento(txt).lower()))


def letras_faladas(txt):
    """Quantas letras a palavra tem QUANDO FALADA.

    '2023' tem 4 caracteres escritos e 20 falados ("dois mil e vinte e
    três"). Medir pelo texto escrito fazia todo numero parecer arrastado
    5x e levar uma cauda cortada no meio - "e ele tem cin-- arcanos"."""
    try:
        from alinhar import expande
        txt = expande(txt)
    except Exception:
        pass
    return max(1, len(chave(txt).replace(" ", "")))


def densidade_fala(txt, dur):
    """Caracteres falados por segundo. Fala normal da ~13; fala lenta com
    pausa dentro cai para 4-6; vocalizacao ("Ous!" em 2,1s) fica perto de
    1,4. E o criterio para separar fala de ruido."""
    return len(chave(txt).replace(" ", "")) / max(0.1, dur)


def limiar_voz(db, offset_db):
    """Piso de ruido do proprio audio + folga = limiar de fala, em dB."""
    import numpy as np
    return float(np.percentile(db, 10)) + offset_db


def mede_voz(db, hop, a, b, limiar):
    """Quanto do trecho [a,b] tem voz -> (fracao de quadros, pico em dB)."""
    seg = db[int(a / hop):int(b / hop)]
    if len(seg) < 2:
        return 0.0, limiar
    return float((seg > limiar).mean()), float(seg.max())


def parece_frase(dur, frac, pico, limiar, ch):
    """Trecho SEM TEXTO grande e alto demais para ser cacoete?

    Quando o transcritor nao devolve texto, o programa vinha concluindo que
    nao havia nada ali e cortava. Errado: o microfone ouviu. Em IMG_0247
    isso apagou quatro frases inteiras - "outros tendem, muitas vezes, a
    seguir essa...", "e a leitura", "tem algo que a gente pode extrair",
    "nao tem nem as vezes um...".

    MEDIDO nos dois videos, relendo cada trecho com contexto: as cinco
    frases reais dao 1,10-1,77s, 57-93% dos quadros com voz e pico 37-45 dB
    acima do limiar de fala. O que fica (cacoete, "eeee" arrastado, folheio
    de carta) da 32-53% de voz, ou menos de 0,9s.

    A faixa de 55-61% e ambigua de verdade - um "É" arrastado e uma frase
    curta medem igual - e por isso a regra e FROUXA de proposito. Nao se
    corta no escuro: o trecho fica e aparece como [?? N.Ns] no revisao.txt
    para o passe de julgamento decidir. Sobrar cacoete se conserta numa
    anotacao; frase apagada nao se conserta, porque ninguem ve."""
    return (dur >= ch["guarda_dur"] and frac >= ch["guarda_voz"]
            and pico - limiar >= ch["guarda_db"])


def classifica_fala(txt, dur, sempre, dens_min):
    """O texto recuperado num trecho e cacoete ou e fala? -> (cortar, motivo)

    Usada por TODOS os caminhos que recuperam texto de um trecho: lacuna
    entre palavras, excesso de palavra com duracao impossivel, e o que
    vier depois."""
    if not txt or not chave(txt):
        return True, "sem texto"
    dens = densidade_fala(txt, dur)
    if dens < dens_min:
        return True, f"nao e fala ({dens:.1f} car/s)"
    toks = [chave_ac(w) for w in txt.split() if chave_ac(w)]
    if len(toks) <= 3 and all(t in sempre or len(t) <= 3 or eh_alongada(t)
                              for t in toks):
        return True, f"hesitação recuperada: {txt[:24]}"
    return False, f"fala nao transcrita: {txt[:26]}"


def hhmmss(t):
    h, r = divmod(int(t), 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def wdir(video):
    d = WORK / Path(video).stem
    d.mkdir(parents=True, exist_ok=True)
    return d


def jdump(obj, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def jload(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)

# ------------------------------------------------------------------- lexico

def carrega_fillers():
    sempre, contextual = set(), set()
    p = ROOT / "lexicon" / "fillers.txt"
    if not p.exists():
        return sempre, contextual
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.split("#")[0].strip()
        if not ln:
            continue
        if ln.startswith("~"):
            contextual.add(chave_ac(ln[1:]))
        else:
            sempre.add(chave_ac(ln))
    return sempre, contextual


def carrega_termos():
    """Retorna (canonicos, mapa_de_1_palavra, mapa_de_frase).

    O mapa de frase existe porque "tarot de ala" -> "Tarô de Yala" tem tres
    palavras: comparando token a token nunca casaria. Chave = tupla de
    palavras normalizadas."""
    canon, uma, frase = [], {}, {}
    p = ROOT / "lexicon" / "terms.txt"
    if not p.exists():
        return canon, uma, frase
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.split("#")[0].strip()
        if not ln:
            continue
        partes = [x.strip() for x in ln.split("|") if x.strip()]
        if not partes:
            continue
        canon.append(partes[0])
        for erro in partes[1:]:
            ks = tuple(chave(w) for w in erro.split() if chave(w))
            if len(ks) == 1:
                uma[ks[0]] = partes[0]
            elif len(ks) > 1:
                frase[ks] = partes[0]
    return canon, uma, frase


def corrige_frases(words, frase):
    """Troca erros de varias palavras no lugar. Quando a correcao tem o
    mesmo numero de palavras, substitui uma a uma; quando nao tem, poe o
    texto inteiro no primeiro token e esvazia os demais - os tempos
    continuam certos de qualquer jeito."""
    if not frase:
        return 0
    ordem = sorted(frase, key=len, reverse=True)
    ks = [chave(w["w"]) for w in words]
    n, i = 0, 0
    while i < len(words):
        for alvo in ordem:
            L = len(alvo)
            if tuple(ks[i:i + L]) != alvo:
                continue
            novo = frase[alvo].split()
            if len(novo) == L:
                for j, t in enumerate(novo):
                    words[i + j]["w"] = t
            else:
                words[i]["w"] = frase[alvo]
                for j in range(1, L):
                    words[i + j]["w"] = ""
            n += 1
            i += L - 1
            break
        i += 1
    return n


def initial_prompt():
    """Prompt que (a) ancora vocabulario e (b) sinaliza ao Whisper que
    disfluencias devem ser transcritas, nao limpas."""
    canon, _, _ = carrega_termos()
    vocab = ", ".join(canon[:30])
    return (f"Aula sobre tarô. Vocabulário: {vocab}. "
            "Transcrição literal, incluindo hesitações: "
            "é... éé... ahn, hmm, tipo assim, né? "
            "Então... então... quer dizer, e... e... "
            "que que eu ia dizer mesmo?")

# ------------------------------------------------------------------- ingest

def cmd_ingest(a):
    # As outras etapas aceitam o nome curto ("IMG_0247") porque o `wdir`
    # resolve. Esta nao: ela e quem escreve o meta.json com o caminho de
    # verdade. Sem esta linha, o nome curto derrubava o ffprobe e saia um
    # traceback com KeyError: 'streams'.
    if not Path(a.video).exists():
        sys.exit(f"[ingest] nao achei {a.video} - esta etapa precisa do "
                 f"caminho do arquivo, nao do nome curto.")
    d = wdir(a.video)
    r = run([FFPROBE, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", a.video])
    info = json.loads(r.stdout)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    meta = {"src": str(Path(a.video).resolve()),
            "dur": float(info["format"]["duration"]),
            "w": v["width"], "h": v["height"],
            "fps": v["r_frame_rate"], "vcodec": v["codec_name"]}
    jdump(meta, d / "meta.json")

    wav = d / "audio.wav"
    if not wav.exists() or a.force:
        print(f"[ingest] extraindo audio de {Path(a.video).name} "
              f"({hhmmss(meta['dur'])})...", flush=True)
        run([FFMPEG, "-y", "-v", "error", "-i", a.video, "-vn",
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
    print(f"[ingest] ok  {meta['w']}x{meta['h']} {meta['vcodec']}  "
          f"{hhmmss(meta['dur'])}  -> {d}")

# --------------------------------------------------------------- transcribe

def cmd_transcribe(a):
    from faster_whisper import WhisperModel
    c = cfg()["whisper"]
    d = wdir(a.video)
    out = d / "words.json"
    if out.exists() and not a.force:
        print(f"[transcribe] cache: {out}")
        return
    modelo = a.model or c["model"]
    print(f"[transcribe] modelo={modelo} ({c['compute_type']}) ...", flush=True)
    m = WhisperModel(modelo, device="cpu", compute_type=c["compute_type"])
    segs, _ = m.transcribe(
        str(d / "audio.wav"), language=c["language"],
        word_timestamps=True, vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=300),
        condition_on_previous_text=False, temperature=0.0,
        beam_size=c["beam_size"], initial_prompt=initial_prompt())
    words, nseg = [], 0
    for s in segs:
        nseg += 1
        for w in (s.words or []):
            words.append({"w": w.word.strip(), "s": round(w.start, 3),
                          "e": round(w.end, 3), "p": round(w.probability, 3)})
        if nseg % 20 == 0:
            print(f"  ... {nseg} segmentos, {len(words)} palavras "
                  f"(t={s.end:.0f}s)", flush=True)
    jdump({"model": modelo, "words": words}, out)
    print(f"[transcribe] {len(words)} palavras -> {out}")

# ------------------------------------------------------------------- align

def cmd_align(a):
    """Refina as fronteiras de palavra com alinhamento forcado.

    Etapa separada e com cache: e a mais cara do pipeline (~1x tempo real),
    e o resultado nao muda a menos que a transcricao mude."""
    d = wdir(a.video)
    p = d / "words.json"
    W = jload(p)
    if W.get("alinhado") and not a.force:
        print(f"[align] cache: {p}")
        return
    import alinhar
    words, info = alinhar.alinha_video(W["words"], d / "audio.wav")
    W["words"] = words
    W["alinhado"] = True
    W["align_info"] = info
    jdump(W, p)
    print(f"[align] -> {p}")

# ------------------------------------------------------------------ energia

def envelope(wav_path, hop=0.02):
    import numpy as np
    w = wave.open(str(wav_path), "rb")
    sr = w.getframerate()
    pcm = np.frombuffer(w.readframes(w.getnframes()),
                        dtype=np.int16).astype(np.float32) / 32768.0
    n = int(hop * sr)
    fr = pcm[:len(pcm) // n * n].reshape(-1, n)
    db = 20 * np.log10(np.sqrt((fr ** 2).mean(axis=1) + 1e-12))
    return db, hop, len(pcm) / sr

# ------------------------------------------------- guardas anti-alucinacao

HALLU = [r"^tchau", r"obrigad", r"legenda", r"amara\.org", r"inscreva",
         r"at[ée] a pr[óo]xima", r"subtitle", r"^valeu",
         r"bom dia", r"boa noite", r"^m[úu]sica", r"^\[.*\]$"]


def alucinou(txt, dur, logprob, c):
    """Retorna motivo da rejeicao, ou None se a re-transcricao e confiavel."""
    if not txt or len(chave(txt)) < 1:
        return "vazio"
    t = txt.lower().strip()
    for pat in HALLU:
        if re.search(pat, t):
            return "frase-fantasma"
    toks = [chave(x) for x in txt.split() if chave(x)]
    if dur > 0 and len(toks) / dur > c["max_palavras_seg"]:
        return f"denso demais ({len(toks)/dur:.1f} pal/s)"
    for n in (1, 2, 3):                       # loop travado
        for i in range(len(toks) - n * 4 + 1):
            bloco = toks[i:i + n]
            if all(toks[i + k * n:i + (k + 1) * n] == bloco for k in range(4)):
                return "loop repetido"
    if logprob is not None and logprob < c["min_logprob"]:
        return f"logprob {logprob:.2f}"
    return None

# --------------------------------------------------- marcacao: cacoetes

def resolve_suspeitos(stream, escuta, ch, sempre, dens_min, db, hop, limiar):
    """Descobre o que ha nos trechos de duracao impossivel.

    Quando o Whisper deixa de transcrever alguma coisa, o alinhador nao tem
    onde por o audio sobrando e o gruda na palavra vizinha - 'principalmente'
    ficou com 4,67s escondendo um "nessas cartas" repetido. Aqui esse excesso
    e re-transcrito isolado e vira um token proprio, com texto de verdade,
    em vez de virar uma cauda cortada no escuro.

    Corta quando o que aparece repete o que vem depois (o Whisper colapsou
    uma repeticao) ou parece cacoete. Se for conteudo novo, PRESERVA - fala
    perdida nao pode virar corte silencioso."""
    novos, n = [], 0
    for t in stream:
        if not t.get("suspeito"):
            novos.append(t)
            continue
        a, b = t["suspeito"]
        txt, lp = escuta(a, b)
        mot = alucinou(txt, b - a, lp, ch)
        t["e"] = a
        t.pop("suspeito", None)
        novos.append(t)
        if mot:
            # O Whisper nao ter lido NAO e prova de que nao ha nada. Antes
            # de apagar, pergunta-se ao microfone (ver `parece_frase`).
            frac, pico = mede_voz(db, hop, a, b, limiar)
            if parece_frase(b - a, frac, pico, limiar, ch):
                rot, cortar = f"?? {b-a:.1f}s", False
                why = (f"som alto sem texto ({frac*100:.0f}% de voz, "
                       f"pico {pico-limiar:.0f}dB acima) - nao se corta no "
                       f"escuro; rejeicao do Whisper: {mot}")
            else:
                rot, cortar, why = ("hesitação", True,
                                    f"voz sem texto {b-a:.1f}s")
        else:
            cortar, why = classifica_fala(txt, b - a, sempre, dens_min)
            rot = txt        # o eco e decidido depois, por marca_ecos
        novos.append({"k": "hesitacao", "t": rot, "s": round(a, 3),
                      "e": round(b, 3), "cut": cortar, "why": why})
        n += 1
    if n:
        stream[:] = novos
    return n


def carrega_marcadores():
    p = ROOT / "lexicon" / "marcadores.txt"
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.split("#")[0].strip()
        if ln:
            ks = tuple(chave(w) for w in ln.split() if chave(w))
            if ks:
                out.append(ks)
    return sorted(out, key=len, reverse=True)


def marca_instrucoes(stream, marcadores, respiro, max_volta=25.0):
    """Corta onde ela MANDA cortar.

    Gravando sozinha, a Mariana narra a propria edicao: "nao, corta essa
    parte", "corto essa parte, espere, deixa eu pensar". Duas vezes em dez
    minutos no IMG_0248. Nenhuma heuristica chega perto dessa confianca -
    e a locutora dizendo literalmente o que quer fora.

    Descarta a frase da instrucao e a tentativa abandonada anterior, voltando
    ate o ultimo respiro (pausa ou hesitacao) - que e onde a tentativa
    comecou."""
    if not marcadores:
        return 0
    idx = [i for i, t in enumerate(stream) if t["k"] == "palavra"]
    keys = [chave(stream[i]["t"]) for i in idx]
    n = 0
    for p in range(len(idx)):
        for m in marcadores:
            L = len(m)
            if tuple(keys[p:p + L]) != m:
                continue
            fim = idx[p + L - 1]
            # volta ate o respiro anterior: ali comecou a tentativa ruim
            ini = idx[p]
            k = idx[p] - 1
            while k >= 0:
                if stream[fim]["e"] - stream[k]["s"] > max_volta:
                    break
                if (stream[k]["k"] == "silencio"
                        and stream[k]["e"] - stream[k]["s"] > respiro):
                    break
                ini = k
                k -= 1
            for j in range(ini, fim + 1):
                if not stream[j]["cut"]:
                    n += 1
                stream[j]["cut"] = True
                stream[j]["prot"] = False
                stream[j]["why"] = f"ela mandou cortar: {' '.join(m)}"
            break
    return n


def marca_ecos(stream, janela=6):
    """Trecho recuperado que REPETE uma palavra vizinha e retomada.

    Passa sobre o fluxo inteiro, uma vez, para todo trecho de texto
    recuperado - venha ele de lacuna entre palavras ou de palavra com
    duracao impossivel. A regra ja existiu so num dos dois caminhos, e pelo
    outro escapava "como se [como seja...] existisse uma tradicao"."""
    n = 0
    for i, t in enumerate(stream):
        if t["k"] != "hesitacao" or t["cut"] or not chave(t["t"]):
            continue
        viz = {chave(x["t"])
               for x in stream[max(0, i - janela):i + janela + 1]
               if x["k"] == "palavra" and chave(x["t"])}
        pals = [p for p in chave(t["t"]).split() if len(p) > 2]
        if any(p in viz for p in pals):
            t["cut"] = True
            t["why"] = f"retomada colapsada: {t['t'][:26]}"
            n += 1
    return n


def _e_lacuna(t):
    return t["k"] in ("silencio", "hesitacao")


# Palavras cuja repeticao imediata e enfase deliberada, nao gagueira.
ENFASE = {"nao", "sim", "muito", "bem", "so", "ja", "mais", "menos",
          "devagar", "calma", "obrigada", "isso"}


def marca_cacoetes(stream, sempre, contextual):
    """Marca cacoetes. Os ambiguos (é, e, então, né...) so caem quando
    estao ILHADOS entre pausas - senao cortariamos a conjuncao 'e' em
    'cavaleiros e cavaleiras' e a frase desmontaria."""
    n = 0
    for i, t in enumerate(stream):
        if t["k"] != "palavra" or t["cut"]:
            continue
        k = chave_ac(t["t"])
        ant = stream[i - 1] if i > 0 else None
        pro = stream[i + 1] if i + 1 < len(stream) else None

        if k in sempre:
            t["cut"], t["why"] = True, "cacoete"
        elif eh_alongada(t["t"]):
            t["cut"], t["why"] = True, "alongamento"
        elif k in contextual and ant is not None and pro is not None \
                and _e_lacuna(ant) and _e_lacuna(pro):
            t["cut"], t["why"] = True, "muleta isolada"
        elif (pro is not None and pro["k"] == "palavra"
              and chave(t["t"]) and chave(pro["t"]) == chave(t["t"])
              and chave(t["t"]) not in ENFASE and len(chave(t["t"])) > 2):
            # palavra identica repetida em sequencia ("cavaleiras,
            # cavaleiras") e gagueira, nao escolha. Corta a primeira e
            # deixa a segunda. ENFASE protege o uso retorico ("nao, nao").
            t["cut"], t["why"] = True, "gaguejada"
        if t["cut"]:
            n += 1
    return n

# ------------------------------------------ marcacao: tentativas de fala

def detecta_tentativas(stream, min_ngram=3, janela_s=30.0, max_span=15.0,
                       max_palavras=12):
    """Objetivo 3: ela comeca uma frase, para, recomeca, ate acertar.

    Procura a mesma sequencia de >=min_ngram palavras reaparecendo pouco
    depois. A primeira ocorrencia (e a bagunca ate a ultima) e tentativa;
    a ultima e a boa.

    O PERIGO e confundir isso com PARALELISMO DIDATICO, que essa aula tem
    aos montes: "que ta relacionado ao elemento fogo / agua / ar / terra".
    Repeticao mecanica nao distingue os dois. Tres filtros separam:

      1. a retomada tem que vir logo APOS UMA PAUSA - numa lista paralela
         a frase repetida vem grudada no meio da sentenca ("Copas, que ta
         relacionado..."), nao depois de um respiro;
      2. o trecho descartado tem que ser CURTO (<=max_palavras) - tentativa
         abandonada e curta, item de lista e uma oracao inteira;
      3. tem que haver uma PAUSA OU HESITACAO dentro do trecho - o ponto
         onde ela travou.

    Ainda assim isto e uma proposta conservadora: o caso duvidoso e melhor
    resolvido lendo o texto (comando `review`)."""
    idx = [i for i, t in enumerate(stream) if t["k"] == "palavra"]
    keys = [chave(stream[i]["t"]) for i in idx]
    n = len(idx)
    cadeias, consumido = [], set()

    for a in range(n):
        if a in consumido or not keys[a]:
            continue
        achou = None
        for b in range(a + min_ngram, min(n, a + 90)):
            if stream[idx[b]]["s"] - stream[idx[a]]["s"] > janela_s:
                break
            m = 0
            while (a + m < b and b + m < n
                   and keys[a + m] and keys[a + m] == keys[b + m]):
                m += 1
            if m < min_ngram:
                continue
            # filtro 1: a retomada precisa vir depois de um respiro
            ant = stream[idx[b] - 1] if idx[b] > 0 else None
            if ant is None or not _e_lacuna(ant):
                continue
            achou = (b, m)
            break
        if not achou:
            continue
        b, m = achou
        ini, fim = idx[a], idx[b]
        dur = stream[fim]["s"] - stream[ini]["s"]
        # Repeticao LONGA afrouxa os limites. Sete palavras identicas
        # reaparecendo depois de 18s de pausa nao sao paralelismo didatico,
        # sao retomada - e foi assim que escapou "força de um arcano menor,
        # ela não ... está atrelada à [18s] força de um arcano menor, ela
        # não está necessariamente atrelada".
        longa = m >= 5
        lim_span = max_span * 3 if longa else max_span
        lim_pal = max_palavras * 2.5 if longa else max_palavras
        if dur > lim_span:
            continue
        # filtro 2: tentativa abandonada e curta; item de lista nao e
        if b - a > lim_pal:
            continue
        # filtro 3: precisa existir o ponto onde ela travou
        if not any(_e_lacuna(stream[j]) for j in range(ini, fim)):
            continue
        cadeias.append({
            "ini": ini, "fim": fim, "n": m, "dur": round(dur, 2),
            "t_ini": stream[ini]["s"], "t_fim": stream[fim]["s"],
            "repetido": " ".join(stream[idx[a + k]]["t"] for k in range(m)),
            "descartado": " ".join(stream[j]["t"] for j in range(ini, fim)
                                   if stream[j]["k"] == "palavra"),
        })
        consumido.update(range(a, b))
    return cadeias


def eh_hesitacao(lac, palavra_antes, sempre, ch, limiar):
    """"Voz com energia e sem texto" NAO e sempre cacoete. Decide se corta.

    Descoberto do jeito ruim: o Whisper escreveu "2023" como um token curto,
    mas ela falou "dois mil e vinte e tres" durante ~1,5s. A sobra virou
    "voz sem texto", foi classificada como hesitacao e cortada - apagando o
    ano da frase.

    A trava "nunca depois de numero" que eu tinha posto aqui deixou de ser
    necessaria e virou estorvo: a etapa `align` expande "2023" ate 210,81,
    cobrindo o numero falado inteiro, e nao sobra lacuna nenhuma. Manter a
    trava so protegeria cacoete legitimo que venha depois de um numero.

    Fica a trava que continua valendo: se a re-transcricao isolada devolveu
    PALAVRAS DE VERDADE, isso e fala que o Whisper perdeu, nao cacoete."""
    txt = (lac.get("texto") or "").strip()
    if not txt:
        # Sem texto nao quer dizer sem fala. Ver `parece_frase`: acima de
        # certo tamanho e certa energia isto e frase, e frase nao se apaga
        # so porque o transcritor nao deu conta de ler.
        if parece_frase(lac["dur"], lac.get("voz", 0.0),
                        lac.get("pico", limiar), limiar, ch):
            return False, (f"som alto sem texto ({lac.get('voz',0)*100:.0f}% "
                           f"de voz) - mantido para julgamento")
        return True, f"voz sem texto {lac['dur']:.1f}s"
    return classifica_fala(txt, lac["dur"], sempre, ch["densidade_minima"])


def carrega_anotacoes(d):
    """work/<video>/anotacoes.json - cortes de julgamento.

    Existe porque ha coisas que nenhuma heuristica pega: a Mariana ser
    interrompida ("Que foi, Rafudo?"), ou refazer o mesmo raciocinio com
    outras palavras (repeticao literal nao casa). Sao trechos escolhidos
    lendo o texto. Formato:

        [{"ini": 228.1, "fim": 231.4, "motivo": "interrupcao domestica"}]

    Fica em arquivo separado do stream para sobreviver a re-analise: apagar
    e refazer a transcricao nao perde o julgamento ja feito."""
    p = d / "anotacoes.json"
    if not p.exists():
        return []
    try:
        dados = jload(p)
    except Exception as e:
        print(f"[analyze] anotacoes.json ilegivel ({e}); ignorando")
        return []
    ok = []
    for a in dados if isinstance(dados, list) else []:
        try:
            ini, fim = float(a["ini"]), float(a["fim"])
        except (KeyError, TypeError, ValueError):
            continue
        if fim > ini:
            ok.append({"ini": ini, "fim": fim,
                       "motivo": str(a.get("motivo", "anotacao"))})
    return ok


def encaixa_anotacoes(stream, anot):
    """Encolhe cada anotacao ate cair em fronteira de palavra.

    Uma anotacao e escrita a olho, lendo tempos da transcricao, e por isso
    cai facil no meio de uma palavra - foi assim que 'reis,' (155,17-156,33)
    sumiu de 'as rainhas estao acima dos reis' quando anotei o corte
    comecando em 156,30. Cortar para dentro so pode perder o que ja se
    queria perder; cortar para fora comeria a palavra vizinha."""
    pal = [t for t in stream if t["k"] == "palavra"]
    for a in anot:
        ini, fim = a["ini"], a["fim"]
        # so mexe na borda que ATRAVESSA uma palavra. Encolher ate a
        # primeira palavra inteira jogaria fora o silencio das pontas -
        # justamente o que a anotacao do fim do video precisa cortar.
        for t in pal:
            if t["s"] < ini < t["e"]:
                ini = t["e"]
            if t["s"] < fim < t["e"]:
                fim = t["s"]
        if abs(ini - a["ini"]) > 0.01 or abs(fim - a["fim"]) > 0.01:
            a["ajustada"] = [round(a["ini"], 2), round(a["fim"], 2)]
        a["ini"], a["fim"] = ini, fim
    return [a for a in anot if a["fim"] - a["ini"] > 0.05]


def aplica_anotacoes(stream, anot):
    """Marca para corte todo token dentro de um trecho anotado. Vence as
    protecoes: se foi julgado, sai."""
    n = 0
    for a in anot:
        for t in stream:
            if t["e"] <= a["ini"] or t["s"] >= a["fim"]:
                continue
            if not t["cut"]:
                n += 1
            t["cut"] = True
            t["prot"] = False
            t["why"] = f"anotado: {a['motivo']}"
    return n



def marca_arrastadas(stream, sempre, C):
    """Alongamento detectado por DURACAO, nao por letra repetida.

    O Whisper escreve "queeeee" como "que" e "ééé" como "e": o texto nao
    denuncia nada, mas a palavra ocupa varias vezes o tempo normal. Compara
    cada palavra com o ritmo mediano do proprio video (cada pessoa fala num
    ritmo; a mediana se adapta sozinha).

    Palavra de UMA letra arrastada e cacoete puro e sai inteira. Palavra
    maior tem so a CAUDA cortada, preservando o comeco - "em" arrastado por
    2,7s vira "em", nao vira nada. E o que um editor humano faz."""
    import statistics
    ca = C["arrastada"]
    pal = [t for t in stream if t["k"] == "palavra" and chave(t["t"])]
    if len(pal) < 20:
        return 0
    med = statistics.median((t["e"] - t["s"]) / letras_faladas(t["t"])
                            for t in pal)
    n = 0
    for t in pal:
        if t["cut"] or t.get("prot"):
            continue
        L = letras_faladas(t["t"])
        dur, esperado = t["e"] - t["s"], med * L
        if dur < ca["min_dur"] or dur < esperado * ca["fator"]:
            continue
        vezes = dur / esperado
        excesso = dur - esperado * ca["manter"]
        # Excesso GRANDE nao e arrasto: e fala que o Whisper nao transcreveu
        # e o alinhador teve que absorver na palavra vizinha. Foi o caso de
        # 'principalmente' com 4,67s - ali dentro havia um "nessas cartas"
        # repetido que o Whisper colapsou. Cortar uma cauda fixa nesse
        # trecho e chute; marca-se para o analyze re-transcrever e decidir.
        if excesso > ca["excesso_suspeito"]:
            t["suspeito"] = [round(t["s"] + esperado * ca["manter"], 3),
                             round(t["e"], 3)]
            t["why"] = (f"duracao impossivel ({vezes:.0f}x): "
                        f"{excesso:.1f}s de fala nao transcrita")
            n += 1
            continue
        # So sai INTEIRA se for cacoete do lexico. Uma palavra de verdade,
        # por curta que seja, perde apenas a cauda: a Mariana arrasta a
        # conjuncao ("ar... eeeee... ouro") e apagar o "e" quebraria a
        # frase. Encurtar de 1,06s para 0,12s ja resolve o incomodo.
        if chave_ac(t["t"]) in sempre:
            t["cut"] = True
            t["why"] = f"arrastada {vezes:.0f}x"
        else:
            corte_ini = t["s"] + esperado * ca["manter"]
            if t["e"] - corte_ini < 0.25:
                continue
            t["cauda"] = [round(corte_ini, 3), t["e"]]
            t["why"] = f"cauda arrastada {vezes:.0f}x"
        n += 1
    return n


# A contagem de cartas - "As, dois, tres, quatro..." - tem que correr
# inteira. So entram aqui numeros INEQUIVOCOS. "as", "um" e "uma" ficaram
# de fora de proposito: `chave()` tira o acento, entao a carta "as" e o
# artigo "as" viram a mesma string, e artigo e das palavras mais comuns do
# idioma. Com eles dentro, o IMG_0247 gerava 6 sequencias e TODAS eram
# falsas ("As figuras, as", "uma acao, uma", "as vezes um"); no total dos
# tres videos a protecao salvou de corte exatamente um token - um "Hum!",
# cacoete que devia sair mesmo.
RE_NUM = re.compile(
    r"^\d+[ºªo]?$|^(dois|duas|tres|quatro|cinco|seis|sete|oito|"
    r"nove|dez|onze|doze|treze|quatorze|catorze|quinze|dezesseis|"
    r"dezessete|dezoito|dezenove|vinte)$")

# Numero AMBIGUO: pega carona numa sequencia que ja se sustenta sem ele,
# nunca a abre nem a sustenta sozinho.
RE_NUM_FRACO = re.compile(r"^(as|um|uma)$")

NAIPES = {"ouros", "ouro", "paus", "pau", "copas", "copa", "espadas",
          "espada", "bastoes", "bastao", "moedas", "moeda"}


def classe_num(pal, k):
    """O que este token e para a varredura de contagem: numero inequivoco
    ("forte"), numero que tambem e artigo ("fraco") ou nada.

    O As e a excecao que vale codigo: acompanhado do naipe - "As de Ouros",
    "As de Paus" - ele e carta, nao artigo, e conta como forte. Sozinho e
    artigo ate prova em contrario, e a prova e a vizinhanca (ver
    `protege_enumeracoes`)."""
    c = chave(pal[k]["t"])
    if RE_NUM.match(c):
        return "forte"
    if (c == "as" and k + 2 < len(pal) and chave(pal[k + 1]["t"]) == "de"
            and chave(pal[k + 2]["t"]) in NAIPES):
        return "forte"
    if RE_NUM_FRACO.match(c):
        return "fraco"
    return None


def protege_enumeracoes(stream, cn, minimo=3):
    """Contagem corrida - "As, 2, 3, 4, 5, 6, 7, 8, 9, 10" - tem que correr
    inteira. Encurtar as pausas entre os numeros quebra o ritmo e o video
    fica sem sentido. Marca a sequencia como intocavel.

    Duas exigencias, as duas medidas (ver [contagem] no config):

    1. `fortes_min` numeros inequivocos. Sem isso "as vezes e um filtro"
       virava contagem, e protecao falsa nao e neutra: ela impede corte de
       cacoete e - pior - cancelava a poda de cauda arrastada.
    2. Numeros vizinhos NO TEMPO. Silencio nao e token `palavra`, entao a
       varredura nao o enxerga: sem esta trava, "As figuras" (80,0s) e "as"
       (86,0s) pareciam lado a lado e a sequencia falsa atravessava 5,6s de
       silencio."""
    idx = [i for i, t in enumerate(stream) if t["k"] == "palavra"]
    pal = [stream[i] for i in idx]
    cls = [classe_num(pal, k) for k in range(len(pal))]
    n, i = 0, 0
    while i < len(idx):
        if not cls[i]:
            i += 1
            continue
        # `ult` e o ultimo numero visto - a distancia se mede a partir dele,
        # nao do token anterior, senao uma palavra qualquer no meio serve de
        # ponte por cima da pausa.
        j, falhas, ult = i, 0, i
        while j + 1 < len(idx) and falhas < 2:
            if pal[j + 1]["s"] - pal[ult]["e"] > cn["gap_max"]:
                break
            if cls[j + 1]:
                j += 1
                falhas = 0
                ult = j
            else:
                falhas += 1
                j += 1
        j = ult
        fortes = sum(1 for k in range(i, j + 1) if cls[k] == "forte")
        if j - i + 1 >= minimo and fortes >= cn["fortes_min"]:
            for k in range(idx[i], idx[j] + 1):
                stream[k]["prot"] = True
                if stream[k]["cut"]:
                    stream[k]["cut"] = False
                    stream[k]["why"] = "protegido: contagem"
                    n += 1
            i = j + 1
        else:
            # sequencia recusada nao consome o que vem depois: a contagem de
            # verdade pode comecar dentro dela.
            i += 1
    return n


def marca_tentativas(stream, cadeias):
    n = 0
    for c in cadeias:
        for j in range(c["ini"], c["fim"]):
            if not stream[j]["cut"]:
                stream[j]["cut"] = True
                stream[j]["why"] = "tentativa"
                n += 1
    return n

# ------------------------------------------------------------------ analyze

def cmd_analyze(a):
    import numpy as np
    from faster_whisper import WhisperModel
    C = cfg()
    cs, ch, cw = C["silencio"], C["hesitacao"], C["whisper"]
    d = wdir(a.video)
    words = jload(d / "words.json")["words"]
    dur_total = jload(d / "meta.json")["dur"]
    db, hop, dur_wav = envelope(d / "audio.wav")

    limiar = limiar_voz(db, cs["limiar_fala_db"])
    voz = db > limiar
    print(f"[analyze] limiar de fala={limiar:.1f}dB  "
          f"{100*voz.mean():.0f}% dos frames com voz", flush=True)

    # --- classifica lacunas -------------------------------------------
    lacunas = []
    for i, (x, y) in enumerate(zip(words, words[1:])):
        g = y["s"] - x["e"]
        if g < cs["gap_min"]:
            continue
        # `pico` vai junto da fracao porque a fracao sozinha nao distingue
        # folheio de carta (muitos quadros, todos fracos) de fala. As duas
        # medidas saem de `mede_voz`, a mesma que o resto do programa usa.
        f, pico = mede_voz(db, hop, x["e"], y["s"], limiar)
        tipo = ("fala" if f > cs["frac_voz_fala"]
                else "silencio" if f <= cs["frac_voz_silencio"] else "misto")
        lacunas.append({"i": i, "s": x["e"], "e": y["s"], "dur": round(g, 3),
                        "voz": round(f, 3), "pico": round(pico, 1),
                        "tipo": tipo})

    alvo = [l for l in lacunas if l["tipo"] in ("fala", "misto")
            and l["dur"] >= ch["isolar_min"]]
    print(f"[analyze] {len(lacunas)} lacunas; {len(alvo)} com voz e "
          f"duracao suficiente -> re-transcricao isolada", flush=True)

    # --- re-transcreve as lacunas com voz ------------------------------
    _cache = {}

    def escuta(s0, e0):
        """Re-transcreve um trecho isolado. Carrega o Whisper so na
        primeira chamada e reaproveita depois."""
        if "m" not in _cache:
            w = wave.open(str(d / "audio.wav"), "rb")
            _cache["sr"] = w.getframerate()
            _cache["pcm"] = np.frombuffer(w.readframes(w.getnframes()),
                                          dtype=np.int16)
            w.close()
            _cache["m"] = WhisperModel(a.model or cw["model"], device="cpu",
                                       compute_type=cw["compute_type"])
        sr, pcm, m = _cache["sr"], _cache["pcm"], _cache["m"]
        s0, e0 = max(0.0, s0), min(dur_wav, e0)
        tmp = d / "_clip.wav"
        ww = wave.open(str(tmp), "wb")
        ww.setnchannels(1)
        ww.setsampwidth(2)
        ww.setframerate(sr)
        ww.writeframes(pcm[int(s0 * sr):int(e0 * sr)].tobytes())
        ww.close()
        segs, _ = m.transcribe(str(tmp), language=cw["language"],
                               vad_filter=False, temperature=0.0,
                               condition_on_previous_text=False,
                               beam_size=cw["beam_size"],
                               no_speech_threshold=0.95)
        segs = list(segs)
        txt = " ".join(s.text.strip() for s in segs).strip()
        lp = (sum(s.avg_logprob for s in segs) / len(segs)) if segs else None
        return txt, lp

    for k, l in enumerate(alvo, 1):
        txt, lp = escuta(l["s"] - 0.10, l["e"] + 0.10)
        mot = alucinou(txt, l["dur"], lp, ch)
        l["bruto"] = txt
        l["rejeitado"] = mot
        l["texto"] = "" if mot else txt
        if k % 10 == 0 or k == len(alvo):
            print(f"  ... {k}/{len(alvo)}", flush=True)

    # --- funde tudo num unico fluxo ------------------------------------
    sempre, contextual = carrega_fillers()
    _, corr, corr_frase = carrega_termos()
    nf = corrige_frases(words, corr_frase)
    if nf:
        print(f"[analyze] correcoes de frase aplicadas: {nf}")
    porlac = {l["i"]: l for l in lacunas}
    stream = []

    def add(kind, texto, s, e, cortar, motivo):
        stream.append({"k": kind, "t": texto, "s": round(s, 3),
                       "e": round(e, 3), "cut": cortar, "why": motivo})

    if words and words[0]["s"] > cs["pausa_intocavel"]:
        add("silencio", "", 0.0, words[0]["s"], True, "abertura")

    for i, x in enumerate(words):
        texto = corr.get(chave(x["w"]), x["w"])
        add("palavra", texto, x["s"], x["e"], False, "")

        l = porlac.get(i)
        if not l:
            continue
        if l["tipo"] == "silencio" or (l["tipo"] == "misto" and not l.get("texto")):
            if l["dur"] > cs["pausa_intocavel"]:
                add("silencio", "", l["s"], l["e"], True,
                    f"pausa {l['dur']:.1f}s")
        else:
            rot = l.get("texto") or "hesitação"
            cortar, motivo = eh_hesitacao(l, words[i], sempre, ch, limiar)
            # som alto que ninguem leu e que se decidiu NAO cortar: o rotulo
            # tem de gritar isso no revisao.txt, senao passa por hesitacao.
            if not cortar and not l.get("texto"):
                rot = f"?? {l['dur']:.1f}s"
            add("hesitacao", rot, l["s"], l["e"], cortar, motivo)

    n_cac = marca_cacoetes(stream, sempre, contextual)
    n_arr = marca_arrastadas(stream, sempre, C)
    if n_arr:
        print(f"[analyze] palavras arrastadas: {n_arr}")
    cadeias = detecta_tentativas(stream)
    n_tent = marca_tentativas(stream, cadeias)
    n_inst = marca_instrucoes(stream, carrega_marcadores(),
                              C["instrucao"]["respiro"])
    if n_inst:
        print(f"[analyze] marcadores verbais de corte: {n_inst} tokens "
              f"(ela pediu para cortar)")
    n_susp = resolve_suspeitos(stream, escuta, ch, sempre,
                               ch["densidade_minima"], db, hop, limiar)
    if n_susp:
        print(f"[analyze] trechos de duracao impossivel resolvidos: {n_susp}")
    # o `escuta` acima escreve _clip.wav a cada trecho, e `resolve_suspeitos`
    # chama de novo depois desta linha; apagar antes dela deixava o arquivo
    # para tras em toda pasta de trabalho.
    (d / "_clip.wav").unlink(missing_ok=True)
    n_eco = marca_ecos(stream)
    if n_eco:
        print(f"[analyze] retomadas colapsadas: {n_eco}")
    n_prot = protege_enumeracoes(stream, C["contagem"])
    anot = encaixa_anotacoes(stream, carrega_anotacoes(d))
    for a in anot:
        if a.get("ajustada"):
            print(f"[analyze] anotacao {a['ajustada']} encaixada em "
                  f"[{a['ini']:.2f}, {a['fim']:.2f}] (fronteira de palavra)")
    n_anot = aplica_anotacoes(stream, anot)
    print(f"[analyze] cacoetes marcados: {n_cac}")
    if n_prot:
        print(f"[analyze] contagens protegidas: {n_prot} tokens preservados")
    if anot:
        print(f"[analyze] anotacoes de julgamento: {len(anot)} trechos, "
              f"{n_anot} tokens novos")
    print(f"[analyze] cadeias de tentativa: {len(cadeias)} "
          f"({n_tent} tokens, {sum(c['dur'] for c in cadeias):.0f}s)")

    # `ini`/`fim` sao indices no fluxo, e `resolve_suspeitos` inseriu tokens
    # depois que eles foram calculados: gravados, apontariam para o lugar
    # errado. Ja foram usados (por `marca_tentativas`) e nao servem mais.
    jdump({"lacunas": lacunas, "stream": stream,
           "cadeias": [{k: v for k, v in c.items() if k not in ("ini", "fim")}
                       for c in cadeias],
           "anotacoes": anot, "dur": dur_total}, d / "stream.json")

    ncut = sum(1 for t in stream if t["cut"])
    tcut = sum(t["e"] - t["s"] for t in stream if t["cut"])
    rec = sum(1 for l in alvo if l.get("texto"))
    print(f"[analyze] fluxo com {len(stream)} tokens; "
          f"{ncut} marcados ({tcut:.0f}s = {100*tcut/dur_total:.0f}% do video)")
    print(f"[analyze] hesitacoes recuperadas com texto: {rec}/{len(alvo)}")
    print(f"[analyze] -> {d/'stream.json'}")

# -------------------------------------------------------------------- draft

CAB = """<!-- tarocut v1 | video={video} | tokens={n} | selo={selo} -->
<!--
COMO EDITAR ESTE ARQUIVO
  * Apague qualquer texto para cortar o trecho correspondente do video.
  * Os trechos {A}assim{F} ja estao marcados para corte (cacoetes,
    hesitacoes, pausas). Deixe como estao para que sejam cortados.
  * Para MANTER um trecho marcado, troque {A} {F} por {KA} {KF}.
  * Nao altere as linhas "## [b###]" - sao as ancoras de tempo.
  * Ao terminar:  python tarocut.py render {video}
-->

"""


def relatorio(st, cadeias, dur):
    """Resumo no topo: o que vai sair e por que. As cadeias de tentativa
    ficam listadas ANTES do texto, para nao serem descobertas tarde."""
    por = {}
    for t in st:
        if t["cut"]:
            k = t["why"].split()[0] if t["why"] else "outro"
            por[k] = por.get(k, 0.0) + (t["e"] - t["s"])
    tot = sum(por.values())
    L = ["## Resumo",
         "",
         f"- Duracao original: **{hhmmss(dur)}**",
         f"- Marcado para corte: **{hhmmss(tot)}** "
         f"({100*tot/dur:.0f}%) -> sobra ~{hhmmss(dur-tot)}",
         ""]
    for k, v in sorted(por.items(), key=lambda x: -x[1]):
        L.append(f"  - {k}: {v:.0f}s")
    L.append("")
    if cadeias:
        L += ["## Tentativas de fala detectadas",
              "",
              "Ela comecou, parou e refez. Cada linha mostra o que sera",
              "DESCARTADO; a versao final da frase e mantida.",
              ""]
        for i, c in enumerate(cadeias, 1):
            desc = c["descartado"][:150] + ("..." if len(c["descartado"]) > 150 else "")
            L.append(f"{i}. **{hhmmss(c['t_ini'])}** ({c['dur']:.1f}s) "
                     f"reencontrou `{c['repetido']}`")
            L.append(f"   descarta: _{desc}_")
        L.append("")
    L.append("---")
    return "\n".join(L)


def blocos(st):
    """Fatia o fluxo em blocos de transcricao: uma pausa longa fecha o
    bloco, e sem pausa ele fecha no tamanho maximo.

    As ancoras `[b###]` sao o nome publico de cada trecho - e por elas que
    se fala de um pedaco do video sem citar tempo. Fica fora do `cmd_draft`
    para que quem precisar da MESMA fatia (uma tela, um relatorio) nao a
    corte a seu modo e de dois nomes ao mesmo trecho."""
    out, i0, bid = [], 0, 1
    for i, t in enumerate(st):
        if (t["k"] == "silencio" and t["e"] - t["s"] > 1.5) or i - i0 > 89:
            out.append({"id": bid, "i0": i0, "i1": i + 1,
                        "ini": st[i0]["s"], "fim": t["e"]})
            i0, bid = i + 1, bid + 1
    if i0 < len(st):
        out.append({"id": bid, "i0": i0, "i1": len(st),
                    "ini": st[i0]["s"], "fim": st[-1]["e"]})
    return out


def texto_token(t):
    """Superficie de um token no transcript.md.

    O par de simbolos TEM que refletir t["cut"] em todos os tipos. Marcar
    toda pausa e toda hesitacao com ⟦ ⟧ fazia o round-trip reimpor o
    corte que a analise tinha decidido evitar - a revisao anulava a analise
    sem avisar."""
    A, F = (ABRE, FECHA) if t["cut"] else (KABRE, KFECHA)
    if t["k"] == "palavra":
        return f"{A}{t['t']}{F}" if t["cut"] else t["t"]
    if t["k"] == "silencio":
        return f"{A}pausa {t['e']-t['s']:.1f}s{F}"
    return f"{A}{t['t']} · {t['e']-t['s']:.1f}s{F}"


def cmd_draft(a):
    d = wdir(a.video)
    S = jload(d / "stream.json")
    st, cadeias, dur = S["stream"], S.get("cadeias", []), S["dur"]
    linhas = []
    for b in blocos(st):
        linhas.append(f"\n## [b{b['id']:03d}] {hhmmss(b['ini'])} → "
                      f"{hhmmss(b['fim'])}")
        linhas.append(" ".join(texto_token(t) for t in st[b["i0"]:b["i1"]]))

    out = d / "transcript.md"
    cab = CAB.format(video=Path(a.video).name, n=len(st),
                     selo=selo_stream(st),
                     A=ABRE, F=FECHA, KA=KABRE, KF=KFECHA)
    out.write_text(cab + relatorio(st, cadeias, dur) + "\n"
                   + "\n".join(linhas) + "\n", encoding="utf-8")
    print(f"[draft] -> {out}")

# ------------------------------------------------ round-trip da transcricao

TOKEN_RE = re.compile(r"⟦([^⟧]*)⟧|⟨([^⟩]*)⟩|([^\s⟦⟧⟨⟩]+)")
RE_PAUSA = re.compile(r"^pausa\s+([\d.]+)s$")


def chave_token(t):
    if t["k"] == "palavra":
        return chave(t["t"])
    dur = t["e"] - t["s"]
    return (f"§pausa{dur:.1f}" if t["k"] == "silencio" else f"§hes{dur:.1f}")


def _chave_superficie(conteudo):
    m = RE_PAUSA.match(conteudo.strip())
    if m:
        return f"§pausa{float(m.group(1)):.1f}"
    if " · " in conteudo:
        d = conteudo.rsplit("·", 1)[1].strip().rstrip("s")
        try:
            return f"§hes{float(d):.1f}"
        except ValueError:
            pass
    return chave(conteudo)


def parse_transcript(path):
    """Le o .md editado -> [(chave, estado)] com estado em
    cortar/manter/normal."""
    saida, dentro = [], False
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.startswith("## [b"):
            dentro = True
            continue
        if not dentro or ln.startswith(("#", "<!--", "-->", "---")):
            continue
        for m in TOKEN_RE.finditer(ln):
            cortar, manter, cru = m.groups()
            if cortar is not None:
                saida.append((_chave_superficie(cortar), "cortar"))
            elif manter is not None:
                saida.append((_chave_superficie(manter), "manter"))
            else:
                saida.append((chave(cru), "normal"))
    return saida


def selo_stream(stream):
    """Impressao digital do fluxo analisado.

    Contar tokens nao bastava: a analise pode mudar QUAIS trechos saem sem
    mudar QUANTOS tokens existem, e foi exatamente o que aconteceu - o
    render leu um transcript.md velho e reimpos decisoes ja corrigidas."""
    import hashlib
    crua = "|".join(f'{t["k"]}{t["s"]:.2f}{t["e"]:.2f}{int(t["cut"])}'
                    for t in stream)
    return hashlib.sha1(crua.encode("utf-8")).hexdigest()[:10]


def confere_atualidade(md, stream):
    """O transcript.md corresponde ao stream.json atual?

    Se a analise rodou de novo e o draft nao, o `reconcilia` reimpoe em
    silencio as decisoes antigas por cima das novas - foi assim que um corte
    ja consertado reapareceu no render. Melhor recusar e mandar rodar
    `draft` do que editar no escuro.

    CABECALHO SEM SELO TAMBEM E RECUSA. Aqui havia um `return True` para
    "arquivo antigo, sem selo", e ele custou uma anotacao: o transcript.md
    do IMG_0246 vinha de uma versao anterior do programa (382 tokens contra
    383), passava batido, e o `reconcilia` casava o "e" de 127,93s - que uma
    anotacao mandou cortar - com o "e" seguinte, que fica. Resultado: um
    trecho julgado por uma pessoa voltava ao video sem uma linha de aviso.
    Quarto caminho encontrado para o mesmo defeito. Nao se le cabecalho que
    nao da para conferir.

    O selo cobre o que a contagem de tokens nao cobria: a analise pode mudar
    QUAIS trechos saem sem mudar QUANTOS tokens existem."""
    m = re.search(r"selo=([0-9a-f]{10})", md.read_text(encoding="utf-8")[:400])
    if m and m.group(1) == selo_stream(stream):
        return True
    if not m:
        print("[aviso] o transcript.md nao tem selo: e de uma versao "
              "anterior do programa e nao da para conferir contra a analise "
              "de agora.")
        print("[aviso] rode 'draft' antes (as marcas ⟨ ⟩ que voce tiver "
              "posto a mao se perdem; copie o arquivo se quiser guarda-las).")
        return False
    print("[aviso] o transcript.md nao corresponde ao stream.json atual: "
          "a analise rodou de novo depois dele.")
    print("[aviso] rode 'draft' antes, ou as edicoes antigas vao valer.")
    return False


def reconcilia(stream, editado):
    """Alinha o fluxo original com o texto editado. O que sumiu do texto
    vira corte; o que voltou para ⟨ ⟩ vira manutencao."""
    import difflib
    ko = [chave_token(t) for t in stream]
    ke = [k for k, _ in editado]
    sm = difflib.SequenceMatcher(None, ko, ke, autojunk=False)
    visto = set()
    for i, j, n in sm.get_matching_blocks():
        for k in range(n):
            estado = editado[j + k][1]
            t = stream[i + k]
            visto.add(i + k)
            if estado == "manter":
                t["cut"], t["why"] = False, "resgatado"
            elif estado == "cortar":
                t["cut"] = True
            else:
                t["cut"], t["why"] = False, ""
    apagados = 0
    for i, t in enumerate(stream):
        if i not in visto:
            t["cut"], t["why"] = True, "apagado no texto"
            apagados += 1
    return apagados

# ----------------------------------------------- camada do editor (montagem)

def _spans(v, ordena=True):
    """Normaliza uma lista de [ini, fim]: descarta lixo, ordena e funde o
    que se toca. Sem isso, mil ajustes de 1 ms viram mil trechos no arquivo.

    LARGURA ZERO NAO E GESTO. `b > a` deixava passar `[37.19, 37.19+5e-15]`
    - ruido de ponto flutuante, nao clique de ninguem. E `aplica_manual`
    parte o trecho mantido em cada corte que recebe, entao cinco desses no
    edicao.json do IMG_0246 estilhacaram "Os Arcanos" em cinco cacos de
    0,10s, cada um com fade de 10 ms na entrada e na saida: metralhadora.
    1 ms e o mesmo limite que `aplica_manual` usa na saida - a 48 kHz sao
    48 amostras, e ninguem clica isso."""
    out = []
    for x in v or []:
        try:
            a, b = float(x[0]), float(x[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if b - a > 0.001:
            out.append([a, b])
    if ordena:
        out.sort()
    fund = []
    for a, b in out:
        if fund and a <= fund[-1][1] + 1e-9:
            fund[-1][1] = max(fund[-1][1], b)
        else:
            fund.append([a, b])
    return fund


def uniao_spans(A, B):
    return _spans(list(A or []) + list(B or []))


def subtrai_spans(A, B):
    """A menos B.

    E o que mantem `cortes` e `restauros` DISJUNTOS dentro do montagem.json:
    cada gesto tira do conjunto contrario antes de entrar no seu. Essa
    disjuncao e o motivo de o botao de mais desfazer o de menos - com os
    dois conjuntos se sobrepondo, `aplica_manual` teria de escolher um
    vencedor, e escolheria sempre o mesmo."""
    out = []
    for a, b in _spans(A):
        pedacos = [[a, b]]
        for x, y in _spans(B):
            novo = []
            for p, q in pedacos:
                if y <= p or x >= q:
                    novo.append([p, q])
                    continue
                if p < x:
                    novo.append([p, x])
                if y < q:
                    novo.append([y, q])
            pedacos = novo
        out += [c for c in pedacos if c[1] - c[0] > 0.001]
    return _spans(out)


def lacunas(iv, dur):
    """O complemento de `iv`: o que NAO se ouve.

    O editor raciocina em lacuna, nao em trecho mantido - os botoes de mais
    e menos movem a borda da lacuna colada na selecao, e e so isso que eles
    fazem. Mora aqui, e nao no navegador, pela mesma regra do resto: quem
    responde onde a lamina esta e quem calculou o corte."""
    out, t = [], 0.0
    for a, b in iv:
        if a > t + 1e-9:
            out.append([t, a])
        t = max(t, b)
    if t < dur - 1e-9:
        out.append([t, dur])
    return out


def carrega_montagem(d):
    """work/<video>/montagem.json - o que o editor de transcricao decidiu.

    Duas camadas, e a diferenca entre elas e o ponto todo do arquivo.

    `tokens` sao decisoes de PALAVRA, do mesmo tipo que apagar texto no
    transcript.md: passam por toda a geometria de emenda (fusao, snap, corte
    minimo, respeito a palavra vizinha), porque quem oculta uma palavra quer
    a palavra fora, nao um numero exato de milissegundos.

    `cortes` e `restauros` sao trechos de TEMPO, os botoes de mais e menos.
    Entram DEPOIS de tudo, sem snap e sem minimo: um ajuste de 1 ms nao
    sobreviveria ao filtro de corte minimo, e nao deve mesmo sobreviver a
    ele - quem pediu 1 ms esta olhando a palavra e quer 1 ms.

    NAO se chama `edicao.json` de proposito. Sobraram `edicao.json` em
    `work/` do editor removido em 01/09, com outro formato e decisoes de
    outra epoca; passar a ler aqueles arquivos seria reimpor decisao velha
    em silencio, que e o defeito que este programa mais teve."""
    p = d / "montagem.json"
    if not p.exists():
        return None
    try:
        m = jload(p)
    except Exception as e:
        print(f"[montagem] montagem.json ilegivel ({e}); ignorando")
        return None
    if not isinstance(m, dict):
        return None
    m["tokens"] = [t for t in (m.get("tokens") or []) if isinstance(t, dict)]
    m["cortes"] = _spans(m.get("cortes"))
    m["restauros"] = _spans(m.get("restauros"))
    return m


def confere_montagem(stream, m):
    """As decisoes de palavra ainda apontam para a palavra em que foram
    tomadas? Devolve a lista de problemas, vazia quando esta tudo no lugar.

    Indice de token caduca: basta a analise inserir um token antes e a
    decisao passa a valer para outra palavra - em silencio, que e como
    julgamento humano some neste programa. Por isso cada decisao guarda
    tambem o `s`/`e` do token em que foi tomada: ou ela cai na palavra
    certa, ou o programa para e diz qual e. E a mesma regra do selo do
    transcript.md, so que por palavra, e por isso o remedio tambem e o
    mesmo - recusa, nao aviso."""
    ruins = []
    for dec in (m or {}).get("tokens") or []:
        i = dec.get("i")
        t = stream[i] if isinstance(i, int) and 0 <= i < len(stream) else None
        if t is None:
            ruins.append(f"token {i} nao existe mais "
                         f"({dec.get('t','')!r}, {dec.get('estado','')})")
        elif (abs(t["s"] - dec.get("s", -9)) > 0.01
              or abs(t["e"] - dec.get("e", -9)) > 0.01):
            ruins.append(f"token {i} mudou de lugar: era {dec.get('t','')!r} "
                         f"em {dec.get('s', 0):.2f}s, hoje e {t['t']!r} em "
                         f"{t['s']:.2f}s")
    return ruins


def aplica_montagem(stream, m, rotulo):
    """Camada de palavra. Vence o transcript.md por ser a decisao mais
    recente e a mais explicita - no editor a pessoa clicou naquela palavra;
    no .md ela pode so ter deixado como veio."""
    ruins = confere_montagem(stream, m)
    if ruins:
        print(f"[{rotulo}] o montagem.json e de outra analise:")
        for r in ruins[:6]:
            print(f"    {r}")
        sys.exit(f"[{rotulo}] abortado: as decisoes de palavra do editor "
                 f"cairiam na palavra errada. Abra o editor e decida de "
                 f"novo, ou apague o montagem.json.")
    n = 0
    for dec in m.get("tokens") or []:
        t = stream[dec["i"]]
        novo = (dec.get("estado") == "cortar")
        if t["cut"] != novo:
            n += 1
        t["cut"] = novo
        # `prot` protege enumeracao contra corte AUTOMATICO; nao autoriza
        # sobreviver a um clique. Mesma regra do `aplica_anotacoes`: se uma
        # pessoa decidiu, sai.
        t["prot"] = False
        t["why"] = "oculto no editor" if novo else "restaurado no editor"
    return n


def carrega_correcoes(d):
    """work/<video>/correcoes.json - o que a pessoa corrigiu no TEXTO de
    uma palavra, no editor, para a legenda que uma etapa futura vai gerar.

    Nao e o `montagem.json`: aquele decide se a palavra sai do video; isto
    decide o que ela DIZ. Sao perguntas diferentes, e por isso e um arquivo
    a parte - misturar as duas faria o mesmo campo responder duas coisas,
    que e como uma das duas comeca a ficar esquecida."""
    p = d / "correcoes.json"
    if not p.exists():
        return None
    try:
        c = jload(p)
    except Exception as e:
        print(f"[correcoes] correcoes.json ilegivel ({e}); ignorando")
        return None
    if not isinstance(c, dict):
        return None
    c["itens"] = [x for x in (c.get("itens") or []) if isinstance(x, dict)]
    return c


def confere_correcoes(stream, c):
    """As correcoes de texto ainda apontam para a palavra em que foram
    tomadas? Mesma pergunta do `confere_montagem`, pela mesma razao:
    indice de token caduca quando a analise roda de novo; `s`/`e` do
    token em que a correcao foi feita, nao."""
    ruins = []
    for dec in (c or {}).get("itens") or []:
        i = dec.get("i")
        t = stream[i] if isinstance(i, int) and 0 <= i < len(stream) else None
        if t is None:
            ruins.append(f"token {i} nao existe mais "
                         f"({dec.get('orig','')!r} -> {dec.get('novo','')!r})")
        elif (abs(t["s"] - dec.get("s", -9)) > 0.01
              or abs(t["e"] - dec.get("e", -9)) > 0.01):
            ruins.append(f"token {i} mudou de lugar: a correcao era de "
                         f"{dec.get('orig','')!r} em {dec.get('s', 0):.2f}s, "
                         f"hoje ai esta {t['t']!r} em {t['s']:.2f}s")
    return ruins


def aplica_correcoes(stream, c, rotulo):
    """So troca `t["t"]`. Nunca `cut`, nunca `s`/`e` - texto corrigido nao
    e decisao de corte, e misturar as duas faria uma correcao de
    ortografia acender os alarmes que existem para julgamento de
    conteudo."""
    ruins = confere_correcoes(stream, c)
    if ruins:
        print(f"[{rotulo}] o correcoes.json e de outra analise:")
        for r in ruins[:6]:
            print(f"    {r}")
        sys.exit(f"[{rotulo}] abortado: as correcoes de texto do editor "
                 f"cairiam na palavra errada. Abra o editor e decida de "
                 f"novo, ou apague o correcoes.json.")
    for dec in c.get("itens") or []:
        stream[dec["i"]]["t"] = dec["novo"]


def spans_da_mao(stream, m, folga=0.0):
    """Os trechos que a PESSOA mandou ficar no editor: os restauros de
    milissegundo mais o tempo das palavras marcadas para manter.

    Serve para uma coisa so: separar "a anotacao nao foi obedecida" - bug, e
    o pior deste programa - de "eu reabri essa anotacao de proposito, depois
    de ouvir". Sem essa separacao todo ajuste feito no editor acenderia o
    alarme que existe para avisar que o julgamento sumiu, e alarme que
    acende sozinho vira papel de parede.

    A palavra entra alargada de `folga_palavra`, que e EXATAMENTE a regiao
    que o `_respeita_palavras` defende em volta dela. Sem isso a conta nao
    fechava: reabrir o `Yala,` de 13,10-13,52 dentro da anotacao de 0-32,45
    devolvia 0,54s ao video (a palavra mais 0,06 de cada lado), mas so 0,42
    contavam como decisao da pessoa, e o `conferir` gritava NAO OBEDECIDA
    numa anotacao que eu mesmo tinha acabado de reabrir."""
    if not m:
        return []
    fora = [list(x) for x in (m.get("restauros") or [])]
    for dec in m.get("tokens") or []:
        if dec.get("estado") != "manter":
            continue
        try:
            a, b = float(dec["s"]), float(dec["e"])
        except (KeyError, TypeError, ValueError):
            continue
        i = dec.get("i")
        if isinstance(i, int) and 0 <= i < len(stream):
            t = stream[i]
            if abs(t["s"] - a) < 0.01:
                b = fim_util(t)      # cauda podada nao e palavra mantida
        fora.append([a - folga, b + folga])
    return _spans(fora)


def aplica_manual(manter, m, dur):
    """Camada de tempo, aplicada por ultimo e sem arredondar nada.

    Restaura primeiro e corta depois. O editor mantem `cortes` e `restauros`
    disjuntos (ver `subtrai_spans`), entao esta ordem nao decide nada; ela so
    fixa o resultado de um arquivo editado a mao em que os dois se cruzem.

    Nada aqui passa por snap, fusao ou minimo - seria desfazer, no escuro, o
    ajuste de milissegundo que a pessoa fez de proposito."""
    if not m:
        return manter
    iv = [[a, b] for a, b in manter]
    for a, b in m.get("restauros") or []:
        iv.append([max(0.0, a), min(dur, b)])
    iv = subtrai_spans(iv, m.get("cortes") or [])
    return [(round(a, 4), round(b, 4)) for a, b in iv if b - a > 0.001]


# -------------------------------------------------------- montagem do corte

def fim_util(t):
    """Ate onde esta palavra e palavra.

    Quando `marca_arrastadas` decide podar a cauda, o que vem depois de
    `cauda[0]` e arrasto - foi essa a decisao. Quem defende palavra mantida
    tem que parar aqui, senao defende tambem o arrasto que se mandou tirar.

    MEDIDO: sem isto, a poda de cauda nunca aconteceu uma vez sequer. Nos
    videos de calibracao eram 10 caudas marcadas (o "Aaas" de 80,0s do
    IMG_0247, os "Eeee" de 44,3s, 216,1s e 481,8s) e 10 desfeitas pela trava
    final - ela via o fim do token, 1,1s adiante, e empurrava a emenda para
    fora da palavra inteira."""
    cauda = t.get("cauda")
    return cauda[0] if cauda else t["e"]


def _tem_fala(stream, a, b):
    """Ha palavra aproveitavel dentro de [a,b]?

    Aproveitavel vai ate `fim_util`: arrasto que se mandou podar nao e
    fala, senao ele segura a fusao dos cortes em volta. MEDIDO: nos tres
    videos de calibracao esta linha nao muda nenhuma emenda - esta assim
    para "ha fala aqui?" ter uma resposta so, aqui e no `_garante_ilhas`."""
    return any(t["k"] == "palavra" and not t["cut"]
               and fim_util(t) > a + 0.02 and t["s"] < b - 0.02
               for t in stream)


def spans_corte(stream, C, anot=None, dur=None):
    """Fluxo -> trechos a REMOVER.

    A versao antiga montava o video somando os pedacos bons, e por isso
    perdia todo o intervalo natural entre palavras: virava um corte a cada
    duas palavras. Aqui e o contrario - parte-se do video inteiro e
    subtrai-se so o que foi marcado. Tudo o que ninguem mandou cortar
    continua contínuo.

    E cada pausa encurtada gera UMA emenda, nao duas: remove-se o MIOLO da
    pausa e deixa-se metade do respiro colado em cada fala vizinha. Antes eu
    guardava uma lasca do meio da pausa, o que punha na tela 0,45s da cabeca
    dela numa posicao sem relacao com nenhum dos lados."""
    cs = C["silencio"]
    alvo = cs["pausa_alvo"]
    out = []
    n = len(stream)

    # 1. RUNS de tokens marcados. Trabalhar por run, e nao token a token,
    #    e o que impede sobrar caco: a borda do corte fica na fronteira
    #    entre o que fica e o que sai, uma vez so por run.
    i = 0
    while i < n:
        t = stream[i]
        if not t["cut"] or t.get("prot"):
            i += 1
            continue
        j = i
        while j + 1 < n and stream[j + 1]["cut"] and not stream[j + 1].get("prot"):
            j += 1
        ini, fim = stream[i], stream[j]

        if ini["k"] == "silencio" and ini["why"] == "abertura":
            a = 0.0
        elif ini["k"] == "silencio":
            a = ini["s"] + alvo / 2
        else:
            a = ini["s"]
        b = fim["e"] - alvo / 2 if fim["k"] == "silencio" else fim["e"]

        # LIMITES: a emenda nao pode passar do fim da palavra mantida
        # anterior nem do inicio da proxima. Sem isso o snap engolia a
        # palavra vizinha inteira ("momen--" + "meira versao").
        la = stream[i - 1]["e"] if i > 0 else 0.0
        lb = stream[j + 1]["s"] if j + 1 < n else (dur or b)
        if b > a:
            out.append([a, b, la, lb])
        i = j + 1

    # 2. Silencio final: depois da ultima palavra nao ha token nenhum, entao
    #    marcar tokens nunca alcanca esse trecho. Era por aqui que passavam
    #    os 15s de ela em silencio, olhando para os lados e saindo de quadro.
    if dur:
        ult = max((t["e"] for t in stream if t["k"] == "palavra"
                   and not t["cut"]), default=None)
        if ult is not None and dur - ult > cs["pausa_intocavel"]:
            out.append([ult + alvo, dur])

    # 3. Caudas de palavra arrastada: a palavra fica, o arrasto sai.
    #    O limite e a propria palavra: a cauda nunca invade a vizinha.
    #    `prot` NAO conta aqui, e isso e proposital: proteger contagem e
    #    impedir que a sequencia seja CORTADA, nao autorizar arrasto. O
    #    "Aaas" de 80,0s do IMG_0247 sobrou inteiro porque uma contagem
    #    inexistente cancelava a poda - em silencio, sem aparecer em lugar
    #    nenhum. Com o RE_NUM corrigido nenhum token protegido dos tres
    #    videos tem cauda, entao hoje isto nao muda uma emenda sequer; fica
    #    para o dia em que a contagem de verdade vier arrastada.
    for k, t in enumerate(stream):
        if t.get("cauda") and not t["cut"]:
            ca, cb = t["cauda"]
            prox = stream[k + 1]["s"] if k + 1 < n else (dur or cb)
            out.append([ca, cb, t["s"], min(cb, prox)])

    # 4. Anotacoes de julgamento entram como TRECHO DE TEMPO, nao como
    #    marcacao de token: valem mesmo onde ninguem fala.
    for a in (anot or []):
        out.append([a["ini"], a["fim"], a["ini"], a["fim"]])

    return out


def _funde(cortes, stream, limiar):
    """Junta cortes vizinhos quando o que sobra entre eles seria uma
    piscada - mas nunca engole fala aproveitavel."""
    cortes = sorted((list(c) for c in cortes if c[1] > c[0]),
                    key=lambda c: c[0])
    fund = []
    for c in cortes:
        a, b = c[0], c[1]
        la = c[2] if len(c) > 2 else a
        lb = c[3] if len(c) > 3 else b
        if fund and a - fund[-1][1] < limiar and not _tem_fala(
                stream, fund[-1][1], a):
            fund[-1][1] = max(fund[-1][1], b)
            fund[-1][3] = max(fund[-1][3], lb)
        else:
            fund.append([a, b, la, lb])
    return fund


def _snap(cortes, db, hop, raio):
    """Pousa cada borda da emenda no instante mais silencioso por perto.

    SIMETRICO. O ponto de menor energia perto da fronteira E o ponto de
    emenda correto - ali nao ha fonema para decepar dos dois lados.

    Ja tentei assimetrico (inicio so para frente, fim so para tras) para
    proteger o "s" final. Deu errado: o inicio andando para frente passou a
    deixar o comeco da palavra cortada audivel ("as rainhas vem depois,
    as--"). Deixar a busca escolher livremente resolve os dois: se o minimo
    esta antes da fronteira, o que se perde do lado mantido e silencio; se
    esta depois, o que sobra do lado cortado tambem e."""
    if db is None or raio <= 0:
        return cortes
    n = len(db)

    def melhor(t, lo, hi):
        lo, hi = max(0.0, lo), max(0.0, hi)
        if hi <= lo:
            return t
        i0, i1 = max(0, int(lo / hop)), min(n, int(hi / hop) + 1)
        return (i0 + int(db[i0:i1].argmin())) * hop if i1 > i0 else t

    saida = []
    for c in cortes:
        a, b = c[0], c[1]
        la = c[2] if len(c) > 2 else a - raio
        lb = c[3] if len(c) > 3 else b + raio
        meio = (a + b) / 2
        # a busca fica presa entre as palavras mantidas vizinhas e nunca
        # ultrapassa o meio do proprio corte
        na = melhor(a, max(a - raio, la), min(a + raio, meio))
        nb = melhor(b, max(b - raio, meio), min(b + raio, lb))
        saida.append([na, nb, la, lb] if nb > na else list(c))
    return saida


def _respeita_palavras(cortes, stream, C):
    """Rede de seguranca final: nenhuma emenda encosta em palavra mantida.

    As etapas anteriores ja tentam isso cada uma a seu modo, e mesmo assim
    escapou "realmente" comecando cortado. Aqui a regra e imposta uma vez so,
    no fim, sobre o resultado de todas elas.

    A regra e SUBTRAIR a palavra do corte, e por isso um corte pode sair
    daqui PARTIDO EM DOIS. Antes ele so sabia encolher para um lado, e
    quando a palavra mantida caia no MEIO do corte o lado de la sobrevivia
    inteiro.

    MEDIDO no IMG_0247: o token "Mae! Mais do..." (49.806-53.078, ela
    falando com outra pessoa) atravessa a palavra "do" (50.640-50.896). O
    corte travava em 50.620 e 2,33s de fala alheia continuavam tocando
    dentro do trecho mantido. Pior: havia uma ANOTACAO de julgamento
    (50.90-53.23) pedindo exatamente aquele trecho - o `_funde` a tinha
    juntado ao token, entao ela morreu junto, sem aparecer em lugar nenhum.
    Era o caminho pelo qual uma decisao humana sumia em silencio."""
    folga = C["corte"]["folga_palavra"]
    pal = [t for t in stream
           if t["k"] == "palavra" and not t["cut"] and not t.get("prot")]
    saida = []
    for c in cortes:
        pedacos = [[c[0], c[1]]]
        for t in pal:
            pa, pb = t["s"] - folga, fim_util(t) + folga
            novo = []
            for a, b in pedacos:
                if pb <= a or pa >= b:
                    novo.append([a, b])
                    continue
                if a < pa:
                    novo.append([a, pa])
                if pb < b:
                    novo.append([pb, b])
            pedacos = novo
        for a, b in pedacos:
            if b - a > 0.05:
                saida.append([a, b] + list(c[2:]))
    return saida


def _garante_ilhas(cortes, minimo, stream):
    """Junta cortes separados por um vao SEM FALA, para nao sobrar piscada.

    Esta trava ja teve um segundo comportamento - alargar a ilha encolhendo
    os cortes vizinhos - que foi removido. Alargar era DESCORTAR material
    que se decidiu cortar, e foi assim que ela comeu 0,09s de
    "metodologias" e entregou "as met--". Se a ilha tem palavra de verdade
    dentro, 0,4s esta bom: e uma palavra, nao um piscar.

    As causas originais do glitch (lasca do meio da pausa, micro-corte de
    0,08s entre palavras) foram resolvidas na estrutura e no alinhamento.
    Sobrou so o caso visual: vao curto e vazio entre dois cortes."""
    cortes = sorted((list(c) for c in cortes), key=lambda c: c[0])
    saida = []
    for c in cortes:
        if not saida:
            saida.append(c)
            continue
        ant = saida[-1]
        folga = c[0] - ant[1]
        if folga <= 0:
            ant[1] = max(ant[1], c[1])
            continue
        # `fim_util` pela mesma razao do `_tem_fala`: a ilha pode conter so
        # a cabeca de uma palavra podada, e e a cabeca que decide.
        mantidas = [t for t in stream if t["k"] == "palavra" and not t["cut"]
                    and fim_util(t) > ant[1] + 0.02 and t["s"] < c[0] - 0.02]
        if folga < minimo and not mantidas:
            ant[1] = max(ant[1], c[1])       # ilha vazia: absorve
            if len(ant) > 3 and len(c) > 3:
                ant[3] = max(ant[3], c[3])
            continue
        saida.append(c)
    return [c for c in saida if c[1] > c[0]]


def intervalos(stream, C, dur, db=None, hop=0.02, anot=None,
               manual=None):
    """Fluxo -> lista de trechos a MANTER."""
    cc = C["corte"]

    # `minimo` existe contra emenda invisivel: economizar 0,2s nao paga um
    # pulo na tela. Mas ele nao tem o que dizer sobre um trecho que uma
    # PESSOA marcou para sair: desfazer no escuro o corte que alguem pediu
    # olhando o texto e o mesmo erro que desfazer o ajuste de quem olha a
    # onda.
    #
    # MEDIDO: um "ééé" de 0,35s anotado vira corte de 0,35s, perde 0,06 de
    # folga de cada lado e sai com 0,23 - abaixo de 0,28. A anotacao era
    # lida, aplicada, e descartada aqui sem uma linha de aviso. Terceiro
    # caminho encontrado pelo qual julgamento humano sumia em silencio.
    faixas = [(x["ini"], x["fim"]) for x in (anot or [])]

    def passa(c):
        if c[1] - c[0] >= cc["minimo"]:
            return True
        return any(min(c[1], y) - max(c[0], x) > 0.05 for x, y in faixas)

    cortes = spans_corte(stream, C, anot, dur)
    cortes = _funde(cortes, stream, cc["ilha_minima"])
    cortes = [c for c in cortes if passa(c)]
    cortes = _snap(cortes, db, hop, cc["snap"])
    cortes = _funde(cortes, stream, cc["ilha_minima"])
    # o snap desloca as bordas e pode encolher um corte abaixo do minimo,
    # entao filtra de novo: nenhuma emenda invisivel escapa.
    cortes = [c for c in cortes if passa(c)]
    cortes = _garante_ilhas(cortes, cc["ilha_minima"], stream)
    cortes = _respeita_palavras(cortes, stream, C)
    cortes = [c for c in cortes if passa(c)]

    # Cola nas pontas: um corte que comeca quase no zero tem que comecar NO
    # zero, senao sobra uma lasca de milissegundos antes dele - foi assim
    # que voltou um trecho de 0,08s no inicio do IMG_0248. Idem no fim.
    if cortes:
        if cortes[0][0] < cc["ilha_minima"]:
            cortes[0][0] = 0.0
        if dur - cortes[-1][1] < cc["ilha_minima"]:
            cortes[-1][1] = dur

    manter, t = [], 0.0
    for c in cortes:
        a, b = max(0.0, c[0]), min(dur, c[1])
        if a > t + 0.02:
            manter.append([t, a])
        t = max(t, b)
    if t < dur - 0.02:
        manter.append([t, dur])
    iv = [(round(a, 3), round(b, 3)) for a, b in manter if b - a > 0.05]
    return aplica_manual(iv, manual, dur)


def trecho_audio(i, a, b, fade):
    """Um trecho de audio aparado, com o micro-fade das duas pontas.

    Mora aqui porque o `audio` e o `render` tem de emendar do mesmo jeito: a
    previa em .m4a so vale como julgamento enquanto for exatamente o que o
    .mov vai receber. Eram duas copias identicas desta linha, uma em cada
    comando - o tipo de par que so se descobre desalinhado tarde, ouvindo."""
    return (f"[0:a]atrim=start={a}:end={b},asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d={fade},"
            f"afade=t=out:st={max(0, b - a - fade):.3f}:d={fade}[a{i}];")


def _grafo_corte(iv, C):
    """As linhas de filter_complex que cortam e concatenam - trim de cada
    trecho mantido, emenda em [vc]/[ao]. Extraida de grafo() para
    `grafo_cartas()` (ver 'adicionar cartas' no CLAUDE.md) cortar
    EXATAMENTE igual: as duas nunca podem discordar de onde cai a
    emenda, e ter dois lugares que montam isso e como ter dois lugares
    que decidem [corte] - so se descobre desalinhado tarde, ouvindo."""
    fade = C["audio"]["fade"]
    L, vs, as_ = [], [], []
    for i, (a, b) in enumerate(iv):
        L.append(f"[0:v]trim=start={a}:end={b},setpts=PTS-STARTPTS[v{i}];")
        L.append(trecho_audio(i, a, b, fade))
        vs.append(f"[v{i}]")
        as_.append(f"[a{i}]")
    pares = "".join(v + a for v, a in zip(vs, as_))
    L.append(f"{pares}concat=n={len(iv)}:v=1:a=1[vc][ao];")
    return L


def grafo(iv, C, escala=None, fps=None):
    L = _grafo_corte(iv, C)
    filtros = []
    if escala:
        filtros.append(f"scale=-2:{escala}")
    if fps:
        filtros.append(f"fps={fps}")
    filtros.append("null")
    L.append("[vc]" + ",".join(filtros) + "[vo]")
    return "\n".join(L)


def _janela_layout3(resumo, margem):
    """Janela de crop (fracao 0-1 do quadro original) que cobre o vaivem
    MEDIDO do rosto (rosto.json/resumo.cx_min..cy_max) com folga, para o
    layout 3 (Mariana centralizada, ver overview da feature). Cortar na
    fracao que o EIXO MAIS largo precisa, nos dois eixos, preserva o
    aspecto 16:9 sem distorcer ao escalar de volta: quando o crop tem o
    mesmo aspecto do quadro inteiro, a fracao de largura cortada e igual
    a fracao de altura cortada."""
    cx0, cx1 = resumo.get("cx_min", 0.4), resumo.get("cx_max", 0.6)
    cy0, cy1 = resumo.get("cy_min", 0.3), resumo.get("cy_max", 0.5)
    l = min(1.0, max(cx1 - cx0, cy1 - cy0) + 2 * margem)
    x = min(max(0.0, (cx0 + cx1) / 2 - l / 2), 1 - l)
    y = min(max(0.0, (cy0 + cy1) / 2 - l / 2), 1 - l)
    return x, y, l, l


def grafo_cartas(iv, C, cartas, resumo_rosto, w_fonte, h_fonte,
                  secundarias_imgs, escala=None, fps=None, hw=False):
    """Estende _grafo_corte com o overlay das cartas e o reenquadramento
    da Mariana - so os layouts 1 e 3 (ver escopo combinado no plano:
    zoom e empilhamento ficam de fora por enquanto).

    Geometria (posicao, tamanho) e a MESMA para toda carta, principal ou
    secundaria - so O QUE aparece e QUANDO mudam. Inputs do ffmpeg, por
    convencao de quem chama isto: 0=video, 1=imagem da carta principal,
    2+=uma por carta secundaria DISTINTA, na ordem de `secundarias_imgs`
    (lista de (nome, Path), sem repetir carta).

    Reenquadramento: um `crop` so, com w/h/x/y como expressao de `t` (o
    ffmpeg 9.x reavalia sozinho quadro a quadro - versoes antigas exigiam
    `eval=frame`, que sumiu da lista de opcoes) que alterna entre o
    quadro inteiro (layout 1) e a
    janela de `_janela_layout3` (layout 3) - troca dura, sem pan suave,
    porque so muda quando uma carta secundaria entra ou sai, e isso ja
    tem o fade dela para disfarcar o corte.

    Fade: so a carta PRINCIPAL tem fade suave de entrada (`format=yuva420p`
    + `fade=alpha=1`, uma vez, nunca sai). A secundaria liga/desliga com
    `overlay=...:enable=` (corte seco) - decisao deliberada do checkpoint
    4: primeiro provar o mecanismo (aparece a hora certa, some a hora
    certa), fade macio da secundaria fica para quando isso estiver
    aprovado, junto com a moldura/sombra/legenda que tambem ficaram de
    fora (ver escopo combinado no plano).

    `hw=True`: o [0:v] vem de decode QSV (`-hwaccel qsv` na chamada do
    ffmpeg) e continua em memoria de GPU ate aqui - `crop`/`overlay` nao
    entendem isso, entao esta funcao baixa para memoria normal ANTES do
    primeiro filtro que precisar de pixel de verdade. MEDIDO no
    IMG_0301: sem isto (decode em software) a previa lo-res levava mais
    de 30 min; com isto, minutos. Chamar com `hw` errado do que a chamada
    do ffmpeg realmente usa e erro de pixel format, nao resultado errado
    - por isso `cmd_cartas` gera os dois grafos e usa o que combina com
    cada tentativa (qsv primeiro, software se aquele falhar)."""
    cc = C["cartas"]
    L = _grafo_corte(iv, C)

    # Escalar ANTES do crop/overlay quando for previa: crop+overlay+fade
    # em 3840x2160 nativo, so para escalar para 480p no final, custa muito
    # mais filtragem que fazer a mesma conta ja pequena. Recalcular a
    # geometria da carta e do crop na resolucao de trabalho e a mesma
    # conta de _janela_layout3, so que sobre w_trab/h_trab em vez de
    # w_fonte/h_fonte. Sem `escala` (bake em qualidade final), trabalha
    # direto no nativo, como antes.
    #
    # Com `hw`, o download tem que vir DEPOIS do scale, nao antes: baixar
    # cada quadro em 3840x2160 (p010le, ~24MB/quadro) so para escalar para
    # 480p em seguida MEDIU quase 14 minutos no IMG_0301 - e so
    # transferencia de GPU para CPU de pixel que ia ser jogado fora.
    # `scale_qsv` escala ainda em memoria de GPU; so entao `hwdownload`
    # baixa o quadro ja pequeno. MEDIDO: um trecho de 20s que levava ~15s
    # nao mudou de tempo sensivelmente - a diferenca aparece no video
    # inteiro, onde e a diferenca entre ~14min e ~1min.
    ult_base = "vc"
    w_trab, h_trab = w_fonte, h_fonte
    if escala:
        h_trab = escala
        w_trab = round(w_fonte * escala / h_fonte / 2) * 2
        if hw:
            L.append(f"[vc]scale_qsv=w={w_trab}:h={h_trab},"
                      f"hwdownload,format=p010le" +
                      (f",fps={fps}" if fps else "") + "[vs];")
        else:
            L.append(f"[vc]scale={w_trab}:{h_trab}" +
                      (f",fps={fps}" if fps else "") + "[vs];")
        ult_base = "vs"
    elif hw:
        L.append("[vc]hwdownload,format=p010le[vd];")
        ult_base = "vd"

    x, y, lw, lh = _janela_layout3(resumo_rosto, cc["crop_margem"])
    secs = cartas["secundarias"]

    def _janela_ativa(spans):
        # `between(t\,a\,b)`: a virgula precisa vir escapada mesmo dentro
        # de aspas - e assim que todo exemplo de expressao condicional do
        # ffmpeg com mais de um argumento escreve (o parser do filtro
        # separa por virgula ANTES do eval da expressao entender que ali
        # e argumento de funcao, nao fim de opcao).
        return "+".join(f"between(t\\,{s['ini']:.3f}\\,{s['fim']:.3f})"
                         for s in spans)

    ativo = _janela_ativa(secs) if secs else "0"
    cw, ch = lw * w_trab, lh * h_trab
    cx0, cy0 = x * w_trab, y * h_trab
    L.append(
        f"[{ult_base}]crop=w='if({ativo},{cw:.1f},{w_trab})':"
        f"h='if({ativo},{ch:.1f},{h_trab})':"
        f"x='if({ativo},{cx0:.1f},0)':y='if({ativo},{cy0:.1f},0)'[base];"
    )

    altura_px = max(2, int(h_trab * cc["carta_altura"]))
    margem_px = int(w_trab * cc["carta_margem"])
    y_carta = (h_trab - altura_px) // 2
    fade = cc["fade"]

    L.append(f"[1:v]scale=-2:{altura_px},format=yuva420p,"
             f"fade=t=in:st=0:d={fade}:alpha=1[cartaP];")
    L.append(f"[base][cartaP]overlay=x={margem_px}:y={y_carta}[c0];")

    ult = "c0"
    for i, (nome, _) in enumerate(secundarias_imgs, start=2):
        janela = _janela_ativa([s for s in secs if s["carta"] == nome])
        prox = f"c{i - 1}"
        L.append(f"[{i}:v]scale=-2:{altura_px}[carta{i}];")
        L.append(f"[{ult}][carta{i}]overlay=x=W-{margem_px}-w:"
                  f"y={y_carta}:enable='{janela}'[{prox}];")
        ult = prox

    # escala/fps ja foram aplicados cedo (antes do crop) quando pedidos -
    # so falta rotular a saida.
    L.append(f"[{ult}]null[vo]")
    return "\n".join(L)


def _base(video, rotulo, ignorar_transcript=False, aplicar_mont=True,
          aplicar_corr=True):
    """Tudo o que precede o calculo dos trechos: le o fluxo analisado e
    empilha por cima dele as decisoes humanas.

    Devolve as pecas soltas, e nao o corte pronto, por duas razoes: o
    `conferir` precisa do fluxo E dos trechos para dizer o que ficou de
    fora do que se mandou cortar, e o editor recalcula a cada clique com o
    estado que esta na tela e ainda nao foi salvo - por isso ele chama com
    `aplicar_mont=False`/`aplicar_corr=False` e aplica as duas camadas ele
    mesmo, com a validacao mais branda que cabe a uma tela interativa (ver
    `Sessao._carrega`/`_carrega_correcoes`).

    A ordem em que as decisoes humanas se empilham e a ordem em que foram
    tomadas: transcript.md, depois anotacoes.json, depois montagem.json. A
    ultima e a mais explicita - no editor a pessoa clicou naquela
    palavra. `correcoes.json` fica fora dessa pilha de proposito: nao e
    decisao de corte, e sim de texto, e nao interfere em nenhuma delas -
    so troca `t["t"]` depois que todo o resto ja decidiu quem fica."""
    C = cfg()
    d = wdir(video)
    S = jload(d / "stream.json")
    stream, dur = S["stream"], S["dur"]

    md = d / "transcript.md"
    if md.exists() and not ignorar_transcript:
        if not confere_atualidade(md, stream):
            sys.exit(f"[{rotulo}] abortado: transcript.md desatualizado")
        n = reconcilia(stream, parse_transcript(md))
        print(f"[{rotulo}] transcript.md aplicado "
              f"({n} tokens apagados a mao)")
    else:
        print(f"[{rotulo}] usando as propostas automaticas")

    db = hop = None
    wav = d / "audio.wav"
    if wav.exists():
        db, hop, _ = envelope(wav)
    # relê as anotacoes do disco: editar anotacoes.json e rodar previa
    # deve funcionar sem refazer a analise inteira
    anot = encaixa_anotacoes(stream,
                             carrega_anotacoes(d) or S.get("anotacoes", []))
    if anot:
        print(f"[{rotulo}] {len(anot)} trechos anotados aplicados")
    mont = carrega_montagem(d)
    if mont and aplicar_mont:
        n = aplica_montagem(stream, mont, rotulo)
        print(f"[{rotulo}] montagem.json: {len(mont['tokens'])} palavras "
              f"decididas a mao ({n} mudam a proposta), "
              f"{len(mont['cortes'])} cortes e "
              f"{len(mont['restauros'])} restauros de milissegundo")
    corr = carrega_correcoes(d)
    if corr and aplicar_corr:
        aplica_correcoes(stream, corr, rotulo)

    return C, d, stream, dur, db, hop or 0.02, anot, mont, corr


def anotacoes_desobedecidas(anot, iv, tol=0.15, mao=None):
    """Anotacao que foi lida, aplicada, e mesmo assim continua no video.

    A pergunta mora aqui, uma vez so, porque duas partes precisam dela: o
    `conferir`, que lista tudo o que merece ouvido, e o `_plano`, que avisa
    antes de gastar minutos gerando audio ou video. Ja se acharam QUATRO
    caminhos pelos quais julgamento humano sumia no calculo do corte, e o
    que eles tinham em comum nao era a causa - era ninguem perguntar.

    `tol` e folga de borda, nao gosto: `folga_palavra` recua a emenda 0,06s
    de cada lado da palavra mantida vizinha, entao uma anotacao cercada de
    fala sobra ~0,12s por construcao. Acima disso e outra coisa.

    `mao` sao os trechos que a pessoa mandou ficar no editor (ver
    `spans_da_mao`). O que sobra por causa deles nao e a anotacao sumindo
    no calculo - e alguem desdizendo a anotacao de olhos abertos, depois de
    ouvir. Sai na quinta posicao como SIM ou NAO, e nao como medida, porque
    a pergunta e uma so e tem de ter uma resposta so: quem chama diz as
    duas coisas com palavras diferentes, mas nao decide qual e qual.

    O MESMO `tol` decide as duas coisas, e pela mesma razao. Reabrir uma
    palavra a mao devolve ela mais a folga em volta - e, quando ela cai
    perto da borda da anotacao, devolve tambem a lasca que sobrou do outro
    lado, fina demais para o corte pegar. MEDIDO no IMG_0246: reabrir o
    `os` de 116,59-116,83 dentro da anotacao de 116,46-128,03 devolve
    0,49s, dos quais 0,36 sao a palavra com folga e 0,13 sao essa lasca.
    Exigir a soma exata fazia o alarme mais serio deste programa disparar
    num ajuste que a propria pessoa acabara de fazer."""
    mao = _spans(mao)
    fora = []
    for an in anot or []:
        x, y = an["ini"], an["fim"]
        vivo = sum(max(0.0, min(y, b) - max(x, a)) for a, b in iv)
        if vivo <= tol:
            continue
        por_mao = sum(max(0.0, min(y, b, q) - max(x, a, p))
                      for a, b in iv for p, q in mao)
        fora.append((x, y, vivo, str(an.get("motivo", "")),
                     por_mao >= vivo - tol))
    return fora


def confere_pelo_whisper(d, C, stream, iv, rotulo):
    """Ultima conferencia antes de gastar minutos gerando audio ou video.

    Todo o resto do programa raciocina sobre os tempos do ALINHADOR. Quando
    eles estao errados, o erro e coerente do comeco ao fim: o corte cai onde
    o fluxo mandou, `_respeita_palavras` confere contra os mesmos numeros
    tortos, e ninguem reclama. Foi assim que o corte do IMG_0247 comeu "que
    muitas" - pelo alinhador a frase comecava em 603,3, depois da emenda;
    pelo audio ela comeca em 600,1, dentro dela.

    Aqui a pergunta e feita com a unica medida INDEPENDENTE que existe, a do
    Whisper: alguma palavra marcada para FICAR cai dentro de um pedaco
    removido? Nao conserta nada - so avisa. O conserto e mudar a anotacao, e
    anotacao quem escreve e uma pessoa."""
    p = d / "words.json"
    if not p.exists():
        return 0
    W = jload(p)["words"]
    pal = [t for t in stream if t["k"] == "palavra"]
    if len(W) != len(pal):
        return 0            # sem correspondencia 1:1 nao da para conferir

    fundo = C["alinhamento"]["enterrada_min"]
    suspeitas = []
    for t, w in zip(pal, W):
        if t["cut"]:
            continue
        s0 = w.get("s_whisper", w["s"])
        e0 = w.get("e_whisper", w["e"])
        if e0 - s0 <= 0.02:
            continue
        viva = sum(max(0.0, min(e0, b) - max(s0, a)) for a, b in iv)
        if viva >= (e0 - s0) * 0.5:
            continue
        # Duas coisas diferentes somem de um jeito parecido, e so a segunda
        # e defeito:
        #   RASPADA - a palavra encosta na emenda e perde um pedaco. Os dois
        #     relogios discordam ~0,1s por natureza, entao isso e rotina.
        #   ENTERRADA - a palavra esta segundos DENTRO do buraco, longe de
        #     qualquer emenda. Nenhuma imprecisao de relogio explica isso;
        #     ou o alinhador pos a palavra no lugar errado, ou o corte esta
        #     no lugar errado. Medido no IMG_0247 quebrado, a separacao e
        #     limpa: as raspadas ficam abaixo de 0,55s de fundo e as duas
        #     palavras que a anotacao comeu de verdade, "que muitas", sao a
        #     1a e a 3a mais fundas da lista.
        meio = (s0 + e0) / 2
        if any(a <= meio <= b for a, b in iv):
            continue
        fund = min(min(abs(meio - a), abs(meio - b)) for a, b in iv)
        if fund >= fundo:
            suspeitas.append((fund, viva / (e0 - s0), s0, w["w"]))
    if not suspeitas:
        return 0

    suspeitas.sort(reverse=True)
    print(f"[{rotulo}] AVISO: {len(suspeitas)} palavra(s) que deviam FICAR "
          f"estao enterradas dentro de um corte, pelos tempos do Whisper.")
    print(f"[{rotulo}] o alinhamento desse trecho provavelmente errou. "
          f"Ouça esses pontos antes de aceitar o corte:")
    for fund, frac, s0, txt in suspeitas[:8]:
        print(f"    {s0:8.2f}s  {txt!r}  ({fund:.1f}s dentro do corte, "
              f"sobra {frac:.0%})")
    if len(suspeitas) > 8:
        print(f"    ... e mais {len(suspeitas) - 8}")
    return len(suspeitas)


def _plano(a, rotulo):
    """Etapas comuns a render, audio e previa: aplica as decisoes humanas e
    calcula os trechos a manter."""
    C, d, stream, dur, db, hop, anot, mont, corr = _base(
        a.video, rotulo, getattr(a, "ignorar_transcript", False))
    iv = intervalos(stream, C, dur, db, hop, anot, mont)
    # O `conferir` ja fazia esta pergunta, e so quem lembrava de roda-lo
    # ouvia a resposta. Aqui ela e feita no caminho que TODO mundo passa
    # antes de gastar minutos: e o unico defeito deste programa que o
    # ouvido nao pega, porque o que sumiu nao faz barulho nenhum.
    for x, y, vivo, motivo, mao in anotacoes_desobedecidas(
            anot, iv, mao=spans_da_mao(stream, mont,
                                       C["corte"]["folga_palavra"])):
        if mao:
            print(f"[{rotulo}] a anotacao {x:.2f}-{y:.2f} foi reaberta "
                  f"no editor - {vivo:.2f}s de volta: {motivo[:50]}")
        else:
            print(f"[{rotulo}] AVISO: a anotacao {x:.2f}-{y:.2f} nao foi "
                  f"obedecida - {vivo:.2f}s continuam no video: "
                  f"{motivo[:60]}")
    confere_pelo_whisper(d, C, stream, iv, rotulo)
    return C, d, dur, iv


def escreve_edl(d, src, iv):
    """EDL do mpv: toca o corte direto da fonte, sem renderizar nada.

    O fim de linha TEM que ser LF - o parser do mpv rejeita o arquivo
    inteiro se encontrar \\r. E o caminho vai com prefixo %N% (N = bytes)
    porque contem ':' e '\\', que sao separadores no formato."""
    q = f"%{len(str(src).encode('utf-8'))}%{src}"
    L = ["# mpv EDL v0"] + [f"{q},{a:.3f},{b - a:.3f}" for a, b in iv]
    p = d / "previa.edl"
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(L) + "\n")
    return p


def fora_de_ordem(st):
    """Quantas palavras comecam ANTES da palavra anterior.

    O alinhador as vezes devolve um trecho com os tempos embaralhados, e ai
    a lacuna `proxima["s"] - atual["e"]` fica NEGATIVA: o detector de pausas
    nao ve pausa nenhuma e a parada longa sai na transcricao como texto
    comum. Hoje essa falha e silenciosa - contar aqui e o que a faz
    aparecer para quem le o revisao.txt."""
    pal = [t for t in st if t["k"] == "palavra"]
    return sum(1 for a, b in zip(pal, pal[1:]) if b["s"] < a["s"])


def sobrepostos(st, folga=0.5):
    """Pares de tokens (palavra OU hesitacao) que invadem o tempo um do
    outro, com o indice ORIGINAL de cada um em `st` (para ancorar no
    revisao.txt).

    `fora_de_ordem` pega o alinhador embaralhando o INICIO da palavra. Isto
    aqui pega um sintoma irmao, que nao mexe em nenhum inicio: quando
    `resolve_suspeitos` (duracao impossivel) e `marca_ecos` (retomada
    colapsada) disputam o MESMO audio, cada um sozinho tem inicio em ordem,
    mas os dois juntos cobrem o mesmo intervalo duas vezes. `_respeita_
    palavras` entao protege cada palavra real por conta propria, sem saber
    que ha uma disputa - e o que sobra e um pedaco isolado entre dois
    cortes, do tamanho de uma unica palavra.

    Medido no IMG_0246_bruto: "principalmente" (136,99-138,64s, duracao
    impossivel) e "nessas" (137,55-138,01s) tiveram cada um sua recuperacao
    disputando a mesma frase ("das mulheres, principalmente, nessas
    cartas, em que..."), sem que nenhuma violasse `fora_de_ordem` sozinha.
    O resultado foi um "em" solto - ILHA CURTA que so apareceu porque
    alguem ouviu o corte, nao porque o revisao.txt avisou.

    `folga=0.5` veio de rodar contra os quatro stream.json disponiveis
    (IMG_0246, IMG_0246_bruto, IMG_0247, IMG_0248): abaixo de ~0,3s e so
    jitter comum entre duas palavras reais vizinhas (visto ate 0,44s em
    trecho limpo, sem nenhum defeito); a 0,5s sobram so os pares que batem
    em casos ja documentados no codigo - o "do" que _respeita_palavras
    cita (IMG_0247, 49,81-53,08 x 50,64-50,90) e o proprio "principalmente"
    acima, presentes tanto no bruto quanto no IMG_0246 ja julgado (onde a
    disputa se resolveu diferente: os dois tokens saem cut=True, sem
    nenhuma anotacao cobrindo o trecho - sinal de que o resultado desse
    canto do pipeline nao e estavel entre rodadas, mais um motivo para
    avisar sempre)."""
    pal = sorted(((t["s"], i) for i, t in enumerate(st)
                 if t["k"] in ("palavra", "hesitacao")))
    out = []
    for (sa, ia), (sb, ib) in zip(pal, pal[1:]):
        if sb < st[ia]["e"] - folga:
            out.append((ia, ib))
    return out


def rarefeitos(st, cr):
    """Trechos que SOBRAM no corte com fala rarefeita demais.

    Nao depende da lacuna entre palavras - que e justamente o que quebra
    quando o alinhador embaralha os tempos (ver `fora_de_ordem`). Conta
    quantas palavras mantidas tem o PONTO MEDIO dentro de uma janela
    deslizante e divide pelo tempo da janela que sobrevive ao corte.

    Um span marcado para corte que ATRAVESSA palavra mantida nao entra na
    conta do que sai: `_respeita_palavras` vai encolhe-lo ate sumir na hora
    de cortar, entao aquele tempo continua no video apesar do [-N.Ns] que
    aparece na transcricao. Sem essa ressalva o trecho do IMG_0247
    (575-598s: 23s para 16 palavras, 0,70/s contra ~2,5/s no resto do
    video) parecia cortado no papel e passava batido - foi assim que ele
    chegou ao corte final e virou ar morto."""
    pal = [t for t in st if t["k"] == "palavra"]
    if not pal:
        return []
    mantidas = [t for t in pal if not t["cut"]]
    meio = sorted((t["s"] + t["e"]) / 2 for t in mantidas)
    fora = []
    for c in sorted([t["s"], t["e"]] for t in st if t["cut"] and not any(
            k["e"] > t["s"] + 0.02 and k["s"] < t["e"] - 0.02
            for k in mantidas)):
        if fora and c[0] <= fora[-1][1]:
            fora[-1][1] = max(fora[-1][1], c[1])
        else:
            fora.append(c)

    def sobra(a, b):
        return (b - a) - sum(max(0.0, min(b, y) - max(a, x)) for x, y in fora)

    def conta(a, b):
        return bisect.bisect_left(meio, b) - bisect.bisect_left(meio, a)

    ini = min(t["s"] for t in pal)
    fim = max(t["e"] for t in pal)
    achados, t = [], ini
    while t < fim:
        b = min(fim, t + cr["janela"])
        s = sobra(t, b)
        if s >= cr["duracao_minima"] and conta(t, b) / s < cr["palavras_seg"]:
            if achados and t <= achados[-1][1]:
                achados[-1][1] = b
            else:
                achados.append([t, b])
        if b >= fim:
            break
        t += cr["passo"]
    return [(a, b, sobra(a, b), conta(a, b)) for a, b in achados]


def cmd_revisar(a):
    """Escreve o arquivo minimo que o passe de julgamento precisa ler.

    Existe por economia: o passe semantico so precisa do texto do video, e
    nada mais. Rodado dentro de uma conversa longa, ele arrasta junto todo o
    historico a cada resposta. Aqui a entrada fica num arquivo pequeno, para
    a analise acontecer numa sessao limpa.

    Compacta agressivamente: o que ja foi cortado vira um marcador com a
    duracao, em vez de aparecer palavra por palavra. Num video de 10 min isso
    troca ~9 mil tokens por ~2,5 mil, sem perder o que importa para julgar."""
    d = wdir(a.video)
    S = jload(d / "stream.json")
    st, dur = S["stream"], S["dur"]
    raros = rarefeitos(st, cfg()["revisao"])
    bagunca = fora_de_ordem(st)
    choques = sobrepostos(st)

    L = [f"# {Path(a.video).stem} | {hhmmss(dur)} | tempos em segundos",
         "# [-N.Ns] = ja marcado para corte, trecho quieto. Texto solto =",
         "# sera mantido.",
         "# [-N.Ns VOZ] = marcado para corte MESMO havendo som alto ali",
         "# dentro. O transcritor nao leu, mas o microfone ouviu: pode ser",
         "# frase inteira prestes a sumir. Confira antes de deixar passar.",
         "# [?? N.Ns] = som alto que ninguem conseguiu ler, MANTIDO de",
         "# proposito para voce decidir. Diga se sai ou fica.",
         "# palavra~ = o tempo dessa palavra nao e confiavel (o alinhador",
         "# recusou o proprio resultado e caiu no tempo grosseiro do",
         "# Whisper). NAO ancore borda de corte numa palavra com ~: ela",
         "# pode estar segundos longe de onde aparece aqui.",
         "# [!Ns/N palavras] = o que SOBRA ali e fala rarefeita (o normal e",
         "# ~2,5 palavras/s): parada longa que o detector de pausas nao viu.",
         "# [disputa N.Ns] = duas recuperacoes automaticas (duracao",
         "# impossivel + retomada colapsada) brigaram pelo mesmo audio.",
         "# Nenhuma teve o inicio embaralhado sozinha, entao isto nao vira",
         "# ~ nem [aviso fora de ordem] - mas o resultado pode ser uma",
         "# palavra solta entre dois cortes (ILHA CURTA no conferir). Ouca",
         "# com tarocut.py audio antes de aceitar o corte automatico ali.",
         "# Procure: retomadas com outras palavras, interrupcoes, trechos",
         "# em que ela se perde. Responda com trechos [inicio, fim, motivo]."]
    if bagunca:
        L.append(f"# aviso: {bagunca} palavras com tempo fora de ordem - o "
                 f"alinhador embaralhou")
        L.append("# esses trechos, e ali o [-N.Ns] pode faltar mesmo "
                 "havendo parada.")
    if choques:
        L.append(f"# aviso: {len(choques)} disputa(s) de recuperacao "
                 f"automatica marcadas [disputa] abaixo - confira com audio.")
    L.append("")

    # ancora cada aviso no primeiro token MANTIDO que cai dentro dele: e o
    # unico ponto do texto corrido onde ele pode aparecer.
    marca = {}
    for x, y, s, n in raros:
        for i, t in enumerate(st):
            if not t["cut"] and t["e"] > x and t["s"] < y:
                marca[i] = f"[!{s:.0f}s/{n} palavras]"
                break

    # disputa de recuperacao: ancora no segundo token do par (o que invade),
    # que e o que aparece "de surpresa" lendo o texto em ordem.
    for ia, ib in choques:
        sobra = st[ia]["e"] - st[ib]["s"]
        marca[ib] = marca.get(ib, "") + f"[disputa {sobra:.1f}s]"

    # Palavras cujo tempo o alinhador recusou. Sem isto o revisao.txt mente
    # com cara de verdade: no IMG_0247 um "As" impresso em 80,0s foi falado
    # em 85,9s, e a anotacao de corte nasceu 6s adiante da frase que queria
    # pegar - foi assim que sobrou o "Aaas" solto aos 20s da previa.
    #
    # So "deriva" e "ordem". O terceiro motivo de recusa, "score", quer
    # dizer que o CTC ficou inseguro e se usou o tempo do Whisper: erro de
    # 0,1s, irrelevante para anotar. Marcar os tres punha ~ em 11% das
    # palavras (92 de 812 no IMG_0247), tres seguidos na mesma linha, e o
    # aviso virava papel de parede. Estes dois sao os que podem estar
    # SEGUNDOS fora do lugar - e a categoria do "As" de 80s.
    W = jload(d / "words.json").get("words", [])
    pal_i = [i for i, t in enumerate(st) if t["k"] == "palavra"]
    duvida = ({pal_i[k] for k, w in enumerate(W)
               if w.get("align_rejeitado") in ("deriva", "ordem")}
              if len(pal_i) == len(W) else set())

    # O microfone e a segunda opiniao sobre o que esta indo embora: um
    # [-1.8s] de silencio e um [-1.8s] de frase eram impressos igual.
    db, hop, _ = envelope(d / "audio.wav")
    limiar = limiar_voz(db, cfg()["silencio"]["limiar_fala_db"])

    linha, ini, cortado, alto = [], None, 0.0, 0.0
    for i, t in enumerate(st):
        if ini is None:
            ini = t["s"]
        if t["cut"]:
            cortado += t["e"] - t["s"]
            # SO conta como VOZ o que foi cortado sem se saber o que era.
            # Anotacao, cacoete do lexico e tentativa detectada foram
            # decididos pelo TEXTO - ter voz dentro e o esperado, e marcar
            # isso fez o VOZ aparecer em 60% das marcas e virar papel de
            # parede. Sobram os dois julgamentos puramente acusticos:
            # "voz sem texto" e "pausa".
            if t["why"].startswith(("voz sem texto", "pausa")):
                frac, pico = mede_voz(db, hop, t["s"], t["e"], limiar)
                if frac > 0.5 and pico - limiar > 25.0:
                    alto += t["e"] - t["s"]
            continue
        if cortado > 0.3:
            linha.append(f"[-{cortado:.1f}s VOZ]" if alto > 0.3
                         else f"[-{cortado:.1f}s]")
        cortado = alto = 0.0
        if i in marca:
            linha.append(marca[i])
        if t["k"] == "palavra":
            linha.append(t["t"] + ("~" if i in duvida else ""))
        else:
            linha.append(f"[{t['t'][:14]}]")
        if len(" ".join(linha)) > 130:
            L.append(f"{ini:6.1f} " + " ".join(linha))
            linha, ini = [], None
    if linha:
        L.append(f"{ini:6.1f} " + " ".join(linha))

    p = d / "revisao.txt"
    p.write_text("\n".join(L) + "\n", encoding="utf-8")
    n = len("\n".join(L))
    print(f"[revisar] {p}  ({n} caracteres, ~{n//4} tokens)")
    print(f"[revisar] escreva o julgamento em {d/'anotacoes.json'}")


def _mapa_previa(iv):
    """Tempo da PREVIA -> tempo da FONTE, e vice-versa.

    O Rafael ouve o .m4a e reclama em "00:14". Todo o resto do programa
    pensa em tempo de fonte. Traduzir isso a mao, um defeito de cada vez,
    foi o trabalho manual de duas rodadas de revisao inteiras."""
    def para_fonte(t):
        p = 0.0
        for a, b in iv:
            if t <= p + (b - a):
                return a + (t - p)
            p += b - a
        return iv[-1][1] if iv else 0.0

    def para_previa(t):
        p = 0.0
        for a, b in iv:
            if t < a:
                return p                    # cai dentro de um corte
            if t <= b:
                return p + (t - a)
            p += b - a
        return p
    return para_fonte, para_previa


def cmd_conferir(a):
    """Confere a GEOMETRIA do corte, nao o texto.

    Existe porque `revisao.txt` mostra o que as palavras DIZEM e nao onde a
    lamina cai. Farelo de palavra, ilha de 0,2s, fala sobrevivendo dentro de
    trecho mantido: nada disso aparece la. Foi assim que seis defeitos
    chegaram ao Rafael de uma vez - nenhum era erro de julgamento de
    conteudo, todos eram de geometria, e a unica rede era o ouvido dele.

    Le pela MESMA funcao que o `audio` e o `render` usam. Ler o
    `graph_audio.txt` seria ler o corte anterior, nao o de agora.

    Cada achado sai nos dois tempos: o da fonte, para anotar, e o da previa,
    para conferir de ouvido sem contar no dedo.

    NAO confere "emenda pousada em ponto alto do envelope". Foi tentado:
    com o piso no percentil 5 e o alarme em 25 dB acima dele, disparava em
    51 das 74 emendas do IMG_0247 e em 51 das 71 do IMG_0248. E esperado -
    a emenda encosta na palavra mantida por construcao, e ali sempre ha voz.
    Alarme que dispara em 70% das emendas e papel de parede. O que aquele
    teste queria pegar - silaba comida - virou a folga de 0,06 no
    config.toml, que ataca a causa em vez do sintoma."""
    C, d, stream, dur, db, hop, anot, mont, corr = _base(a.video, "conferir",
                                                         a.ignorar_transcript)
    iv = intervalos(stream, C, dur, db, hop, anot, mont)
    para_fonte, para_previa = _mapa_previa(iv)
    mantido = sum(b - x for x, b in iv)

    if getattr(a, "em", None):
        p = a.em.split(":")
        t = sum(float(x) * 60 ** i for i, x in enumerate(reversed(p)))
        print(f"[conferir] previa {hhmmss(t)} = fonte {para_fonte(t):.2f}s "
              f"({hhmmss(para_fonte(t))})")
        return

    achados = []

    def diz(rot, x, y, txt):
        achados.append((para_previa(x), rot, x, y, txt))

    # 1. Anotacao desobedecida. E o pior de todos: o julgamento humano foi
    #    escrito, foi lido, e sumiu na geometria sem uma linha de aviso.
    for x, y, vivo, motivo, mao in anotacoes_desobedecidas(
            anot, iv, mao=spans_da_mao(stream, mont,
                                       C["corte"]["folga_palavra"])):
        diz("ANOTACAO REABERTA A MAO" if mao
            else "ANOTACAO NAO OBEDECIDA", x, y,
            f"sobra {vivo:.2f}s de {y-x:.2f}s | {motivo[:50]}")

    # 2. Fala marcada para sair que continua tocando. Silencio nao entra:
    #    metade da pausa fica colada em cada lado de proposito
    #    (pausa_alvo/2), entao ele "sobrevive" sempre, por desenho.
    for t in stream:
        if not t["cut"] or t["k"] == "silencio":
            continue
        vivo = sum(max(0.0, min(t["e"], b) - max(t["s"], aa)) for aa, b in iv)
        if vivo > 0.15:
            diz("FALA CORTADA SOBREVIVE", t["s"], t["e"],
                f"{vivo:.2f}s | {t.get('why','')[:24]} | {t['t'][:30]!r}")

    # 3. Ilha curta. Nem sempre e defeito - uma palavra sozinha entre dois
    #    cortes pode ser exatamente o que a anotacao pediu -, mas e sempre
    #    duas emendas em volta de pouca coisa, e vale um ouvido.
    for x, y in iv:
        if y - x < C["corte"]["ilha_minima"]:
            pals = " ".join(t["t"] for t in stream if t["k"] == "palavra"
                            and not t["cut"] and t["e"] > x and t["s"] < y)
            diz("ILHA CURTA", x, y, f"{y-x:.2f}s | {pals[:40]!r}")

    # 4. Palavra mantida raspada pela borda. Contra os tempos do alinhador
    #    isto tem de dar zero - `_respeita_palavras` garante. Fica como
    #    alarme de regressao: se um dia aparecer, alguem quebrou a trava.
    for t in stream:
        if t["cut"] or t["k"] != "palavra":
            continue
        fe, largura = fim_util(t), fim_util(t) - t["s"]
        if largura <= 0:
            continue
        vivo = sum(max(0.0, min(fe, b) - max(t["s"], aa)) for aa, b in iv)
        if largura - vivo > 0.03 and vivo > 0.02:
            diz("PALAVRA RASPADA", t["s"], fe,
                f"perde {1000*(largura-vivo):.0f}ms | {t['t'][:20]!r}")

    # ORDEM DE ESCUTA, nao ordem de categoria. Quem confere ouve o .m4a do
    # comeco ao fim uma vez so; uma lista agrupada por tipo de defeito obriga
    # a pular para tras seis vezes.
    achados.sort()
    L = [f"# {Path(a.video).stem} | {len(iv)} emendas | "
         f"{hhmmss(mantido)} de {hhmmss(dur)}",
         "# ouca na ordem: a coluna PREVIA e o tempo no .m4a.",
         "# ANOTACAO NAO OBEDECIDA = voce mandou cortar e nao saiu. Sempre bug.",
         "# ANOTACAO REABERTA A MAO = voce mesmo trouxe de volta, no editor.",
         "# FALA CORTADA SOBREVIVE = o programa marcou para sair e continua ali.",
         "# ILHA CURTA = pouca coisa entre dois cortes; nem sempre e defeito.",
         "# PALAVRA RASPADA = borda comeu pedaco de palavra mantida.",
         ""]
    for p, rot, x, y, txt in achados:
        L.append(f"{hhmmss(p)}  {rot:24s} fonte {x:8.2f}-{y:<8.2f} {txt}")
    if not achados:
        L.append("nada suspeito")

    p = d / "conferir.txt"
    p.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    print(f"\n[conferir] {len(achados)} pontos para ouvir -> {p}"
          if achados else f"\n[conferir] nada suspeito -> {p}")


def cmd_editor(a):
    """Abre a transcricao no navegador para o ajuste fino.

    Mora em `editor.py`, e o import e aqui dentro de proposito: o
    editor carrega servidor HTTP e thread, e nenhuma das outras etapas
    precisa disso para rodar.

    Vem DEPOIS do passe de julgamento, nao no lugar dele. O `/revisar`
    decide o que sai lendo o texto inteiro; aqui se conserta a borda de
    milissegundo que so aparece ouvindo."""
    import editor
    editor.serve(a.video, a.porta, not a.sem_abrir)


def cmd_previa(a):
    _, d, dur, iv = _plano(a, "previa")
    meta = jload(d / "meta.json")
    edl = escreve_edl(d, meta["src"], iv)
    mantido = sum(b - x for x, b in iv)
    print(f"[previa] {len(iv)} trechos  {hhmmss(mantido)} de {hhmmss(dur)} "
          f"(corta {100*(1-mantido/dur):.0f}%)")
    mpv = _find_mpv()
    if not mpv:
        print(f"[previa] mpv nao encontrado. Abra manualmente: {edl}")
        return
    print(f"[previa] abrindo mpv... (setas = pular, espaco = pausa)")
    subprocess.run([mpv, "--force-window=yes",
                    f"--force-media-title=previa {Path(a.video).name}",
                    str(edl)])


def _find_mpv():
    p = shutil.which("mpv")
    if p:
        return p
    for c in (Path(r"C:\Program Files\MPV Player\mpv.exe"),
              Path(r"C:\Program Files\mpv\mpv.exe")):
        if c.exists():
            return str(c)
    base = Path(os.environ.get("LOCALAPPDATA", ""))
    for c in base.glob("Microsoft/WinGet/Packages/*mpv*/**/mpv.exe"):
        return str(c)
    return None


def _loudnorm(entrada, ca):
    """Mede o loudness e devolve (filtro da 2a passada, LUFS de entrada).

    Devolve o filtro INTEIRO, e nao so os parametros medidos, para que o
    `audio` e o `render` nao possam divergir: a previa em .m4a so serve para
    julgar volume enquanto receber o mesmo tratamento do .mov. O highpass e
    o `linear=true` ja estiveram escritos duas vezes, um em cada comando.

    LUFS de entrada = None quando a medicao falha; ai o filtro sai sem os
    `measured_*` e o loudnorm faz uma passada so, que e pior mas funciona."""
    alvo = (f"loudnorm=I={ca['lufs_alvo']}:TP={ca['true_peak']}"
            f":LRA={ca['lra']}")
    med = run([FFMPEG, "-hide_banner", "-nostats", "-i", str(entrada), "-vn",
               "-af", alvo + ":print_format=json", "-f", "null", "-"])
    try:
        blob = med.stderr[med.stderr.rindex("{"):]
        M = json.loads(blob[:blob.index("}") + 1])
        medido = (f":measured_I={M['input_i']}:measured_TP={M['input_tp']}"
                  f":measured_LRA={M['input_lra']}"
                  f":measured_thresh={M['input_thresh']}"
                  f":offset={M['target_offset']}")
        lufs = M["input_i"]
    except Exception:
        medido, lufs = "", None
    return (f"highpass=f={ca['highpass_hz']},{alvo}{medido}:linear=true",
            lufs)


def cmd_audio(a):
    """Corte so em audio, para julgar sem esperar render de video.

    O gargalo do render e decodificar 4K60 HEVC 10 bits; com `-vn` o ffmpeg
    nem toca no video. Da para ouvir a edicao inteira em segundos em vez de
    minutos, que e o que torna a calibracao viavel."""
    C, d, dur, iv = _plano(a, "audio")
    meta = jload(d / "meta.json")
    ca = C["audio"]
    fade = ca["fade"]

    L = [trecho_audio(i, x, y, fade) for i, (x, y) in enumerate(iv)]
    L.append("".join(f"[a{i}]" for i in range(len(iv)))
             + f"concat=n={len(iv)}:v=0:a=1[ac]")
    g = d / "graph_audio.txt"
    with open(g, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(L))

    saida = Path(a.out) if a.out else (
        Path(meta["src"]).parent / f"{Path(a.video).stem}_previa.m4a")
    bruto = d / "_previa_bruta.wav"
    mantido = sum(y - x for x, y in iv)
    print(f"[audio] {len(iv)} trechos, {hhmmss(mantido)} de {hhmmss(dur)} "
          f"(corta {100*(1-mantido/dur):.0f}%) ...", flush=True)

    # `meta["src"]` e nao `a.video`: todo o resto do programa aceita o nome
    # curto ("IMG_0247"), porque `wdir` resolve. So o ffmpeg precisa do
    # arquivo de verdade, e ele esta anotado no meta.json desde o ingest.
    # Passar `a.video` fazia `audio IMG_0247` morrer em "No such file" depois
    # de ja ter calculado o corte inteiro - e o `previa` ao lado ja fazia
    # certo.
    r = subprocess.run([FFMPEG, "-y", "-v", "error", "-vn",
                        "-i", str(meta["src"]),
                        "-/filter_complex", str(g), "-map", "[ac]",
                        "-c:a", "pcm_s16le", "-ar", "48000", str(bruto)])
    if r.returncode != 0:
        sys.exit("[audio] ffmpeg falhou no corte")

    af, entrada_lufs = _loudnorm(bruto, ca)
    if entrada_lufs is not None:
        print(f"[audio] {entrada_lufs} LUFS -> {ca['lufs_alvo']} LUFS")
    r = subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(bruto),
                        "-af", af, "-c:a", "aac", "-b:a", "192k",
                        "-ar", "48000", str(saida)])
    bruto.unlink(missing_ok=True)
    if r.returncode != 0:
        sys.exit("[audio] ffmpeg falhou na normalizacao")
    print(f"[audio] pronto: {saida}  ({hhmmss(mantido)}, "
          f"{saida.stat().st_size/1e6:.1f} MB)")


def _tempo_saida(t, iv):
    """Converte um instante de tempo de FONTE para tempo de SAIDA (depois
    do corte), pela mesma `iv` que o render usa (CLAUDE.md: 'o navegador
    nao calcula corte' - vale aqui do mesmo jeito, so que em vez do
    editor e o comando `cartas`). Um instante DENTRO de um trecho cortado
    nao existe na saida; devolve o ponto do trecho mantido mais proximo,
    porque uma carta nao pode aparecer num tempo que o corte apagou."""
    acumulado = 0.0
    for a, b in iv:
        if t < a:
            return acumulado
        if t <= b:
            return acumulado + (t - a)
        acumulado += b - a
    return acumulado


def _agrupa_spans_cartas(mencoes, iv, cc):
    """Mencoes (tempo de FONTE) -> spans de exibicao (tempo de SAIDA).
    Mapeia cada mencao para a saida ANTES de agrupar - fundir por
    proximidade so faz sentido no relogio que quem assiste ve, nao no da
    fonte (o corte pode ter comido o intervalo entre duas falas, ou
    esticado por causa de outra coisa cortada no meio)."""
    pontos = sorted(
        (_tempo_saida(m["s"], iv), _tempo_saida(m["e"], iv), m["carta"])
        for m in mencoes)

    spans = []
    for ini, fim, carta in pontos:
        if (spans and spans[-1]["carta"] == carta
                and ini - spans[-1]["fim"] <= cc["gap_max"]):
            spans[-1]["fim"] = max(spans[-1]["fim"], fim)
        else:
            spans.append({"carta": carta, "ini": ini, "fim": fim})

    for s in spans:
        s["ini"] = max(0.0, s["ini"] - cc["antecipa"])
        if s["fim"] - s["ini"] < cc["duracao_minima"]:
            s["fim"] = s["ini"] + cc["duracao_minima"]

    # Sem empilhamento (layout 4 ficou de fora por decisao do Rafael - ver
    # plano): se o minimo de uma carta invade a proxima, a de tras cede -
    # a carta que comecou depois e a que esta sendo falada agora.
    for i in range(len(spans) - 1):
        if spans[i]["fim"] > spans[i + 1]["ini"]:
            spans[i]["fim"] = spans[i + 1]["ini"]

    return [s for s in spans if s["fim"] > s["ini"]]


def _resolve_carta(texto):
    import baralho
    alvo = baralho.chave(texto)
    for c in baralho.catalogo():
        if baralho.chave(c["nome"]) == alvo:
            return c["nome"]
    sys.exit(f"[cartas] '{texto}' nao bate com nenhuma carta do catalogo "
             f"(veja baralho.py --carta imprime a lista)")


def _catalogo_img(nome):
    import baralho
    for c in baralho.catalogo():
        if c["nome"] == nome:
            return c["img"]
    sys.exit(f"[cartas] carta '{nome}' sumiu do catalogo entre a deteccao "
             f"e o render - baralho completo/ mudou no meio do caminho?")


def cmd_cartas(a):
    """Checkpoint 3 + 4 do plano 'adicionar cartas': acha as mencoes de
    carta na fala, decide a principal, monta a agenda de quando uma
    secundaria entra em tela (`cartas.json`) e renderiza uma previa
    barata (baixa resolucao) para aprovar antes do `render` de verdade
    bakear isso em qualidade final. Video sem carta principal detectavel
    nao escreve `cartas.json` nem gera previa - fica como generalistico,
    fora do escopo desta feature por ora."""
    import baralho
    C, d, stream, dur, db, hop, anot, mont, corr = _base(a.video, "cartas")
    iv = intervalos(stream, C, dur, db, hop, anot, mont)

    # Casador so olha token que SOBREVIVE ao corte (stream[i]['cut'] ja
    # tem transcript.md/anotacoes/montagem aplicados por _base): uma
    # tentativa de fala cortada nao pode acender uma carta que ninguem
    # vai ouvir.
    vivos = [t for t in stream if not t.get("cut")]
    mencoes = baralho.acha_mencoes(vivos)

    if not mencoes:
        print(f"[cartas] nenhuma carta reconhecida na fala - "
              f"parece video generalistico, nada a fazer aqui")
        (d / "cartas.json").unlink(missing_ok=True)
        return

    contagem = {}
    for m in mencoes:
        contagem[m["carta"]] = contagem.get(m["carta"], 0) + 1
    principal = _resolve_carta(a.carta) if a.carta \
        else max(contagem, key=contagem.get)

    print(f"[cartas] carta principal: {principal}"
          + ("" if a.carta else "  (auto: a mais mencionada)"))
    for carta, n in sorted(contagem.items(), key=lambda x: -x[1]):
        print(f"  {n}x  {carta}" + ("  <- principal" if carta == principal
                                     else ""))

    cc = C["cartas"]
    secundarias_menc = [m for m in mencoes if m["carta"] != principal]
    spans = _agrupa_spans_cartas(secundarias_menc, iv, cc)
    cartas = {"principal": principal, "fade_entrada": 2.0,
              "secundarias": spans}
    jdump(cartas, d / "cartas.json")

    if spans:
        print(f"[cartas] {len(spans)} exibicao(oes) de carta secundaria "
              f"(tempo de SAIDA, ja cortado):")
        for s in spans:
            print(f"  {hhmmss(s['ini'])} -> {hhmmss(s['fim'])}  {s['carta']}")
    else:
        print("[cartas] nenhuma carta secundaria mencionada nesta edicao - "
              "o video fica todo no layout 1")

    # ---- previa: barata, sempre gerada (README: o gargalo e decodificar
    # 4K60, e a previa sai em resolucao baixa) ----
    import rosto
    rjson = d / "rosto.json"
    if not rjson.exists() or a.force:
        rosto.rastreia(Path(a.video), d)
    resumo_rosto = jload(rjson)["resumo"]

    meta = jload(d / "meta.json")
    w_fonte, h_fonte = meta["w"], meta["h"]
    principal_img = _catalogo_img(principal)
    nomes_sec = list(dict.fromkeys(s["carta"] for s in spans))
    secundarias_imgs = [(nome, _catalogo_img(nome)) for nome in nomes_sec]

    # Dois grafos, nao um: com decode QSV o [0:v] ainda esta em memoria
    # de GPU quando chega no crop/overlay, e so `hw=True` poe o
    # `hwdownload` que isso pede - grafo errado para a chamada e erro de
    # pixel format, nao resultado errado (ver nota em grafo_cartas()).
    #
    # 360p/15fps, nao 480p/30: MEDIDO que decode e crop nao sao o custo
    # da previa (crop sozinho roda a 1.5x tempo real) - e o `overlay`
    # que e caro por QUADRO nesta versao do ffmpeg, com ou sem QSV, com
    # ou sem imagem de carta real (reproduzido ate com uma cor lisa de
    # lavfi). Metade dos quadros = metade do trabalho de overlay: e o
    # unico botao que realmente mexe no tempo da previa.
    gscript_hw = d / "graph_cartas_qsv.txt"
    gscript_hw.write_text(
        grafo_cartas(iv, C, cartas, resumo_rosto, w_fonte, h_fonte,
                     secundarias_imgs, escala=360, fps=15, hw=True),
        encoding="utf-8")
    gscript_sw = d / "graph_cartas.txt"
    gscript_sw.write_text(
        grafo_cartas(iv, C, cartas, resumo_rosto, w_fonte, h_fonte,
                     secundarias_imgs, escala=360, fps=15, hw=False),
        encoding="utf-8")

    mantido = sum(b - x for x, b in iv)
    saida = d / "cartas_previa.mp4"
    entradas = (["-i", str(meta["src"]), "-loop", "1", "-i", str(principal_img)]
                + [x for _, p in secundarias_imgs
                   for x in ("-loop", "1", "-i", str(p))])

    def _saidas(gscript):
        return (["-/filter_complex", str(gscript), "-map", "[vo]", "-map", "[ao]",
                 "-t", f"{mantido:.3f}",
                 "-c:v", "libx264", "-preset", "fast", "-crf", "28",
                 "-c:a", "aac", "-b:a", "128k", str(saida)])

    print(f"[cartas] gerando previa lo-res...", flush=True)
    # QSV primeiro: decode de software do IMG_0301 inteiro (5,5min) levou
    # mais de 30min so nesta etapa, contra minutos com QSV - MEDIDO no
    # checkpoint 4. Se a combinacao hwaccel+filtros nao colar nesta versao
    # do ffmpeg, cai para software antes de desistir - mesma rede de
    # seguranca do rosto.amostra_frames().
    r = subprocess.run([FFMPEG, "-y", "-v", "error", "-stats",
                        "-hwaccel", "qsv"] + entradas + _saidas(gscript_hw))
    if r.returncode != 0:
        r = subprocess.run([FFMPEG, "-y", "-v", "error", "-stats"]
                            + entradas + _saidas(gscript_sw))
    if r.returncode != 0 or not saida.exists():
        sys.exit("[cartas] ffmpeg falhou na previa - veja graph_cartas*.txt")
    print(f"[cartas] previa pronta: {saida}  "
          f"({saida.stat().st_size/1e6:.1f} MB)")
    mpv = _find_mpv()
    if mpv and not a.sem_abrir:
        subprocess.run([mpv, "--force-window=yes",
                        f"--force-media-title=previa cartas {Path(a.video).name}",
                        str(saida)])


def cmd_render(a):
    C, d, dur, iv = _plano(a, "render")
    cr = C["render"]
    meta = jload(d / "meta.json")
    mantido = sum(b - a_ for a_, b in iv)
    print(f"[render] {len(iv)} trechos mantidos  "
          f"{hhmmss(mantido)} de {hhmmss(dur)}  "
          f"(corta {100*(1-mantido/dur):.0f}%)")
    jdump({"manter": iv, "dur_original": dur, "dur_final": mantido},
          d / "cuts.json")
    if a.dry_run:
        print("[render] --dry-run: parei aqui. Veja cuts.json")
        return

    escala = fps = None
    if cr["perfil"] == "1080p30":
        escala, fps = 1080, 30
    gscript = d / "graph.txt"
    gscript.write_text(grafo(iv, C, escala, fps), encoding="utf-8")

    saida = Path(a.out) if a.out else d / f"{Path(a.video).stem}_cortado.mov"
    corte = d / "_corte.mov"
    hevc = "hevc" in meta.get("vcodec", "")
    enc = ["-c:v", "hevc_qsv" if hevc else "h264_qsv",
           "-global_quality", str(cr["qualidade"])]
    if not cr["gpu"]:
        enc = ["-c:v", "libx265" if hevc else "libx264",
               "-crf", str(cr["qualidade"]), "-preset", "medium"]
    cor = ["-color_primaries", "bt2020", "-color_trc", "arib-std-b67",
           "-colorspace", "bt2020nc", "-tag:v", "hvc1"] if hevc else []

    dec = ["-hwaccel", "qsv"] if cr["gpu"] else []
    cmd = ([FFMPEG, "-y", "-v", "error", "-stats"] + dec
           # "-/filter_complex arquivo" le o grafo de um arquivo. Necessario:
           # sao centenas de emendas, nao cabem na linha de comando. (O antigo
           # -filter_complex_script saiu no ffmpeg 9.)
           # `meta["src"]`, nao `a.video`: ver a mesma nota no `cmd_audio`.
           + ["-i", str(meta["src"]), "-/filter_complex", str(gscript),
              "-map", "[vo]", "-map", "[ao]"] + enc + cor
           + ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(corte)])
    print(f"[render] cortando ({len(iv)} emendas)...", flush=True)
    r = subprocess.run(cmd)
    if r.returncode != 0 or not corte.exists():
        sys.exit("[render] ffmpeg falhou no corte")

    # ---- audio: loudnorm de duas passadas -----------------------------
    ca = C["audio"]
    print("[render] medindo loudness...", flush=True)
    af, entrada_lufs = _loudnorm(corte, ca)
    print(f"[render] entrada {entrada_lufs} LUFS -> alvo {ca['lufs_alvo']} LUFS"
          if entrada_lufs is not None
          else "[render] medicao falhou; loudnorm em passada unica")
    print("[render] normalizando audio...", flush=True)
    r = subprocess.run([FFMPEG, "-y", "-v", "error", "-stats", "-i",
                        str(corte), "-c:v", "copy", "-af", af,
                        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(saida)])
    if r.returncode != 0:
        sys.exit("[render] ffmpeg falhou no audio")
    corte.unlink(missing_ok=True)
    mb = saida.stat().st_size / 1e6
    print(f"[render] pronto: {saida}  ({hhmmss(mantido)}, {mb:.0f} MB)")

# ---------------------------------------------------------------------- lote

EXTS = {".mov", ".mp4", ".mkv", ".m4v", ".avi"}

# A ordem do pipeline, uma vez so. Ja morou em tres lugares - a lista de
# nomes do `lote`, a lista de pares logo abaixo dela e o `choices` do
# argparse -, e acrescentar etapa exigia lembrar dos tres.
ETAPAS = [("ingest", cmd_ingest), ("transcribe", cmd_transcribe),
          ("align", cmd_align), ("analyze", cmd_analyze),
          ("draft", cmd_draft), ("render", cmd_render)]


def cmd_lote(a):
    """Processa uma pasta inteira. Pensado para rodar de madrugada:
    'ate=draft' transcreve e analisa tudo, deixando os transcript.md
    prontos para revisao. O render fica para depois, ja com as edicoes."""
    pasta = Path(a.pasta)
    vids = sorted(p for p in pasta.iterdir()
                  if p.suffix.lower() in EXTS and p.is_file())
    if a.pular:
        pad = [x.strip().lower() for x in a.pular.split(",")]
        vids = [v for v in vids
                if not any(p in v.name.lower() for p in pad)]
    if not vids:
        sys.exit(f"[lote] nenhum video em {pasta}")

    ate = [n for n, _ in ETAPAS].index(a.ate)
    print(f"[lote] {len(vids)} videos, ate a etapa '{a.ate}'\n")
    falhas = []
    for i, v in enumerate(vids, 1):
        print(f"{'='*70}\n[lote] {i}/{len(vids)}  {v.name}\n{'='*70}",
              flush=True)
        ns = argparse.Namespace(video=str(v), force=a.force, model=a.model,
                                out=None, dry_run=False,
                                ignorar_transcript=a.ignorar_transcript)
        try:
            for _, fn in ETAPAS[:ate + 1]:
                fn(ns)
        except SystemExit as e:
            print(f"[lote] FALHOU em {v.name}: {e}")
            falhas.append(v.name)
        except Exception as e:
            print(f"[lote] FALHOU em {v.name}: {type(e).__name__}: {e}")
            falhas.append(v.name)
        print()
    print(f"{'='*70}\n[lote] concluido. "
          f"{len(vids)-len(falhas)}/{len(vids)} ok")
    if falhas:
        print("[lote] falhas: " + ", ".join(falhas))

# --------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(prog="tarocut")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for nome, fn in ETAPAS[:-1]:          # render tem parser proprio
        p = sub.add_parser(nome)
        p.add_argument("video")
        p.add_argument("--force", action="store_true")
        p.add_argument("--model", default=None)
        p.set_defaults(fn=fn)
    rv = sub.add_parser("revisar",
                        help="escreve revisao.txt para o passe de julgamento")
    rv.add_argument("video")
    rv.set_defaults(fn=cmd_revisar)

    cf = sub.add_parser("conferir",
                        help="confere a geometria do corte sem ouvir")
    cf.add_argument("video")
    cf.add_argument("--em", default=None,
                    help="traduz um tempo da previa (MM:SS) para a fonte")
    cf.add_argument("--ignorar-transcript", action="store_true")
    cf.set_defaults(fn=cmd_conferir)

    au = sub.add_parser("audio", help="corte so em audio, rapido, para julgar")
    au.add_argument("video")
    au.add_argument("--out", default=None)
    au.add_argument("--ignorar-transcript", action="store_true")
    au.set_defaults(fn=cmd_audio)

    ed = sub.add_parser("editor",
                        help="ajusta o corte pela transcricao, no navegador")
    ed.add_argument("video")
    ed.add_argument("--porta", type=int, default=8731)
    ed.add_argument("--sem-abrir", action="store_true")
    ed.set_defaults(fn=cmd_editor)

    v = sub.add_parser("previa", help="ve o corte no mpv sem renderizar")
    v.add_argument("video")
    v.add_argument("--ignorar-transcript", action="store_true")
    v.set_defaults(fn=cmd_previa)

    ct = sub.add_parser("cartas",
                        help="adiciona overlay de carta + reenquadramento; "
                             "gera cartas.json e uma previa lo-res")
    ct.add_argument("video")
    ct.add_argument("--carta", default=None,
                    help="carta principal (ex.: '7 de espadas'); "
                         "sem isto, usa a mais mencionada na fala")
    ct.add_argument("--force", action="store_true",
                    help="refaz o rastreio de rosto mesmo se ja tiver cache")
    ct.add_argument("--sem-abrir", action="store_true",
                    help="gera a previa mas nao abre o mpv sozinho")
    ct.set_defaults(fn=cmd_cartas)

    r = sub.add_parser("render")
    r.add_argument("video")
    r.add_argument("--out", default=None)
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--ignorar-transcript", action="store_true",
                   help="usa so as propostas automaticas, ignora transcript.md")
    r.set_defaults(fn=cmd_render)

    p = sub.add_parser("run")
    p.add_argument("video")
    p.add_argument("--force", action="store_true")
    p.add_argument("--model", default=None)

    def _todos(a):
        for _, fn in ETAPAS[:-1]:
            fn(a)
    p.set_defaults(fn=_todos)

    b = sub.add_parser("lote", help="processa uma pasta inteira")
    b.add_argument("pasta")
    b.add_argument("--ate", default="draft",
                   choices=[n for n, _ in ETAPAS],
                   help="etapa final (padrao: draft, para revisar antes)")
    b.add_argument("--pular", default=None,
                   help="ignora arquivos cujo nome contenha estes trechos, "
                        "separados por virgula")
    b.add_argument("--force", action="store_true")
    b.add_argument("--model", default=None)
    b.add_argument("--ignorar-transcript", action="store_true")
    b.set_defaults(fn=cmd_lote)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
