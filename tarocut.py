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

import pastas

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work"
# midias avulsas (fotos, graficos - NAO cartas do baralho) que o roteiro
# escrito a mao pode por na coluna da direita ou em tela cheia. So entra no
# autocomplete e no export o que estiver aqui dentro - "sera impossivel
# adicionar uma imagem que nao esteja nessa pasta" (pedido do Rafael,
# 09/09/2026). Ver catalogo_midias().
MIDIAS = ROOT / "midias"
EXT_MIDIA = {".jpg", ".jpeg", ".png", ".webp"}

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
RE_LETRA_LONGA = re.compile(r"([^\W\d_])\1{3,}")


def eh_alongada(txt):
    """Tem letra repetida 3+ vezes? ('queeee', 'ééé', 'naaao')

    ACHADO NAO CONSERTADO (04/09/2026), medido nos 7 fluxos de work/: o
    `.` das reticencias tambem e um caractere repetido 3 vezes, e e ele
    que dispara 7 dos 8 cortes por `alongamento` do corpus - "ehm...",
    "as...", "mais...", "são...", "a...", "ás...". So UM veio de letra
    esticada de verdade (o "ééé" de 20,5s do "as de espadas"). Ou seja: a
    regra que este docstring descreve quase nunca e a regra que roda, e
    ela apaga palavra de conteudo ("mais", "são") por causa de como o
    Whisper pontuou.

    Nao foi mexido aqui de proposito: e decisao do `analyze`, entao mudar
    a exige re-analisar todo video: os `transcript.md` perdem o selo e a
    camada de texto apagado a mao se perde junto. E as tres palavras que
    voltariam no IMG_0248 mudam um video de calibracao - isso quer ouvido,
    nao regex."""
    return bool(RE_ALONGAMENTO.search(sem_acento(txt).lower()))


def encurta_alongamento(txt):
    """Corrida de letra repetida vira TRES letras: "Eeeeee…" de 889
    caracteres vira "Eee…".

    E so a forma de ESCREVER, e existe por causa da tela. O Whisper
    escreve uma vogal sustentada repetindo a letra ate o teto de tokens
    dele - 889 caracteres no re-transcrito de lacuna, 200 no "ééé" de
    20,5 s do "as de espadas" -, e esse texto ia inteiro para o
    transcript.md, para o editor e para a legenda, onde uma palavra so
    passava da borda da tela (achado do Rafael, 04/10/2026). Tres letras
    continuam dizendo "isto e alongado" para `eh_alongada` e para o
    lexico de cacoetes ("ééé", "eee", "queee"), e `letras_uma_vez` ja
    contava a corrida como um som so.

    So LETRA: a corrida de digito e numero ("10000"), e a de ponto e
    reticencia."""
    return RE_LETRA_LONGA.sub(lambda m: m.group(1) * 3, str(txt))


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


def letras_uma_vez(txt):
    """`letras_faladas` contando corrida de letra repetida como UM som.

    "ééééé..." com 200 caracteres nao sao 200 letras faladas: e UMA vogal
    sustentada, e 20,5s dela e o tempo de um som so. Sem isto qualquer
    medida de tempo contra texto se deixa enganar pelo proprio texto que
    esta julgando - o "éé..." de 20,5s do "as de espadas" dava 9,7
    caracteres por segundo, ritmo de fala fluente, e nada tinha o que
    estranhar num corte de 27,5s.

    Fica SEPARADA de `letras_faladas` de proposito, e nao e um conserto
    dela: aquela decide CORTE (a cauda que `marca_arrastadas` poda) dentro
    do `analyze`, esta decide PERGUNTA. Trocar a de baixo mudaria o corte
    de todo video ja analisado, e isso e medicao que ninguem fez."""
    return letras_faladas(RE_ALONGAMENTO.sub(lambda m: m.group(1), txt))


def ritmo_mediano(st):
    """Segundos por letra falada NESTE video, ou None se ha pouca palavra.

    Cada pessoa fala num ritmo, e a mediana se adapta sozinha. Mora aqui,
    e nao dentro de `marca_arrastadas`, porque duas partes precisam dela:
    a poda de cauda, que corta, e a pendencia `texto nao explica o tempo`,
    que pergunta. Duas medianas calculadas em dois lugares seriam dois
    ritmos diferentes para o mesmo video."""
    import statistics
    pal = [t for t in st if t["k"] == "palavra" and chave(t["t"])]
    if len(pal) < 20:
        return None
    return statistics.median((t["e"] - t["s"]) / letras_faladas(t["t"])
                             for t in pal)


def densidade_fala(txt, dur):
    """Caracteres falados por segundo. Fala normal da ~13; fala lenta com
    pausa dentro cai para 4-6; vocalizacao ("Ous!" em 2,1s) fica perto de
    1,4. E o criterio para separar fala de ruido."""
    return len(chave(txt).replace(" ", "")) / max(0.1, dur)


def limiar_voz(db, offset_db):
    """Piso de ruido do proprio audio + folga = limiar de fala, em dB.

    O piso e o da SALA, e audio zerado nao e sala. MEDIDO no "modulo de
    tiragens 2": os ultimos 614 s (36% do arquivo) sao o int16 parado em
    1 passo, -91,0 dB constante, e o percentil 10 caiu ali dentro - limiar
    de -108 dB, e 100% da aula passava por voz (56% com o piso da sala).
    Quadro de ate 1 passo do int16 nao e microfone nenhum, entao nao entra
    na conta. Nao e numero de gosto, e o degrau do formato: a sala mais
    quieta dos 28 outros videos de work/ fica em -89 dB, e nenhum deles
    tem um quadro sequer nessa faixa - para eles o limiar sai identico."""
    import numpy as np
    sala = db[db > 20 * np.log10(1 / 32768)]
    return float(np.percentile(sala if len(sala) else db, 10)) + offset_db


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


def classifica_fala(txt, dur, sempre, ch):
    """O texto recuperado num trecho e cacoete ou e fala? -> (cortar, motivo)

    Usada por TODOS os caminhos que recuperam texto de um trecho: lacuna
    entre palavras, excesso de palavra com duracao impossivel, e o que
    vier depois.

    `ch` e o bloco [hesitacao] inteiro, e nao so a densidade: os outros
    dois numeros desta funcao - quantas palavras ainda cabem num cacoete e
    o que conta como palavra curta - moravam escritos no braco aqui, e
    numero que decide corte nao mora no codigo."""
    if not txt or not chave(txt):
        return True, "sem texto"
    dens = densidade_fala(txt, dur)
    if dens < ch["densidade_minima"]:
        return True, f"nao e fala ({dens:.1f} car/s)"
    toks = [chave_ac(w) for w in txt.split() if chave_ac(w)]
    if (len(toks) <= ch["recuperado_max_palavras"]
            and all(t in sempre or len(t) <= ch["palavra_curta_max"]
                    or eh_alongada(t) for t in toks)):
        return True, f"hesitação recuperada: {txt[:24]}"
    return False, f"fala nao transcrita: {txt[:26]}"


def hhmmss(t):
    h, r = divmod(int(t), 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def le_tempo(txt):
    """"1:29", "01:29.5", "0:01:29", "89" -> segundos. None se nao der.

    Uma conversao so de tempo escrito para segundo, porque ja houve duas: o
    `--em` do conferir tinha esta linha no corpo do comando, e o roteiro de
    cartas precisa dela de novo. Duas leituras do mesmo "1:29" e a mesma
    coisa dita duas vezes, e a que diverge calada e sempre a que ninguem
    esta olhando.

    O relogio de quem escreve "1:29" e sempre o da SAIDA - e o do .m4a, o da
    previa e o do video exportado. Quem traduz para a fonte e
    `_mapa_previa`, e ninguem mais."""
    p = str(txt).strip().split(":")
    if not p or len(p) > 3 or any(not x.strip() for x in p):
        return None
    try:
        return sum(float(x) * 60 ** i for i, x in enumerate(reversed(p)))
    except ValueError:
        return None


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def wdir(video):
    d = WORK / Path(video).stem
    d.mkdir(parents=True, exist_ok=True)
    return d


def jdump(obj, path):
    """Grava JSON de um jeito que nao pode deixar o arquivo pela metade.

    Escreve ao lado e troca com `os.replace`, que e atomico: ou o arquivo
    antigo esta la inteiro, ou o novo esta. Os arquivos que passam por
    aqui sao justamente os que este programa jura nao perder -
    `anotacoes.json`, `montagem.json`, `respostas.json`, `legendas.json` -,
    e `open(w)` TRUNCA antes de escrever: um travamento no meio, ou um
    `render` lendo no instante errado, deixava o arquivo vazio ou pela
    metade. A janela existe de verdade - o editor grava a cada gesto, e
    desde o modo cartas grava tambem a cada meio segundo de digitacao."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


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


GUIA = ROOT / "material" / "guia-taro-de-yala.md"
RE_PALAVRA = re.compile(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'-]*")


def carrega_vocabulario(d):
    """Todas as palavras que se sabe existirem: as que a propria aula usa,
    as do guia do Yala e as do lexico.

    Serve para UMA pergunta, e so no relatorio: quando o Whisper
    re-transcreve um trecho isolado e devolve texto, esse texto e uma
    leitura de confianca baixa. Se a leitura inventa uma palavra que nao
    existe em lugar nenhum do material do curso nem no resto da propria
    aula, quase sempre nao havia palavra ali - havia bocejo, resmungo ou
    eco de microfone.

    MEDIDO nos 10 fluxos de work/: dos tokens recuperados que o programa
    MANTEM, so quatro tem palavra fora deste vocabulario, e os quatro sao
    exatamente os suspeitos ja conhecidos - "despeçoso" (o bocejo do 7 de
    espadas, relatado como "ai que horror"), "Disse percebe", "Núrope" e
    o par disputado "Os pardes"/"Os pássaros". Nenhuma frase real da
    lista. Nos tokens ja cortados a taxa de alarme e alta (14%), e por
    isso a marca so vale para o que FICA: o que ja vai sair nao precisa
    de segunda opiniao.

    Isto NAO corta nada e nao pode cortar: um dicionario de 3 mil palavras
    tirado de um livro e da fala de dez videos nao e o portugues. E uma
    seta para o passe de julgamento olhar, do mesmo tipo que o [??]."""
    vocab = set()
    for arq in (GUIA, ROOT / "lexicon" / "terms.txt",
                ROOT / "lexicon" / "fillers.txt",
                ROOT / "lexicon" / "marcadores.txt"):
        if arq.exists():
            for w in RE_PALAVRA.findall(arq.read_text(encoding="utf-8")):
                k = chave(w)
                if k:
                    vocab.add(k)
    wp = d / "words.json"
    if wp.exists():
        for w in jload(wp).get("words", []):
            k = chave(w["w"])
            if k:
                vocab.add(k)
    return vocab


def fora_do_vocabulario(txt, vocab):
    """As palavras de `txt` que ninguem mais no material usa. Ignora as
    curtas: com 1-3 letras a chance de ser sigla, onomatopeia ou pedaco de
    palavra e alta demais para o aviso valer alguma coisa."""
    return [w for w in txt.split()
            if len(chave(w)) > 3 and chave(w) not in vocab]


def corrige_frases(words, frase):
    """Troca erros de varias palavras no lugar. Quando a correcao tem o
    mesmo numero de palavras, substitui uma a uma; quando nao tem, poe o
    texto inteiro no primeiro token e esvazia os demais - os tempos
    continuam certos de qualquer jeito.

    O token esvaziado e CONTINUACAO do primeiro, e mantido ele nao escreve
    nada no transcript.md. Quem le o texto de volta tem de saber disso: ate
    28/09/2026 o `reconcilia` o cortava como "apagado no texto", e o
    "-Smith" de "Waite-Smith" saia do video calado. Juntar os dois num
    token so seria mais limpo so no papel: os fluxos ja gravados continuam
    com o token vazio, e reanalisar tira um token do fluxo - toda decisao de
    palavra do `montagem.json` depois dele caducaria."""
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
    disfluencias devem ser transcritas, nao limpas.

    Testado e RECUSADO (04/10/2026): tirar daqui o exemplo "é... éé...".
    O exemplo e o que poe o Whisper em laco por cima de fala - no "as de
    espadas" saiu "Na sua forma mais... ééééé..." (200 letras) sobre 25 s
    de aula, e a mesma passagem sem o exemplo sai inteira e certa. Mas
    MEDIDO pela cadeia toda contra os videos editados a mao (mesmo codigo,
    4 videos), o prompt sem o exemplo e com o nome da carta saiu PIOR:
    46% de cartoes intocados contra 48%, 19 s a mais de sobra, erro de
    palavra de 11,7% para 14,7%. O laco acontece em 3 de ~60 videos e quem
    o resolve hoje e o `_texto_nao_cabe`, depois do fato. O Whisper
    `large-v3-turbo` tambem foi medido e perdeu (38% de cartoes): escreve
    "tá"/"pra" melhor, mas limpa as retomadas que este programa precisa
    ver para cortar. Quem quiser tentar de novo, meca pela cadeia toda."""
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


def encolhe_ao_som(words, voz, hop, C, med=None):
    """Tira da palavra o silencio que ficou colado na borda dela.

    A palavra que o alinhador RECUSA (`align_rejeitado`: deriva, score,
    ordem) volta para o tempo do Whisper, e o Whisper estica a palavra por
    cima da pausa vizinha: "Entao," do "5 de copas" saiu com 0,96 s, dos
    quais 0,6 s de silencio depois da palavra. Silencio dentro de palavra
    nao e pausa para ninguem - nem para o `analyze`, que so mede lacuna
    ENTRE palavras, nem para o `pausa_alvo`, que so encurta token de pausa.
    Ele chegava inteiro ao video.

    MEDIDO (04/10/2026) nas 52.217 palavras dos ~60 fluxos de work/: 445
    palavras com 0,3 s ou mais de silencio no fim (366 s) e 129 no comeco
    (97 s) - 98% delas recusadas pelo alinhador, quase todas entre 0,7 e
    0,9 s. Nos 40 videos preparados, o silencio de 0,7 s ou mais que
    SOBRAVA no corte morava dentro de palavra em 106 de 150 casos.

    Aqui a borda muda maior que `[silencio].borda_muda` sai da palavra, e
    o vao que ela vira passa pelas MESMAS regras de qualquer vao: abaixo de
    `pausa_intocavel` e respiro e fica, acima vira pausa e e encurtada. A
    palavra guarda `folga_palavra` do silencio de cada lado - e o erro de
    fronteira medido do alinhador, e cobre o "s" final que fica abaixo do
    limiar de voz.

    Palavra INTEIRA sem voz (7 em 52.217) fica como esta: ela esta no lugar
    errado, e encolher ao som nao tem para onde encolher.

    Palavra ARRASTADA tambem fica como esta, e isso foi medido do jeito
    ruim. A poda de cauda (`marca_arrastadas`) ja corta, de uma vez, o
    arrasto E o silencio colado nele ("daaas" + pausa). Encolhendo antes,
    a palavra deixava de parecer arrastada, a poda nao acontecia, e o vao
    que sobrava, menor que `pausa_intocavel`, ficava inteiro: nos 7
    primeiros videos editados isso devolveu ao corte 6 s de arrasto que o
    Rafael tinha tirado. `med` e o ritmo do video medido ANTES de
    encolher, o mesmo que a poda vai usar."""
    X = C["silencio"]["borda_muda"]
    folga = C["corte"]["folga_palavra"]
    ca = C["arrastada"]
    n = 0
    for w in words:
        dur = w["e"] - w["s"]
        if (med is not None and dur >= ca["min_dur"]
                and dur >= med * letras_faladas(w["w"]) * ca["fator"]):
            continue            # e da poda de cauda (ver acima)
        a, b = int(w["s"] / hop), int(w["e"] / hop)
        seg = voz[a:b]
        if len(seg) < 2 or not seg.any():
            continue
        ini = int(seg.argmax()) * hop             # primeiro quadro com voz
        fim = int(seg[::-1].argmax()) * hop       # quadros mudos no fim
        s, e = w["s"], w["e"]
        if ini >= X:
            s = round(w["s"] + ini - folga, 3)
        if fim >= X:
            e = round(w["e"] - fim + folga, 3)
        if (s, e) != (w["s"], w["e"]) and e > s:
            # campo extra, como `align_rejeitado`: o tempo de antes fica
            # gravado para quem quiser saber o que foi tirado e de onde.
            w["encolhida"] = [w["s"], w["e"]]
            w["s"], w["e"] = s, e
            n += 1
    return n

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

def resolve_suspeitos(stream, escuta, ch, sempre, db, hop, limiar, C=None):
    """Descobre o que ha nos trechos de duracao impossivel.

    Quando o Whisper deixa de transcrever alguma coisa, o alinhador nao tem
    onde por o audio sobrando e o gruda na palavra vizinha - 'principalmente'
    ficou com 4,67s escondendo um "nessas cartas" repetido. Aqui esse excesso
    e re-transcrito isolado e vira um token proprio, com texto de verdade,
    em vez de virar uma cauda cortada no escuro.

    Corta quando o que aparece repete o que vem depois (o Whisper colapsou
    uma repeticao) ou parece cacoete. Se for conteudo novo, PRESERVA - fala
    perdida nao pode virar corte silencioso.

    TRECHO LONGO que fica volta PARTIDO (`C` presente e o trecho com
    `[revisao].corte_por_texto_min` ou mais). E o caso do Whisper preso num
    "ééé..." por cima de fala (ver `_texto_nao_cabe`): no "as de espadas"
    sao 20 s de aula, com uma pausa de 5 s no meio. Um token so seria
    indivisivel - a pausa ficaria inteira no video - e as regras de texto
    que correm depois, calibradas em trecho de 1 a 2 s, cortariam o bloco
    inteiro por uma palavra repetida. E o mesmo problema da ponta do video,
    e a mesma resposta: `parte_ponta` + `poe_texto_na_ponta`, e cada pedaco
    de fala sai marcado para `protege_pontas`."""
    novos, n = [], 0
    for t in stream:
        if not t.get("suspeito"):
            novos.append(t)
            continue
        a, b = t["suspeito"]
        txt, lp, fatias = escuta(a, b)
        mot = alucinou(txt, b - a, lp, ch)
        t["e"] = a
        t.pop("suspeito", None)
        if eh_alongada(t["t"]) or chave_ac(t["t"]) in sempre:
            # a cabeca de um "ééé..." que o Whisper esticou por cima de
            # fala (ver `_texto_nao_cabe`): o que sobra dela e o proprio
            # cacoete, e sai como sairia sozinho.
            t["cut"], t["why"] = True, "alongamento"
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
            longo = (C is not None
                     and b - a >= C["revisao"]["corte_por_texto_min"])
            # trecho longo tem as pausas da fala dentro: a densidade se
            # mede contra o tempo FALADO, como na ponta (ver `ponta`)
            falado = (b - a) * (mede_voz(db, hop, a, b, limiar)[0]
                                if longo else 1.0)
            cortar, why = classifica_fala(txt, falado, sempre, ch)
            rot = encurta_alongamento(txt)   # o eco e decidido depois, por marca_ecos
            if longo and not cortar:
                l = {"s": a, "e": b, "texto": rot}
                for tipo, pa, pb, tx in poe_texto_na_ponta(
                        parte_ponta(l, db, hop, limiar, C), fatias):
                    if tipo == "sil":
                        novos.append({"k": "silencio", "t": "",
                                      "s": round(pa, 3), "e": round(pb, 3),
                                      "cut": True,
                                      "why": f"pausa {pb - pa:.1f}s"})
                    else:
                        novos.append({"k": "hesitacao",
                                      "t": encurta_alongamento(tx)
                                      or f"?? {pb - pa:.1f}s",
                                      "s": round(pa, 3), "e": round(pb, 3),
                                      "cut": False, "why": why,
                                      "ponta": "fala longa"})
                n += 1
                continue
        novo = {"k": "hesitacao", "t": rot, "s": round(a, 3),
                "e": round(b, 3), "cut": cortar, "why": why}
        if mot:
            # O QUE O WHISPER LEU antes de a recusa apagar a leitura. Fica
            # gravado porque o editor mostra isso no vao e oferece como
            # ponto de partida para escrever - sem esta linha, tudo o que
            # sobrava de uma leitura recusada aqui era o motivo da recusa.
            # As lacunas guardam o equivalente em `lacunas[].bruto`; estes
            # tokens nascem DEPOIS de elas serem medidas e nao tinham onde.
            # Nao entra no `selo_stream` (so k/s/e/cut), entao nao invalida
            # transcript.md nenhum.
            novo["ouviu"], novo["recusa"] = encurta_alongamento(txt), mot
        novos.append(novo)
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


def marca_instrucoes(stream, marcadores, ci):
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
    respiro, max_volta = ci["respiro"], ci["volta_max"]
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


def _parecida(a, b, razao):
    """Duas chaves que sao a MESMA palavra dita duas vezes, do jeito que o
    Whisper as escreve: igual, uma dentro da outra pela ponta ("tao" e
    "estao", "com" e "como", "esta" e "estar") ou quase igual
    ("movimento" e "monumento", "sincronicidade" e "sincronosidade").
    Duas leituras do mesmo som raramente saem identicas, e a retomada que
    so casasse por igualdade perdia justamente as que ela existe para
    achar."""
    if a == b:
        return True
    if min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a)
                                     or a.endswith(b) or b.endswith(a)):
        return True
    import difflib
    return (min(len(a), len(b)) >= 5
            and difflib.SequenceMatcher(None, a, b).ratio() >= razao)


# cacoete escrito com pontuacao colada ("É...", "é,", "E,") dentro do texto
# recuperado: e a marca de que o PROPRIO trecho e uma tentativa baguncada,
# e nao a frase refeita.
HES_PONTUADA = {"e", "é", "eh", "ah", "ahn", "hum", "hm", "ã", "ãh", "uh", "oh"}


def _rabisco(txt):
    """O trecho recuperado e ele mesmo uma tentativa baguncada? Duas
    reticencias ou mais ("essa... essa... que representa..."), um
    cacoete com pontuacao colada ("é uma, é, do que a..."), ou o mesmo par
    de palavras duas vezes dentro dele ("que é o limite, que é...") - ela
    gaguejando DENTRO do trecho, que entao nao e a frase refeita."""
    if txt.count("...") + txt.count("…") >= 2:
        return True
    ks = [chave(w) for w in txt.split() if chave(w)]
    pares = list(zip(ks, ks[1:]))
    if len(pares) != len(set(pares)):
        return True
    for w in txt.split():
        base = w.lower().rstrip(".,!?;:…")
        if base in HES_PONTUADA and base != w.lower():
            return True
    return False


def retomada_antes(stream, i, cc):
    """O trecho recuperado `stream[i]` e a frase REFEITA de algo que acabou
    de ser dito? -> lista dos indices da tentativa anterior, ou [].

    Mede quanto do texto recuperado repete, NA ORDEM, uma corrida de
    palavras que termina colada nele. O trecho pode abrir com ate 3
    palavras novas ("Quando a gente esta, mas o ponto e..." depois de
    "mas o ponto e,") e duas palavras podem divergir no caminho ("traz de
    um lugar subjacente" contra "traz de uma forma subjacente"). Palavra
    longa (7 letras ou mais) vale por duas: "elaborar" e "estrategica"
    repetidas sao a frase, "entao" e "sobre" repetidas sao cacoete."""
    t = stream[i]
    if _rabisco(t["t"]):
        return []
    rk = [chave(w) for w in t["t"].split() if chave(w)]
    antes = [k for k in range(max(0, i - cc["eco_janela"]), i)
             if stream[k]["k"] == "palavra" and chave(stream[k]["t"])]
    ak = [chave(stream[k]["t"]) for k in antes]
    if not rk or not ak:
        return []
    razao = cc["retomada_parecida"]
    melhor, j_ini = 0, None
    for i0 in range(min(4, len(rk))):
        for j0 in range(len(ak)):
            if not _parecida(ak[j0], rk[i0], razao):
                continue
            a, b, m, salto = i0, j0, 0, 0
            while a < len(rk) and b < len(ak):
                if _parecida(rk[a], ak[b], razao):
                    m += 2 if len(rk[a]) >= 7 else 1
                    a, b = a + 1, b + 1
                elif salto < 2 and b + 1 < len(ak) and _parecida(rk[a], ak[b + 1], razao):
                    b, salto = b + 1, salto + 1
                elif salto < 2 and a + 1 < len(rk) and _parecida(rk[a + 1], ak[b], razao):
                    a, salto = a + 1, salto + 1
                elif salto < 2:
                    a, b, salto = a + 1, b + 1, salto + 1
                else:
                    break
            # a tentativa abandonada e o que vem IMEDIATAMENTE antes da
            # retomada: a corrida tem que chegar ate a ultima palavra (ou a
            # penultima) antes do trecho recuperado.
            if len(ak) - b <= 1 and m > melhor:
                melhor, j_ini = m, j0
    if melhor < cc["retomada_min"]:
        return []
    return [k for k in antes[j_ini:] if not stream[k].get("prot")]


def marca_ecos(stream, cc):
    """Trecho recuperado que REPETE uma palavra vizinha e retomada.

    Passa sobre o fluxo inteiro, uma vez, para todo trecho de texto
    recuperado - venha ele de lacuna entre palavras ou de palavra com
    duracao impossivel. A regra ja existiu so num dos dois caminhos, e pelo
    outro escapava "como se [como seja...] existisse uma tradicao".

    `cc` e o bloco [cacoete] do config: `eco_janela` (quantos tokens de cada
    lado contam como "vizinho") e os dois numeros de `retomada_antes`. Sao
    eles que decidem se um trecho recuperado sai ou fica, e numero que
    decide corte nao mora no codigo.

    QUAL DAS DUAS COPIAS SAI. Ate 04/10/2026 saia sempre o trecho
    recuperado, e isso estava ao contrario na metade dos casos. Quando ela
    refaz uma frase ("que nao esta... que nao esta dominando"), o Whisper
    escreve a frase UMA vez; o alinhador encaixa essas palavras na PRIMEIRA
    tentativa, a abandonada, e a segunda - a boa, a que emenda na
    continuacao - sobra como audio sem texto, e e ELA que o
    `resolve_suspeitos` recupera. Cortar o recuperado era cortar a versao
    final e deixar a tentativa tocando.

    MEDIDO nas 78 retomadas colapsadas dos 22 videos editados a mao: o
    Rafael desfez 30 (69 s de fala), e em quase todas cortou a copia de
    ANTES no lugar. A regra nova - quando o recuperado repete pelo menos
    `retomada_min` palavras do que vem colado antes dele, fica o
    recuperado e sai a copia anterior - acerta 60 das 78, contra 32 da
    antiga. Dos 7 casos em que ela erra onde a antiga acertava, 6 sao
    repeticao identica ("esse aspecto" /
    "esse aspecto"), em que ficar com uma ou outra soa igual.

    Quando o recuperado repete so UMA palavra, ou o que vem DEPOIS dele,
    ou e ele mesmo um rabisco ("É... sobre...", "essa... essa..."), nada
    muda: ele e o cacoete e sai, como sempre saiu."""
    n = 0
    janela = cc["eco_janela"]
    for i, t in enumerate(stream):
        if t["k"] != "hesitacao" or t["cut"] or not chave(t["t"]):
            continue
        if t.get("ponta"):
            # fala recuperada longa (ponta do video, ou o "ééé..." de 20 s
            # que o `resolve_suspeitos` partiu): com dezenas de palavras,
            # acertar uma das 6 vizinhas e quase certo. `protege_pontas` a
            # devolveria de qualquer jeito; aqui ela nem leva a tentativa
            # de antes junto.
            continue
        viz = {chave(x["t"])
               for x in stream[max(0, i - janela):i + janela + 1]
               if x["k"] == "palavra" and chave(x["t"])}
        pals = [p for p in chave(t["t"]).split() if len(p) > 2]
        if not any(p in viz for p in pals):
            continue
        antes = retomada_antes(stream, i, cc)
        if antes:
            velho = " ".join(stream[k]["t"] for k in antes)
            for k in antes:
                if not stream[k]["cut"]:
                    n += 1
                stream[k]["cut"] = True
                stream[k]["why"] = f"tentativa antes da retomada: {t['t'][:26]}"
            # campo extra, fora do `selo_stream` (so k/s/e/cut): e o que
            # deixa a pendencia dizer QUAL copia saiu, para o passe de
            # julgamento conferir a troca em vez de redescobri-la.
            t["retomada_de"] = velho
            continue
        t["cut"] = True
        t["why"] = f"retomada colapsada: {t['t'][:26]}"
        n += 1
    return n


def _e_lacuna(t):
    return t["k"] in ("silencio", "hesitacao")


# Palavras cuja repeticao imediata e enfase deliberada, nao gagueira.
ENFASE = {"nao", "sim", "muito", "bem", "so", "ja", "mais", "menos",
          "devagar", "calma", "obrigada", "isso"}


def proxima_audivel(stream, i):
    """Indice da proxima palavra que a pessoa VAI OUVIR depois de `i`.

    Nao e `stream[i + 1]`: entre duas palavras cabem pausa e hesitacao ja
    marcadas para sair, e o que some nao separa nada para quem escuta. Foi
    por olhar so o vizinho imediato que sobreviveram "no ... no"
    (IMG_0248, 488,0s) e "um, ... um" (as de espadas, 557,3s) - entre as
    duas metades da gagueira havia um token cortado, e a regra concluia
    que nao eram vizinhas.

    Devolve None quando o que vem antes da proxima palavra FICA (pausa ou
    hesitacao mantida) ou quando a propria palavra seguinte ja esta
    cortada: nos dois casos elas nao ficam vizinhas no corte, e a pergunta
    nao se aplica."""
    j = i + 1
    while j < len(stream):
        t = stream[j]
        if t["k"] == "palavra":
            return None if t["cut"] else j
        if not t["cut"]:
            return None
        j += 1
    return None


def marca_cacoetes(stream, sempre, contextual, cc):
    """Marca cacoetes. Os ambiguos (é, e, então, né...) so caem quando
    estao ILHADOS entre pausas - senao cortariamos a conjuncao 'e' em
    'cavaleiros e cavaleiras' e a frase desmontaria.

    Duas passadas, e a ordem importa: a gaguejada pergunta qual e a
    proxima palavra AUDIVEL, e so depois da primeira passada as muletas
    do meio ja estao marcadas para sair."""
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
        elif k in contextual and ant is not None and pro is not None                 and _e_lacuna(ant) and _e_lacuna(pro):
            t["cut"], t["why"] = True, "muleta isolada"
        if t["cut"]:
            n += 1

    # Palavra identica repetida em sequencia ("cavaleiras, cavaleiras") e
    # gagueira, nao escolha. Corta a primeira e deixa a segunda; ENFASE
    # protege o uso retorico ("nao, nao").
    #
    # A comparacao e por `chave_ac`, COM acento. Sem isso "e é" (conjuncao
    # mais verbo) vira repeticao e o programa come a conjuncao - e o mesmo
    # motivo pelo qual `chave_ac` existe.
    #
    # NAO ha piso de tamanho de palavra. Ja houve (`len > 2`) e ele nao
    # protegia nada: as gagueiras que sobravam nos videos de calibracao
    # eram justamente "de de" (7 de espadas, 179,1s), "no no" (IMG_0248,
    # 488,0s), "um, um" (as de espadas, 557,3s) e "ou ou" (IMG_0247,
    # 355,9s), todas de uma ou duas letras. Quem faz o trabalho que o piso
    # fazia mal sao o acento e o [cacoete].gaguejada_max do config.
    #
    # MEDIDO nos 10 fluxos de work/: corta 6 tokens novos no total, e os 6
    # sao gagueira de verdade - nenhuma palavra de conteudo na lista.
    for i, t in enumerate(stream):
        if t["k"] != "palavra" or t["cut"] or not chave_ac(t["t"]):
            continue
        j = proxima_audivel(stream, i)
        if j is None or chave_ac(stream[j]["t"]) != chave_ac(t["t"]):
            continue
        if (chave(t["t"]) in ENFASE
                or stream[j]["s"] - t["e"] > cc["gaguejada_max"]):
            continue
        t["cut"], t["why"] = True, "gaguejada"
        n += 1
    return n

# ------------------------------------------ marcacao: tentativas de fala

def detecta_tentativas(stream, ct, min_ngram=None, respiro_gap=None):
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
    resolvido lendo o texto (comando `revisar`).

    `ct` e o bloco [tentativa] do config.toml - os quatro numeros moravam
    aqui como default de parametro, que e um segundo lugar decidindo gosto
    de corte. `min_ngram` sobrescreve o do config e existe por um motivo
    so: o `revisar` roda esta mesma varredura mais frouxa para PERGUNTAR,
    sem cortar nada (ver `cmd_revisar`).

    `respiro_gap` existe pelo mesmo motivo, e fecha um buraco que o
    `min_ngram` sozinho nao alcanca. Os filtros 1 e 3 procuram o respiro
    como TOKEN de lacuna, e pausa menor que `pausa_intocavel` (0,75s) nunca
    vira token - nao ha o que encurtar, entao o `analyze` nao registra
    nada. Para uma retomada refeita depressa, os dois filtros veem a
    repeticao grudada no meio da sentenca: a forma exata do paralelismo
    didatico que eles existem para proteger. Baixar `min_ngram` nao ajuda,
    porque o que barra nao e o tamanho da repeticao - MEDIDO no "3 de
    espadas", "que o amor e uma ferramenta para / que o amor e um caminho
    para" repete QUATRO palavras (acima do limiar de corte) e mesmo assim
    nao virou nem pergunta, separada por 0,50s de respiro.

    Com `respiro_gap`, o filtro 1 tambem aceita o respiro medido em TEMPO
    entre as duas palavras, e o filtro 3 aceita esse mesmo respiro como o
    ponto onde ela travou. MEDIDO nos 11 fluxos de work/: 8 cadeias novas,
    das quais 5 sao retomada de verdade - entre elas "nao e sempre que a
    saida para um problema e bater o peito, matar... nao e sempre que a
    saida para um problema de dificuldade e matar o problema no peito" (9
    palavras identicas, a primeira abandonada no meio) e "Em Yala, todas
    as... [0,74s] em Yala, todas as rainhas" do IMG_0247, que o CLAUDE.md
    ja registrava como retomada escapada: ela passa um centesimo abaixo do
    `pausa_intocavel`. Duas sao ANAFORA ("a gente tem os problemas, a gente
    tem as dificuldades") e uma e duvidosa.

    5 em 8 e bom para PERGUNTAR e pessimo para cortar, e por isso so o
    `cmd_revisar` passa este parametro. Cortar as duas anaforas seria o
    pior defeito deste programa."""
    min_ngram = ct["min_ngram"] if min_ngram is None else min_ngram
    janela_s, max_span = ct["janela"], ct["span_max"]
    max_palavras = ct["palavras_max"]
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
            if ant is None:
                continue
            por_gap = False
            if not _e_lacuna(ant):
                # Sem token de lacuna, o respiro so existe como tempo entre
                # as duas palavras. Quem corta nao olha para ele (ver o
                # docstring); quem pergunta, sim - e paga mais caro pelo
                # texto, porque respiro medido em tempo e evidencia mais
                # fraca que um token: aqui vale o `min_ngram` do CORTE,
                # nunca o do aviso. MEDIDO nos 11 fluxos: com o limiar do
                # aviso (2) isto traz +16 perguntas, e as que ele acrescenta
                # sao anafora ("nao e ignorar a dificuldade, nao e tipo
                # assim reprimir", "de uma passagem, de uma transicao") -
                # 5,4 perguntas por video, acima do teto de 4 que o config
                # registra. Com o limiar do corte sao +7, e 4 delas sao
                # retomada de verdade.
                if (respiro_gap is None or m < ct["min_ngram"]
                        or stream[idx[b]]["s"] - ant["e"] < respiro_gap):
                    continue
                por_gap = True
            achou = (b, m, por_gap)
            break
        if not achou:
            continue
        b, m, por_gap = achou
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
        # filtro 3: precisa existir o ponto onde ela travou. Quando o
        # filtro 1 achou o respiro pelo tempo, ele JA e esse ponto - e o
        # unico que existe, porque token de lacuna ali nao ha nenhum.
        if not por_gap and not any(_e_lacuna(stream[j])
                                   for j in range(ini, fim)):
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
    return classifica_fala(txt, lac["dur"], sempre, ch)


def carrega_respostas(d):
    """work/<video>/respostas.json - o que o passe de julgamento DECIDIU
    sobre cada pendencia do revisao.txt.

    Irmao do `anotacoes.json`, e pela mesma razao: a anotacao registra o
    que sai, e esta lista registra o que FICA e por que. Sem ela "responda
    toda pendencia" era regra do prompt do /revisar e de mais ninguem - o
    programa listava, numerava e ancorava cada `Pn`, e ninguem conferia se
    alguma tinha ficado sem resposta. Julgamento que ninguem cobra e
    julgamento que um dia nao acontece, e este e o programa em que uma
    pendencia sem resposta ja chegou ao Rafael como bocejo no audio.

        [{"em": 305.04, "resposta": "fica", "nota": "por que"}]

    `em` e o TEMPO da pendencia, o mesmo numero que o revisao.txt imprime
    na linha `Pn`. E o tempo, e nao o numero `n`, porque numero caduca: se
    uma pendencia do meio da lista some (foi cortada), todas as de baixo
    mudam de numero e as respostas passariam a valer para outra pergunta -
    a mesma armadilha do indice de token que o `confere_montagem` recusa.
    Tempo de palavra nao caduca.

    `em` aceita uma lista quando a mesma decisao vale para varias ("P2, P3
    e P5: som alto que e ela respirando fundo, ficam"), que e como o
    relatorio do /revisar ja pede.

    Recusa em vez de avisar, como o selo do transcript.md: um arquivo de
    julgamento que o programa nao entende e julgamento sumindo em
    silencio, que e o defeito que este arquivo existe para fechar."""
    p = d / "respostas.json"
    if not p.exists():
        return []
    try:
        dados = jload(p)
    except Exception as e:
        sys.exit(f"[respostas] {p} ilegivel ({e}). Conserte o arquivo: e "
                 f"julgamento humano, nao da para ignorar em silencio.")
    ok = []
    for r in dados if isinstance(dados, list) else []:
        em = r.get("em")
        em = em if isinstance(em, (list, tuple)) else [em]
        try:
            tempos = [float(x) for x in em]
        except (TypeError, ValueError):
            sys.exit(f"[respostas] {p}: 'em' tem de ser o tempo da "
                     f"pendencia (numero ou lista de numeros), e veio "
                     f"{r.get('em')!r}.")
        resp = str(r.get("resposta", "")).strip().lower()
        if resp not in ("sai", "fica", "ouvido"):
            sys.exit(f"[respostas] {p}: 'resposta' em {tempos} e "
                     f"{resp!r}; so vale 'sai', 'fica' ou 'ouvido'.")
        ok.append({"em": tempos, "resposta": resp,
                   "nota": str(r.get("nota", ""))})
    return ok


# `why` que so uma pessoa escreve ao MANTER: ela viu o token marcado para
# sair e clicou nele no editor. Manter de olhos abertos e resposta tao
# explicita quanto cortar - cobrar resposta escrita depois disso e
# perguntar duas vezes. Sem isto o `conferir` do IMG_0247 cobrava o
# 'As cartas' de 160,34s, que a pessoa tinha restaurado no editor.
#
# "resgatado" NAO entra, por mais que o nome prometa. Ele vem do
# `reconcilia`, para todo token que o transcript.md traz entre ⟨ ⟩ - e
# `texto_token` imprime ASSIM toda hesitacao e toda pausa MANTIDA, nao so
# as que alguem trouxe de volta. Aceita-lo dava "ja respondida" a todas as
# 8 pendencias do IMG_0247, inclusive as que ninguem tinha olhado.
MAO_MANTEM = ("restaurado no editor",)


def casa_resposta(q, resp, tol=0.05):
    """Qual resposta do `respostas.json` responde esta pendencia (indice na
    lista), ou None.

    Regra de UMA linha, mas mora aqui porque quem pergunta sao dois: o
    `revisar`, que marca na lista o que ja foi respondido, e o
    `pendencias_sem_resposta`, que cobra o resto. Dois lugares casando
    tempo com tolerancia propria seriam duas respostas diferentes para a
    mesma pergunta - o mesmo defeito de ter `minimo` no codigo e
    `fortes_min` no config.

    `tol` = 0,05s casa o tempo escrito a mao com o tempo da pendencia: o
    revisao.txt imprime com duas casas, entao o erro de copia e de
    arredondamento, nao de julgamento. Nao da para afrouxar muito: nos 10
    fluxos de work/ duas pendencias diferentes chegam a ficar a 0,13s uma
    da outra, e uma resposta que casasse com as duas responderia por
    engano uma pergunta que ninguem leu."""
    for k, r in enumerate(resp):
        if any(abs(t - q["s"]) <= tol for t in r["em"]):
            return k
    return None


def _mao_responde(q, iv, mao, tol=0.15):
    """O que sobrou tocando desta pendencia sobrou porque uma PESSOA mandou?

    Mesma pergunta, mesma forma e mesmo `tol` de `anotacoes_desobedecidas`,
    e pela mesma razao: trecho que continua no video porque alguem o trouxe
    de volta nao e alarme, e resposta.

    Existe porque o editor da onda restaura ARRASTANDO, e arrasto e trecho
    de TEMPO - nao escreve `why` em token nenhum, entao o `MAO_MANTEM` que
    `pendencias` consulta nao o enxerga. Sem isto, responder a pendencia
    pelo gesto mais direto que o editor oferece deixava a cobranca acesa
    para sempre, que e o contrario do que a cobranca existe para fazer.

    `vivo > tol` nao e detalhe de borda: sem ele a pendencia `cortada` - a
    unica que nasce sobre trecho JA cortado, e cujo `vivo` e zero - passaria
    trivialmente (0 >= -0,15) e sairia da lista sem ninguem ter respondido
    nada."""
    mao = _spans(mao)
    if not mao:
        return False
    vivo = sum(max(0.0, min(q["y"], b) - max(q["x"], a)) for a, b in iv)
    por_mao = sum(max(0.0, min(q["y"], b, y) - max(q["x"], a, x))
                  for a, b in iv for x, y in mao)
    return vivo > tol and por_mao >= vivo - tol


def pendencias_sem_resposta(pend, resp, iv, mao=None):
    """A pergunta que fecha o laco: sobrou pendencia sem resposta escrita?

    Mora aqui, uma vez so, do mesmo jeito e pela mesma razao que
    `anotacoes_desobedecidas`: quem pergunta sao o `conferir` (que lista) e
    o `_plano` (que avisa antes de gerar audio ou video). Regra de prompt
    depende de alguem lembrar; isto nao.

    Uma pendencia se responde de tres jeitos, e os tres contam:
      - CORTANDO o trecho - a resposta e o proprio corte, e por isso a
        pendencia que nao sobrevive ao `iv` nao e cobrada. A excecao e a
        `cortada` (`texto nao explica o tempo`), que ja nasce sobre trecho
        cortado: ali o corte nao responde nada, e quem responde e o
        restauro. Ver `parado` abaixo;
      - TRAZENDO de volta a mao um trecho marcado para sair - a pessoa
        olhou aquele token e disse fica. Vale pelas duas camadas do editor,
        e tem de valer: pelo `why` de `MAO_MANTEM`, quando ela clicou na
        PALAVRA, e por `mao` (os trechos de `spans_da_mao`), quando ela
        arrastou o TEMPO na onda. Ate 08/09/2026 so a primeira contava, e
        entao o editor nao tinha a segunda;
      - ESCREVENDO no respostas.json - unico jeito de dizer "fica" ou
        "vale ouvido" sobre o que ninguem tocou, que e o caso em que a
        decisao nao deixa marca em lugar nenhum.

    Escrever "sai" NAO corta nada - o `respostas.json` registra decisao,
    quem executa e a anotacao (ou o transcript.md, ou o editor). Uma
    resposta "sai" sobre trecho que continua tocando por isso nao conta
    como respondida: seria um jeito novinho de julgamento humano sumir em
    silencio, e este programa ja tem quatro. Ela volta com `falta`
    dizendo PENDENCIA NAO OBEDECIDA, irma da anotacao desobedecida.

    Essa cobranca so vale para a pendencia cujo trecho E o que sai
    (`proprio`, as duas de texto recuperado). Numa `retomada?` o trecho
    vai da primeira tentativa ate a versao boa, e cortar a primeira -
    exatamente o que se pede - deixa a segunda tocando: cobrar ali seria
    reclamar de quem obedeceu.

    Devolve (sem_resposta, orfas). Orfa e resposta que nao casa com
    pendencia nenhuma - tempo digitado errado, ou pendencia que ja saiu
    no corte depois de respondida. Nao e erro; e o que se olha quando a
    lista de sem-resposta nao diminui."""
    casadas = set()
    sem = []
    for q in pend:
        vivo = sum(max(0.0, min(q["y"], b) - max(q["x"], a)) for a, b in iv)
        # `cortada` inverte as duas contas acima, e so ela: a pergunta e
        # sobre audio que JA SAI ("o texto que justificou este corte nao
        # explica o tempo dele"), entao cortar nao responde nada - o corte
        # ja estava feito quando a pergunta nasceu. Responde-la e trazer o
        # trecho de volta ou escrever no respostas.json. Sem esta inversao
        # a pendencia nascia e sumia da cobranca no mesmo instante, que e o
        # defeito que o `pendencias_sem_resposta` inteiro existe para
        # fechar.
        # `parado` = ninguem mexeu neste trecho: ele continua tocando (o
        # normal) ou continua cortado (o `cortada`).
        parado = (vivo <= 0.15) if q.get("cortada") else (vivo > 0.15)
        k = casa_resposta(q, resp)
        if k is not None:
            casadas.add(k)
            quer = "fica" if q.get("cortada") else "sai"
            if resp[k]["resposta"] == quer and parado and q.get("proprio"):
                sem.append(dict(q, falta="PENDENCIA NAO OBEDECIDA"))
        elif (parado and not q.get("mao")
                and not _mao_responde(q, iv, mao)):
            sem.append(dict(q, falta="PENDENCIA SEM RESPOSTA"))
    orfas = [r for k, r in enumerate(resp) if k not in casadas]
    return sem, orfas


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



def _texto_nao_cabe(t, med, C):
    """Token que uma regra de TEXTO cortou num tempo que o texto nao
    explica. Mesma pergunta - e mesmos numeros - da pendencia `texto nao
    explica o tempo` (`[revisao].corte_por_texto_min` e `[arrastada].fator`),
    feita antes de o corte virar fato."""
    if med is None or not str(t.get("why", "")).startswith(CORTE_POR_TEXTO):
        return False
    dur = t["e"] - t["s"]
    return (dur >= C["revisao"]["corte_por_texto_min"]
            and dur >= med * letras_uma_vez(t["t"]) * C["arrastada"]["fator"])


def marca_arrastadas(stream, sempre, C):
    """Alongamento detectado por DURACAO, nao por letra repetida.

    O Whisper escreve "queeeee" como "que" e "ééé" como "e": o texto nao
    denuncia nada, mas a palavra ocupa varias vezes o tempo normal. Compara
    cada palavra com o ritmo mediano do proprio video (cada pessoa fala num
    ritmo; a mediana se adapta sozinha).

    Palavra de UMA letra arrastada e cacoete puro e sai inteira. Palavra
    maior tem so a CAUDA cortada, preservando o comeco - "em" arrastado por
    2,7s vira "em", nao vira nada. E o que um editor humano faz."""
    ca = C["arrastada"]
    med = ritmo_mediano(stream)
    if med is None:
        return 0
    pal = [t for t in stream if t["k"] == "palavra" and chave(t["t"])]
    n = 0
    for t in pal:
        if t["cut"] and _texto_nao_cabe(t, med, C):
            # Cortado por TEXTO ("éé..." de alongamento, cacoete do lexico)
            # num tempo que o texto nao explica: e o Whisper preso num laco
            # por cima de fala de verdade. MEDIDO no "as de espadas": um
            # "éé..." de 200 letras em 20,5 s, e por baixo dele "na sua
            # forma mais profunda, mais potente. Entao ela fala do
            # intelecto..." - transcrito inteiro quando o trecho e lido
            # sozinho, sem o prompt. Tambem no "9 de espadas" (26,4 s) e
            # na "rainha de copas" (15,6 s). Em vez de cortar no escuro, o
            # trecho vai para a MESMA re-transcricao isolada das outras
            # duracoes impossiveis, e o `resolve_suspeitos` decide pelo
            # que se ouvir ali. A pendencia `texto nao explica o tempo`
            # continua existindo para o que ainda sair por texto.
            esperado = med * letras_uma_vez(t["t"])
            t["cut"] = False
            t["suspeito"] = [round(t["s"] + esperado * ca["manter"], 3),
                             round(t["e"], 3)]
            t["why"] = (f"duracao impossivel ({(t['e'] - t['s']) / esperado:.0f}x): "
                        f"o texto {t['t'][:12]!r} nao explica "
                        f"{t['e'] - t['s']:.1f}s")
            n += 1
            continue
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


def protege_enumeracoes(stream, cn):
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
       silencio.

    Havia um terceiro, `minimo=3`, default de parametro que ninguem nunca
    passou: comprimento minimo da sequencia contando numero fraco. Era um
    segundo lugar decidindo o que `fortes_min` ja decide - exigir 3
    numeros INEQUIVOCOS ja exige 3 tokens -, e um segundo lugar que o
    config.toml nao alcancava. Baixar `fortes_min` sem saber dele nao
    frouxaria nada, o que e exatamente o tipo de surpresa que faz numero
    de corte morar em um lugar so."""
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
        if fortes >= cn["fortes_min"]:
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


def protege_naolidos(stream):
    """Token `??` e o programa dizendo que NAO SABE o que foi dito ali.

    `parece_frase` e `eh_hesitacao` ja decidiram MANTER esse trecho, e o
    comentario de `parece_frase` promete o motivo com todas as letras: "nao
    se corta no escuro - o trecho fica e aparece como [?? N.Ns] no
    revisao.txt para o passe de julgamento decidir". So que as passadas
    seguintes decidem por TEXTO - tentativa, eco, cacoete, gaguejada -, e
    num `??` texto e exatamente o que nao existe. Elas cortavam por cima da
    promessa.

    E o corte levava a PERGUNTA junto: a lista de pendencias pula token
    cortado (`if t["cut"] or t["k"] != "hesitacao"`), entao o trecho saia do
    video e do revisao.txt na mesma linha. Julgamento humano sumindo em
    silencio, por um caminho novo.

    MEDIDO (04/09/2026) nos 7 fluxos de work/: 20 tokens `??`, UM cortado
    por regra - 669,1s do "as de espadas", 1,13s marcado `tentativa`. Nos
    outros 19 esta guarda nao muda nada. Que ali havia fala de verdade nao e
    suposicao: o wav2vec2 do `align`, decodificado livre em vez de alinhado,
    ouve "...sica, e isso e t...". Foi a UNICA discordancia entre os dois
    reconhecedores nos 114 cortes automaticos de 1s ou mais dos 7 videos -
    nos outros 113 os dois ouvem a mesma vogal arrastada ou o mesmo silencio.

    Nao protege de `ela mandou cortar`: ali quem mandou foi ela, em voz
    alta, e o que ela quer fora pode muito bem ser o que ninguem leu. Nem de
    anotacao - `aplica_anotacoes` corre depois desta linha de proposito."""
    n = 0
    for t in stream:
        if not t["cut"] or not str(t["t"]).startswith("??"):
            continue
        # `ela mandou cortar` e `anotado:` sao GENTE, nao regra: uma
        # e ela em voz alta, a outra o passe de julgamento.
        # `aplica_anotacoes` corre depois desta linha, entao o
        # segundo nunca chega aqui pelo caminho normal - a linha
        # existe para que chamar esta funcao FORA de ordem (num
        # teste, num script de medicao) nao desfaca julgamento
        # humano, que e o defeito que ela propria existe para
        # fechar. Medido: sem ela, rodar a guarda sobre os
        # stream.json prontos de work/ ressuscitava 9 trechos que
        # uma anotacao tinha mandado cortar.
        if str(t["why"]).startswith(("ela mandou cortar",
                                     "anotado:")):
            continue
        t["cut"] = False
        t["why"] = f"protegido: nao lido ({t['why']})"
        n += 1
    return n


def parte_ponta(l, db, hop, limiar, C):
    """A ponta recuperada, partida em pedacos. -> [(k, ini, fim, texto)]

    Recuperar o texto da ponta devolveu a aula ao video, mas devolveu num
    BLOCO SO: os 74,4 s do "5 de espadas" viraram um token unico, com as
    pausas, as respiradas e as hesitacoes da gravacao dentro. E um bloco e
    indivisivel - nenhuma das regras de encurtar pausa alcanca o que esta
    dentro de um token, entao a previa ficou com quase um minuto de tempo
    morto que o resto do video nao tem.

    Aqui a ponta e partida pelo MESMO envelope que o programa ja usa no
    video inteiro, e por uma razao de principio: no meio do video quem diz
    onde ha pausa e o silencio medido entre duas palavras alinhadas; numa
    ponta nao ha palavra alinhada nenhuma, e a unica testemunha e o som.
    Cada silencio maior que `pausa_intocavel` vira um pedaco de pausa (e o
    `pausa_alvo` o encurta depois, como faz com qualquer outra); o que
    sobra entre eles vira pedaco de fala, e fica.

    DOIS numeros, os dois ja no config e os dois com o sentido que ja
    tinham. `[silencio].pausa_intocavel` decide o que e pausa - abaixo dele
    e respiracao e nao se toca. `[corte].ilha_minima` decide o que e
    piscada: a voz PISCA entre silabas, entao o envelope cru devolve
    lampejos de 0,04 s entre dois silencios longos, e emitir um token para
    cada um encheria o revisao.txt de "?? 0.0s". Ilha menor que ela e
    absorvida pela pausa vizinha, que e exatamente o que `intervalos` ja
    faz com ilha curta no resto do video.

    MEDIDO na abertura do "5 de espadas": 8 pedacos de fala e 8 de pausa,
    30,0 s no lugar de 74,4 s, sem perder a frase "Os cinco de espadas."

    O TEXTO nao decide fronteira nenhuma - so e distribuido depois, e por
    isso esta funcao nao le `fatias` para partir. Ver `poe_texto_na_ponta`."""
    cs, cc = C["silencio"], C["corte"]
    a0, b0 = l["s"], l["e"]
    voz = (db > limiar)[int(a0 / hop):int(b0 / hop)]
    if len(voz) < 2:
        return [("fala", a0, b0, l.get("texto", ""))]

    # 1. corridas cruas de voz/silencio, ja rotuladas: so o silencio LONGO
    #    parte a ponta. O curto e respiracao e fica dentro da fala.
    peds, k = [], 0
    while k < len(voz):
        j = k
        while j < len(voz) and voz[j] == voz[k]:
            j += 1
        ini, fim = a0 + k * hop, min(a0 + j * hop, b0)
        tipo = ("sil" if (not voz[k] and fim - ini > cs["pausa_intocavel"])
                else "fala")
        if peds and peds[-1][0] == tipo:
            peds[-1][2] = fim
        else:
            peds.append([tipo, ini, fim])
        k = j
    peds[-1][2] = b0

    # 2. ilha de fala menor que `ilha_minima` e piscada do envelope, nao
    #    fala: vira pausa e funde com as vizinhas. Repete ate estabilizar,
    #    porque absorver uma ilha pode encostar duas pausas.
    def funde(ps):
        out = []
        for r in ps:
            if out and out[-1][0] == r[0]:
                out[-1][2] = r[2]
            else:
                out.append(list(r))
        return out

    mudou = True
    while mudou and len(peds) > 1:
        mudou = False
        for i, (tipo, a, b) in enumerate(peds):
            if tipo == "fala" and b - a < cc["ilha_minima"]:
                peds[i][0] = "sil"
                peds = funde(peds)
                mudou = True
                break
    return [(t, a, b, "") for t, a, b in peds]


def poe_texto_na_ponta(peds, fatias):
    """Distribui as frases recuperadas pelos pedacos de fala da ponta.

    A regra que importa e a que NAO se ve: uma frase nunca cai num pedaco
    de PAUSA. Os tempos do Whisper nesta escala erram por dezenas de
    segundos - "Os cinco de espadas." vem datado em 0-10 s num trecho onde
    a voz so comeca em 1,82 s -, e atribuir pelo ponto medio jogava 2 das 4
    frases da abertura do "5 de espadas" dentro de um silencio. Silencio e
    um pedaco que SAI: a frase que caisse ali seria cortada junto, e o
    texto que o programa acabou de resgatar do escuro sumiria de novo, pela
    porta do lado. E o defeito desta casa, na versao mais irritante -
    aparecer no transcript.md e sumir do video.

    Entao a frase vai para o pedaco de FALA mais proximo do seu ponto
    medio, e nunca para tras do pedaco em que a frase anterior caiu: os
    tempos sao ruins, mas a ORDEM em que o Whisper devolve as frases e boa,
    e e ela que mantem o transcript.md legivel. Pedaco de fala que nao
    receba frase nenhuma sai como `?? N.Ns`, que e o rotulo que este
    programa ja usa para "ha voz aqui e ninguem leu"."""
    falas = [i for i, (t, a, b, _) in enumerate(peds) if t == "fala"]
    if not falas:
        return peds
    texto = {i: [] for i in falas}
    piso = falas[0]
    for a, b, frase in fatias or []:
        meio = (a + b) / 2
        cand = [i for i in falas if i >= piso] or falas
        alvo = min(cand, key=lambda i: 0.0 if peds[i][1] <= meio < peds[i][2]
                   else min(abs(meio - peds[i][1]), abs(meio - peds[i][2])))
        texto[alvo].append(frase)
        piso = alvo
    return [(t, a, b, " ".join(texto[i]).strip() if t == "fala" else "")
            for i, (t, a, b, _) in enumerate(peds)]


def protege_pontas(stream):
    """Regra de TEXTO nao desfaz a fala recuperada na ponta do video.

    Irma de `protege_naolidos`, e pela mesma razao dela, um passo adiante.
    La o trecho nao tinha texto e as regras decidiam sobre um texto que nao
    existe; aqui ele TEM texto, e o problema e o tamanho: a abertura e o
    fechamento podem ser um pedaco inteiro de aula, e as regras de texto
    que correm depois do `analyze` foram todas calibradas em trecho
    recuperado de 1 a 2 segundos.

    MEDIDO (05/09/2026) no "5 de espadas": a abertura de 74,4 s foi
    recuperada com "Os cinco de espadas. Essa carta tem uma dor, ela tem
    uma cena tao triste..." - a aula que o Rafael ouviu faltando - e
    `marca_ecos` a cortou inteira como `retomada colapsada`. A regra dela
    esta certa e e estreita: trecho recuperado que repete uma palavra
    vizinha e retomada. So que "vizinha" e uma janela de 6 tokens
    (`[cacoete].eco_janela`, calibrada no "como se [como seja...] existisse
    uma tradicao"), e um trecho recuperado de 74 s tem dezenas de palavras
    - acertar uma das 6 seguintes e quase certo. A regra nao errou de
    logica; foi aplicada num tamanho em que ninguem a mediu.

    O mesmo vale para `marca_cacoetes` e `marca_tentativas`, que decidem
    pelo mesmo texto. Por isso a guarda e por TOKEN DE PONTA, e nao por
    regra: sao tres passadas com o mesmo defeito e uma pergunta so.

    Nao protege de `ela mandou cortar` - ali quem mandou foi ela, em voz
    alta, e uma introducao inteira e justamente onde ela diz "nao, corta
    essa parte". Nem de anotacao: `aplica_anotacoes` corre depois desta
    linha, de proposito, como na irma."""
    n = 0
    for t in stream:
        if not t.get("ponta") or not t["cut"]:
            continue
        if str(t["why"]).startswith(("ela mandou cortar", "anotado:")):
            continue
        t["why"] = f"protegido: {t['ponta']} recuperada ({t['why']})"
        t["cut"] = False
        n += 1
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

def edicao_a_mao(d):
    """O que, neste video, uma pessoa ja decidiu no editor e que um
    `analyze` novo deixaria sem chao. -> lista de motivos, vazia se nada.

    O `analyze` reescreve o `stream.json` inteiro, sempre, e nao e
    reproduzivel. Tudo o que o editor guarda POR PALAVRA (`montagem.json`
    `tokens`, `correcoes.json`) aponta para indice de token, e o
    `confere_*` recusa a partir dai; a legenda reescrita (`legendas.json`)
    casa pelo tempo do cartao, que muda junto. Nada se perde calado - mas
    o video para de exportar ate alguem refazer o trabalho, e para quem
    editou isso E perder o trabalho (pedido do Rafael, 04/10/2026: "nao
    me faca perder o que eu ja editei").

    Os trechos de TEMPO do `montagem.json` (`cortes`, `restauros`) nao
    entram: sobrevivem a re-analise por construcao, e o proprio passe de
    julgamento os escreve no meio do laco de `analyze --force`."""
    motivos = []

    def itens(nome, chave):
        p = d / nome
        if not p.exists():
            return 0
        try:
            return len(jload(p).get(chave) or [])
        except Exception:
            return 1          # ilegivel tambem e trabalho de alguem
    if itens("montagem.json", "tokens"):
        motivos.append(f"{itens('montagem.json', 'tokens')} palavras "
                       f"decididas no editor (montagem.json)")
    if itens("correcoes.json", "itens"):
        motivos.append(f"{itens('correcoes.json', 'itens')} correcoes de "
                       f"texto (correcoes.json)")
    if itens("legendas.json", "itens"):
        motivos.append(f"{itens('legendas.json', 'itens')} legendas "
                       f"reescritas (legendas.json)")
    rot = d / "cartas-roteiro.json"
    if rot.exists():
        try:
            if jload(rot).get("pronto"):
                motivos.append("marcado 'pronto para exportacao' no modo "
                               "cartas")
        except Exception:
            pass
    return motivos


def cmd_analyze(a):
    import numpy as np
    from faster_whisper import WhisperModel
    C = cfg()
    cs, ch, cw = C["silencio"], C["hesitacao"], C["whisper"]
    d = wdir(a.video)
    ja = edicao_a_mao(d) if (d / "stream.json").exists() else []
    if ja and not getattr(a, "apesar_da_edicao", False):
        sys.exit("[analyze] RECUSADO: este video ja foi editado a mao -\n    "
                 + "\n    ".join(ja) + "\n"
                 "Refazer a analise trocaria as palavras por baixo dessas "
                 "decisoes, e o video pararia de exportar ate tudo ser "
                 "refeito. Nada foi alterado. Se e isso mesmo que voce "
                 "quer, rode `analyze` com --apesar-da-edicao.")
    words = jload(d / "words.json")["words"]
    dur_total = jload(d / "meta.json")["dur"]
    db, hop, dur_wav = envelope(d / "audio.wav")

    limiar = limiar_voz(db, cs["limiar_fala_db"])
    voz = db > limiar
    print(f"[analyze] limiar de fala={limiar:.1f}dB  "
          f"{100*voz.mean():.0f}% dos frames com voz", flush=True)
    # antes de medir qualquer lacuna: o silencio que mora DENTRO de uma
    # palavra so vira lacuna - e pausa encurtavel - se sair dela primeiro.
    med_bruto = ritmo_mediano([{"k": "palavra", "t": w["w"], "s": w["s"],
                                "e": w["e"]} for w in words])
    n_enc = encolhe_ao_som(words, voz, hop, C, med_bruto)
    if n_enc:
        print(f"[analyze] silencio tirado da borda de {n_enc} palavras",
              flush=True)

    # --- classifica lacunas -------------------------------------------
    def mede_lacuna(i, ini, fim):
        """Uma lacuna medida. `i` e a palavra ANTERIOR a ela (ver `porlac`).

        `pico` vai junto da fracao porque a fracao sozinha nao distingue
        folheio de carta (muitos quadros, todos fracos) de fala. As duas
        medidas saem de `mede_voz`, a mesma que o resto do programa usa."""
        f, pico = mede_voz(db, hop, ini, fim, limiar)
        tipo = ("fala" if f > cs["frac_voz_fala"]
                else "silencio" if f <= cs["frac_voz_silencio"] else "misto")
        return {"i": i, "s": ini, "e": fim, "dur": round(fim - ini, 3),
                "voz": round(f, 3), "pico": round(pico, 1), "tipo": tipo}

    lacunas = []
    for i, (x, y) in enumerate(zip(words, words[1:])):
        g = y["s"] - x["e"]
        if g < cs["gap_min"]:
            continue
        lacunas.append(mede_lacuna(i, x["e"], y["s"]))

    # A ABERTURA (antes da primeira palavra) e o FECHAMENTO (depois da
    # ultima) sao lacunas como as outras, e ate 05/09/2026 eram o unico
    # lugar do programa que cortava sem NUNCA perguntar ao microfone. A
    # abertura saia daqui com `cut=True` escrito no braco ("abertura"); o
    # fechamento nem token tinha, era cortado la na geometria (passo 2 do
    # `spans_corte`). Nos dois, a unica pergunta feita era "a lacuna passa
    # de pausa_intocavel?" - enquanto a lacuna do MEIO, ao lado, ja media
    # voz, ja re-transcrevia isolada com vad_filter=False e ja protegia o
    # que ninguem conseguiu ler.
    #
    # O custo desse silencio: no "5 de espadas" o Whisper nao transcreveu
    # NADA nos primeiros 74,4s, e o Rafael ouviu ali mais de um minuto de
    # aula, com a frase "o 5 de espadas" dentro. Sumiu do corte sem `[??]`,
    # sem pendencia, sem nada - o pior defeito deste programa por definicao
    # propria, por um caminho que nenhuma das quatro guardas conhecidas
    # cobria, porque todas elas moram DEPOIS desta linha.
    #
    # MEDIDO (05/09/2026) nos 12 fluxos de work/, com a MESMA regra das
    # lacunas do meio: 7 das 12 aberturas e 6 dos 12 fechamentos dao
    # fala/misto - 13 pontas com voz, nao um caso isolado.
    #
    # `-1` e `-2` sao indices que `porlac` nunca procura (ele so usa
    # 0..len-2, porque a lacuna e sempre a que vem DEPOIS da palavra `i`):
    # quem emite estes dois tokens e o `ponta`, mais abaixo, e nao o laco
    # de fusao. O piso continua sendo `pausa_intocavel` - abaixo dele a
    # ponta nao e tocada hoje, entao nao ha corte cego que medir.
    ab = fe = None
    if words:
        if words[0]["s"] > cs["pausa_intocavel"]:
            ab = mede_lacuna(-1, 0.0, words[0]["s"])
            lacunas.insert(0, ab)
        if dur_total - words[-1]["e"] > cs["pausa_intocavel"]:
            fe = mede_lacuna(-2, words[-1]["e"], dur_total)
            lacunas.append(fe)

    alvo = [l for l in lacunas if l["tipo"] in ("fala", "misto")
            and l["dur"] >= ch["isolar_min"]]
    print(f"[analyze] {len(lacunas)} lacunas; {len(alvo)} com voz e "
          f"duracao suficiente -> re-transcricao isolada", flush=True)
    for l, nome in ((ab, "abertura"), (fe, "fechamento")):
        if l:
            print(f"[analyze] {nome}: {l['dur']:.1f}s, "
                  f"{l['voz']*100:.0f}% de voz -> {l['tipo']}", flush=True)

    # --- re-transcreve as lacunas com voz ------------------------------
    _cache = {}

    def escuta(s0, e0):
        """Re-transcreve um trecho isolado. Carrega o Whisper so na
        primeira chamada e reaproveita depois.

        Devolve `(texto, logprob, fatias)`. As fatias sao os segmentos do
        Whisper com os tempos ja na escala da FONTE, e existem para UMA
        coisa: saber qual frase vai em qual pedaco quando `parte_ponta`
        quebra uma ponta longa. NAO servem para decidir corte - os tempos
        do Whisper sao ruins por definicao (e a razao de o `align` existir),
        e nesta abertura de 74,4 s eles erram por dezenas de segundos: "Os
        cinco de espadas." sai datado em 0-10 s num trecho onde o envelope
        so acha voz a partir de 1,82 s. Quem decide onde cortar e o
        envelope; a fatia so diz o que estava sendo dito por ali."""
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
        fatias = [(s0 + g.start, s0 + g.end, g.text.strip())
                  for g in segs if g.text.strip()]
        return txt, lp, fatias

    for k, l in enumerate(alvo, 1):
        txt, lp, fatias = escuta(l["s"] - 0.10, l["e"] + 0.10)
        mot = alucinou(txt, l["dur"], lp, ch)
        l["bruto"] = txt
        l["rejeitado"] = mot
        l["texto"] = "" if mot else txt
        # so a PONTA precisa das fatias: e a unica lacuna que pode ser longa
        # o bastante para ser partida. Guardar em toda lacuna do meio so
        # engordaria o stream.json sem ninguem ler.
        if l["i"] in (-1, -2) and not mot:
            l["fatias"] = fatias
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
        # a corrida de letra que o Whisper escreve por uma vogal sustentada
        # nao passa daqui com mais de tres letras (ver `encurta_alongamento`)
        texto = encurta_alongamento(texto)
        stream.append({"k": kind, "t": texto, "s": round(s, 3),
                       "e": round(e, 3), "cut": cortar, "why": motivo})

    def ponta(l, nome, silencio):
        """Abertura e fechamento entram no fluxo pela MESMA decisao das
        lacunas do meio: texto recuperado vira token de verdade, voz alta
        que ninguem leu vira `??` (e `protege_naolidos` a segura mais
        adiante), e o resto cai no `silencio` - que continua sendo a
        decisao certa quando nao ha voz aproveitavel ali.

        O `silencio` vem de fora porque as duas pontas o fazem diferente, e
        as duas por causa da geometria. A abertura precisa do rotulo
        "abertura", que o `spans_corte` conhece pelo nome para comecar a
        emenda no zero. O fechamento nao emite token nenhum: quem corta o
        rabo do video e o passo 2 do `spans_corte`, e dar-lhe um token aqui
        encurtaria a cauda de todo video em 0,39 s sem nenhuma medicao
        pedindo isso. O que muda aqui e o que se DECIDE; a geometria de
        quem ja decidia certo fica onde estava."""
        if l is None or l["tipo"] == "silencio" or (l["tipo"] == "misto"
                                                    and not l.get("texto")):
            silencio()
            return
        if l.get("texto"):
            # A densidade da lacuna do meio vale aqui; o DENOMINADOR dela
            # nao. `densidade_fala` divide letras por DURACAO, e numa
            # lacuna do meio isso e honesto: ela e curta e e vocalizacao de
            # ponta a ponta. Uma ponta pode ser um pedaco inteiro de aula,
            # com as pausas da aula dentro - no "5 de espadas" sao 74,4 s
            # com 29% de voz, ou seja, 53 s de silencio no denominador. A
            # conta deu 2,4 car/s e `classifica_fala` cortou "Os cinco de
            # espadas. Essa carta tem uma dor, ela tem uma cena tao
            # triste..." como se fosse um "eeee" arrastado.
            #
            # DESLIGAR a regra aqui - o que se fez em 05/09 - foi consertar
            # o sintoma no lugar errado: a conta nao estava errada, o
            # divisor estava. Sem ela nada mais separava "Eu sei." de uma
            # aula inteira, e a ponta passou a ficar SEMPRE que houvesse
            # texto. O divisor honesto ja esta medido dentro do proprio
            # token: `voz` e a fracao de quadros em que o microfone ouviu
            # voz, entao `dur * voz` e o tempo FALADO.
            #
            # Nao ha chave nova no config.toml de proposito. Isto nao e uma
            # taxa nova de gosto - e a mesma `[hesitacao].densidade_minima`
            # de sempre, medida contra o tempo em que houve fala em vez do
            # tempo de relogio. Um segundo numero decidindo a mesma coisa e
            # exatamente o defeito que aquele arquivo existe para impedir.
            #
            # MEDIDO (08/09/2026) nas 24 pontas dos 12 fluxos de work/: 11
            # das 13 que chegam ao Whisper sao recusadas por `alucinou`
            # (frase-fantasma, loop repetido, logprob, denso demais) - e o
            # `alucinou` que filtra lixo nesta escala, nao a densidade.
            # Passam duas, as duas aberturas, e o divisor decide as duas
            # certo:
            #   "5 de espadas" 74,4 s, 29% de voz: 2,43 -> 8,42 car/s, e as
            #     ~40 palavras nao cabem em `recuperado_max_palavras`: FICA
            #     (e o minuto de aula que sumia calado).
            #   "6 de espadas"  8,8 s, 20% de voz: 0,57 -> 2,87 car/s, e ai
            #     "Eu sei." cai na regra seguinte - 2 palavras, as duas
            #     curtas: SAI. Sao 8,8 s de quase so silencio.
            falado = l["dur"] * l["voz"]
            cortar, motivo = classifica_fala(l["texto"], falado, sempre, ch)
            if cortar:
                # Cacoete: sai inteiro, num token so, e SEM a marca `ponta`.
                # A marca e o que faz `protege_pontas` desfazer corte de
                # regra; marcar aqui faria a guarda desfazer a decisao de
                # quem ela deveria proteger - o "Eu sei." sairia daqui
                # cortado e voltaria ao video 30 linhas adiante, calado, o
                # defeito de sempre com a guarda no papel de quem apaga.
                # Marca quer dizer "fala recuperada que sobreviveu ao
                # nascimento", e o nascimento e a unica decisao desta ponta
                # tomada com o divisor certo.
                add("hesitacao", l["texto"], l["s"], l["e"], True, motivo)
                return
            # FICA - e ai a ponta e PARTIDA. Um bloco unico de 74,4 s e
            # indivisivel: as regras de encurtar pausa nao entram dentro de
            # um token, e a previa do "5 de espadas" ficou com quase um
            # minuto de tempo morto que o resto do video nao tem. Partida
            # pelo envelope, cada pausa longa vira token proprio e o
            # `pausa_alvo` a encurta como encurta qualquer outra. MEDIDO:
            # 74,4 s viram 30,0 s, sem perder "Os cinco de espadas."
            por = poe_texto_na_ponta(parte_ponta(l, db, hop, limiar, C),
                                     l.get("fatias"))
            porque = (f"fala nao transcrita na {nome}: "
                      f"{l['voz']*100:.0f}% de voz em {l['dur']:.1f}s")
            for tipo, pa, pb, txt in por:
                if tipo == "sil":
                    # O rabo do video continua sendo do passo 2 do
                    # `spans_corte`: dar-lhe um token aqui encurtaria a
                    # cauda de todo video em 0,39 s sem medicao nenhuma
                    # pedindo isso. Ja a pausa que ABRE o video precisa do
                    # rotulo "abertura", que o `spans_corte` conhece pelo
                    # nome para comecar a emenda no zero em vez de deixar
                    # meio `pausa_alvo` de ar antes da primeira palavra.
                    if nome == "fechamento" and pb >= l["e"] - 1e-6:
                        continue
                    rot = ("abertura" if nome == "abertura" and pa <= 1e-6
                           else f"pausa {pb-pa:.1f}s")
                    add("silencio", "", pa, pb, True, rot)
                    continue
                add("hesitacao", txt or f"?? {pb-pa:.1f}s", pa, pb, False,
                    porque)
                # cada pedaco que fica carrega a marca, senao `marca_ecos` e
                # as irmas cortam pedaco por pedaco o que a guarda inteira
                # protegia quando era um bloco so.
                #
                # Mesma familia de `ouviu`/`recusa` e `cauda`: campo extra
                # no token, fora do `selo_stream` (so k/s/e/cut), entao nao
                # invalida transcript.md nenhum.
                stream[-1]["ponta"] = nome
            return
        # Sem texto a ponta e a lacuna do meio sao a mesma pergunta, e quem
        # responde e a mesma funcao. `eh_hesitacao` nao le mais a palavra
        # anterior - a trava "nunca depois de numero" saiu quando o `align`
        # passou a cobrir o numero falado inteiro -, e na abertura nao ha
        # palavra anterior nenhuma.
        cortar, motivo = eh_hesitacao(l, None, sempre, ch, limiar)
        rot = "hesitação" if cortar else f"?? {l['dur']:.1f}s"
        add("hesitacao", rot, l["s"], l["e"], cortar, motivo)

    if ab is not None:
        ponta(ab, "abertura",
              lambda: add("silencio", "", 0.0, ab["e"], True, "abertura"))

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

    if fe is not None:
        ponta(fe, "fechamento", lambda: None)

    n_cac = marca_cacoetes(stream, sempre, contextual, C["cacoete"])
    n_arr = marca_arrastadas(stream, sempre, C)
    if n_arr:
        print(f"[analyze] palavras arrastadas: {n_arr}")
    cadeias = detecta_tentativas(stream, C["tentativa"])
    n_tent = marca_tentativas(stream, cadeias)
    n_inst = marca_instrucoes(stream, carrega_marcadores(), C["instrucao"])
    if n_inst:
        print(f"[analyze] marcadores verbais de corte: {n_inst} tokens "
              f"(ela pediu para cortar)")
    n_susp = resolve_suspeitos(stream, escuta, ch, sempre, db, hop, limiar, C)
    if n_susp:
        print(f"[analyze] trechos de duracao impossivel resolvidos: {n_susp}")
    # o `escuta` acima escreve _clip.wav a cada trecho, e `resolve_suspeitos`
    # chama de novo depois desta linha; apagar antes dela deixava o arquivo
    # para tras em toda pasta de trabalho.
    (d / "_clip.wav").unlink(missing_ok=True)
    n_eco = marca_ecos(stream, C["cacoete"])
    if n_eco:
        print(f"[analyze] retomadas colapsadas: {n_eco} tokens cortados")
    n_prot = protege_enumeracoes(stream, C["contagem"])
    n_esc = protege_naolidos(stream)
    n_pta = protege_pontas(stream)
    anot = encaixa_anotacoes(stream, carrega_anotacoes(d))
    for a in anot:
        if a.get("ajustada"):
            print(f"[analyze] anotacao {a['ajustada']} encaixada em "
                  f"[{a['ini']:.2f}, {a['fim']:.2f}] (fronteira de palavra)")
    n_anot = aplica_anotacoes(stream, anot)
    print(f"[analyze] cacoetes marcados: {n_cac}")
    if n_prot:
        print(f"[analyze] contagens protegidas: {n_prot} tokens preservados")
    if n_esc:
        print(f"[analyze] cortes no escuro desfeitos: {n_esc} trecho(s) "
              f"`??` devolvidos ao video e ao revisao.txt - a regra "
              f"decidia por um texto que nao existe")
    if n_pta:
        print(f"[analyze] fala recuperada na ponta devolvida: {n_pta} "
              f"trecho(s) - a regra de texto decidia num tamanho em que "
              f"ninguem a mediu")
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


def le_superficie(txt):
    """Um pedaco do transcript.md -> [(chave, estado)], com estado em
    cortar/manter/normal.

    E o LEITOR do round-trip, e mora fora do `parse_transcript` porque o
    `reconcilia` precisa dele tambem: a chave de cada token do fluxo e lida
    da superficie que o `texto_token` escreveu, por esta mesma funcao. Um
    segundo leitor ali seria uma segunda opiniao sobre o que o draft quis
    dizer - e a que diverge calada e sempre a que ninguem esta olhando."""
    saida = []
    for m in TOKEN_RE.finditer(txt):
        cortar, manter, cru = m.groups()
        if cortar is not None:
            saida.append((_chave_superficie(cortar), "cortar"))
        elif manter is not None:
            saida.append((_chave_superficie(manter), "manter"))
        else:
            saida.append((chave(cru), "normal"))
    return saida


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
        saida += le_superficie(ln)
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
    vira corte; o que voltou para ⟨ ⟩ vira manutencao.

    A chave de cada token e lida da SUPERFICIE que o draft escreveu para
    ele, pelo mesmo leitor do transcript.md. Ate 28/09/2026 ela era
    calculada a parte, do texto do token, por uma `chave_token` - apagada,
    nao mudada de lugar: era uma segunda opiniao sobre o que o draft
    escreve. As duas davam a mesma coisa em quase todo token - MEDIDO nos
    38 fluxos de work/, 34.376 tokens, iguais em todos menos 2 - e os 2
    eram o defeito: palavra MANTIDA de texto vazio. Quando a correcao de
    frase muda o numero de palavras, `corrige_frases` poe a frase no
    primeiro token e esvazia o seguinte ("Wait" + "Smith," ->
    "Waite-Smith" + ""), e mantida ela nao escreve nada no transcript.md:
    la ela e so um espaco a mais. A chave a parte era "", o texto nao
    devolvia chave nenhuma, e sem par ela caia no laco de baixo e virava
    "apagado no texto" sem ninguem ter apagado nada. Os 2 eram o
    "-Smith" de "Waite-Smith": 0,68 s no "6 de paus" (153,20 s), que so
    voltou ao video por um restauro a mao, e 0,30 s no "modulo de tiragens
    3" (597,04 s), que sobrevivia por acaso - encolhido pelas duas
    `folga_palavra`, o corte ficava menor que `minimo`. O wav2vec2 do
    `align`, decodificado livre, ouve "smede" e "semej" ali: e fala.

    Token que nao escreve nada nao tem o que casar, e segue a PALAVRA
    anterior que escreveu, a que carrega o texto dele. O dono, e nao a
    proposta da analise: apagar "Waite-Smith" no texto e apagar a palavra
    inteira, e a proposta deixaria um "Smith" solto no video. Sem dono
    fica a proposta, que e tudo o que se sabe dele.

    O mesmo leitor fecha o caso irmao, que nenhum dos 38 tem ainda: token
    cuja superficie se le como VARIAS palavras. O lexico faz um assim de
    "tarodiala" e de "Tarou Diálamas" - "Tarô de Yala" num token so -, e
    pela chave a parte ele seria "taro de yala" contra "taro", "de", "yala"
    no texto: sumiria do video do mesmo jeito. Lido pela superficie, cada
    pedaco casa com o seu. Se os pedacos discordam (o texto deixa apagar
    metade de um token), manter ganha: o token nao se parte, e cortar
    tiraria a fala que alguem deixou escrita."""
    import difflib
    # o que o draft escreveu de cada token, lido de volta: 0, 1 ou N chaves
    lido = [[k for k, _ in le_superficie(texto_token(t))] for t in stream]
    ko, de = [], []
    for i, ks in enumerate(lido):
        ko += ks
        de += [i] * len(ks)
    ke = [k for k, _ in editado]
    sm = difflib.SequenceMatcher(None, ko, ke, autojunk=False)
    estados = {}
    for i, j, n in sm.get_matching_blocks():
        for k in range(n):
            estados.setdefault(de[i + k], []).append(editado[j + k][1])
    apagados, dono = 0, None
    for i, t in enumerate(stream):
        if not lido[i]:
            if dono is not None:
                t["cut"], t["why"] = dono["cut"], dono["why"]
            continue
        es = estados.get(i)
        if not es:
            t["cut"], t["why"] = True, "apagado no texto"
            apagados += 1
        elif "manter" in es:
            t["cut"], t["why"] = False, "resgatado"
        elif "normal" in es:
            t["cut"], t["why"] = False, ""
        else:
            t["cut"] = True
        if t["k"] == "palavra":
            dono = t
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
        # quem escreveu este texto foi uma PESSOA, e isso muda quem tem o
        # que perguntar sobre ele (ver `pendencias`). Nao entra no
        # `selo_stream`, que so olha k/s/e/cut.
        stream[dec["i"]]["mao_texto"] = True


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
    #
    #    "Depois da ultima palavra nao ha token nenhum" deixou de ser sempre
    #    verdade: o `analyze` mede o fechamento como mede qualquer lacuna, e
    #    quando ha voz legivel ali o trecho vira `hesitacao` de verdade. Por
    #    isso a conta soma a hesitacao MANTIDA. Sem ela este passo cortaria
    #    por cima da recuperacao, e a fala apareceria no transcript.md so
    #    para sumir do video na linha seguinte - o defeito de sempre, com
    #    uma etapa a mais no caminho.
    #
    #    MEDIDO nos 12 fluxos de work/ analisados pela versao antiga: a
    #    conta larga da EXATAMENTE o mesmo numero que a estreita nos 12, e
    #    por isso nenhum video ja analisado muda de corte por causa dela.
    if dur:
        ult = max((t["e"] for t in stream if not t["cut"]
                   and t["k"] in ("palavra", "hesitacao")), default=None)
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
    """Rede de seguranca final: nenhuma emenda encosta em palavra mantida
    - nem em token que uma pessoa mandou manter, seja ele qual for.

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
    # Palavra mantida, MAIS o token que uma PESSOA mandou manter, seja
    # qual for o `k`. A lista era so `k == "palavra"`, e por isso o
    # restauro a mao funcionava pela METADE: no "as de espadas" cinco
    # tokens seguidos foram restaurados dentro da mesma anotacao, os
    # quatro que eram palavra voltaram inteiros e o quinto - uma
    # `hesitacao` de 6,06s, que o `resolve_suspeitos` fez de um bloco de
    # audio so - sobrou 0,06s. E os 0,06 nem eram dele: era a folga
    # vazando da borda da palavra vizinha. Cinco cliques iguais, dois
    # destinos, e nada na tela dizendo qual foi qual - julgamento humano
    # sumindo pela metade, que e pior de perceber do que sumir inteiro.
    #
    # A anotacao entra no corte como TRECHO DE TEMPO (passo 4 do
    # `spans_corte`), fora da camada de palavra; este e o unico lugar que
    # escava token mantido de dentro de um trecho de tempo. Enquanto ele
    # so olhasse `k`, restaurar hesitacao ou silencio dentro de anotacao
    # nao podia fazer nada, por mais explicito que fosse o clique.
    #
    # `MAO_MANTEM` e estreito de proposito: e o `why` que SO uma pessoa
    # escreve ao restaurar (ver `aplica_montagem` e `Sessao._aplica`).
    # Proteger `hesitacao`/`silencio` em geral mudaria o corte automatico
    # de todo video; assim so muda onde alguem clicou. MEDIDO nos tres de
    # calibracao: IMG_0246 e IMG_0248 (sem montagem.json) saem byte a
    # byte iguais - 0 emenda, 0 token. O IMG_0247 tem duas `hesitacao`
    # restauradas a mao e muda em 63 ms: 71 emendas viram 70, porque a
    # emenda de 22 ms que sobrava dentro do "Mae! Mais do..." reaberto
    # deixa de existir (menos emenda e melhor), e o `cartas` oculto a mao
    # em 159,57 passa de 19 para 60 ms vivos - a folga de 0,06 que agora
    # defende o "As cartas" reaberto ao lado dele, a mesma conta que ja
    # vale entre duas palavras quaisquer.
    pal = [t for t in stream
           if not t["cut"] and not t.get("prot")
           and (t["k"] == "palavra" or t.get("why") in MAO_MANTEM)]
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


def grafo(iv, C, escala=None, fps=None, hw=False, w_fonte=None, h_fonte=None):
    """O grafo de corte puro (sem cartas). `hw=True` espera que [0:v] chegue
    em memoria QSV - decode com `-hwaccel qsv -hwaccel_output_format qsv`,
    ver `_corta_final`.

    Ate 09/09/2026 esta funcao nao tinha `hw` nenhum, e `cmd_render`
    mandava o MESMO grafo de software para a tentativa QSV e para a
    tentativa de software - a tentativa QSV falhava sempre (quadro de GPU
    batendo num `scale` que so entende memoria normal) e o render caia pro
    software calado. MEDIDO no IMG_0247: decode QSV e 4,71-5,1x mais rapido
    que software (132s contra ~1110s no arquivo inteiro).

    Sem `escala` (perfil "igual"): nada aqui pede pixel de verdade, entao o
    corte puro (so trim/concat) vai direto pro encoder QSV sem sair da GPU.
    Com `escala` (perfil "1080p30"): `scale_qsv` escala AINDA em memoria de
    GPU, e so entao `hwdownload` baixa o quadro ja pequeno - mesmo padrao e
    mesma medicao do `grafo_cartas` (baixar o quadro 4K 10 bits ANTES de
    escalar mediu quase 14min no IMG_0301, so para jogar fora pixel que ia
    ser descartado no scale)."""
    L = _grafo_corte(iv, C)
    if hw and escala:
        w_trab = (round(w_fonte * escala / h_fonte / 2) * 2
                  if w_fonte and h_fonte else -1)
        filtros = [f"scale_qsv=w={w_trab}:h={escala}",
                   "hwdownload", "format=p010le"]
        if fps:
            filtros.append(f"fps={fps}")
    elif hw:
        filtros = []
    else:
        filtros = []
        if escala:
            filtros.append(f"scale=-2:{escala}")
        if fps:
            filtros.append(f"fps={fps}")
    filtros.append("null")
    L.append("[vc]" + ",".join(filtros) + "[vo]")
    return "\n".join(L)


_ASPECTO_CARTA = None
_ASPECTO_IMG = {}


def aspecto_imagem(path):
    """Largura/altura de UMA imagem do overlay - carta ou midia -, medida
    no proprio arquivo e cacheada por CAMINHO, nao num numero so.

    `aspecto_carta()` basta para carta porque as 78 sao identicas (medido:
    0,5609 sem excecao, ver la). Midia nao tem essa garantia - cada arquivo
    de `midias/` pode ter a proporcao que quiser -, entao o cache tem de
    ser por Path. ffprobe, nao cv2, mesma razao de `aspecto_carta`: o
    `imread` do OpenCV nao abre caminho com acento no Windows.

    Existe porque `faixa_livre` tratava toda imagem da pilha como se fosse
    uma carta (aspecto 0,56 fixo) - ver o comentario la para a medicao que
    achou isso errado."""
    key = str(path)
    if key not in _ASPECTO_IMG:
        aspecto = 0.5609
        try:
            r = run([FFPROBE, "-v", "error", "-select_streams", "v",
                     "-show_entries", "stream=width,height", "-of", "json",
                     str(path)])
            st = json.loads(r.stdout)["streams"][0]
            aspecto = st["width"] / st["height"]
        except Exception:
            pass
        _ASPECTO_IMG[key] = aspecto
    return _ASPECTO_IMG[key]


def aspecto_carta():
    """Largura/altura da imagem de uma carta, medida no proprio arquivo.

    MEDIDO (09/09/2026) nas 78 de `tarodeyala-borda`: 0,5609 nas 78, sem
    uma unica excecao - o baralho e uniforme. Mesmo assim isto le o disco
    em vez de guardar 0,5609 no config: a faixa livre entre as duas cartas
    sai deste numero, e um baralho redigitalizado noutro formato mudaria a
    geometria do layout 3 em silencio.

    Por ffprobe, e nao por cv2: o `imread` do OpenCV nao abre caminho com
    acento no Windows, e ha `11 - a forca.jpg` na pasta. Uma leitura por
    processo, guardada aqui.

    Devolve o valor de referencia quando a leitura falha - errar a faixa
    livre por alguns por cento e melhor que nao exportar."""
    global _ASPECTO_CARTA
    if _ASPECTO_CARTA is None:
        _ASPECTO_CARTA = 0.5609
        try:
            import baralho
            img = next(iter(sorted(baralho.BARALHO.glob("*.jpg"))), None)
            if img:
                r = run([FFPROBE, "-v", "error", "-select_streams", "v",
                         "-show_entries", "stream=width,height", "-of", "json",
                         str(img)])
                st = json.loads(r.stdout)["streams"][0]
                _ASPECTO_CARTA = st["width"] / st["height"]
        except Exception:
            pass
    return _ASPECTO_CARTA


def faixa_livre(C, w, h, camadas=None):
    """(inicio, fim) da faixa de largura que a pilha NAO cobre, em fracao
    do quadro. E o espaco que sobra para a Mariana no layout 3, e e contra
    ele que `_janela_layout3` decide o reenquadramento.

    Sem `camadas` (o modo automatico `cartas`, so carta, ver `grafo_cartas`):
    a conta de sempre, UMA carta de cada lado. MEDIDO num quadro 16:9 com
    os numeros de hoje (`carta_altura` 0,85 e `carta_margem` 0,025): a
    carta cobre 0,268 da largura e sobram 0,414 no meio. Mexer em
    `carta_altura` aperta esta faixa e o enquadramento responde sozinho.

    Com `camadas` (o roteiro escrito a mao, que pode ter midia avulsa ao
    lado da carta - `parse_roteiro`): tratar toda imagem da pilha como se
    tivesse o aspecto da carta era ERRADO, e so nao doia enquanto so havia
    carta. MEDIDO no "3 de espadas" com a midia `cura-bh.jpg` (aspecto
    0,71, mais larga que a carta a mesma altura): a conta antiga dava
    0,29-0,71 (a faixa da carta), a ocupacao real era 0,37-0,63, e
    `_janela_layout3` saia com `l`=0,96 - praticamente o quadro inteiro,
    sem reenquadrar nada, porque achava que sobrava espaco que a midia ja
    tinha tomado. Com a largura certa, `l`=0,77: a Mariana volta a ficar
    entre as duas imagens.

    A ocupacao de cada lado e o MAIOR ocupante que aquele lado chega a ter
    - a pilha empilha para o centro (`prof * deslocamento`) e cada camada
    tem a largura da PROPRIA imagem, medida no arquivo dela
    (`aspecto_imagem`), nao um numero fixo. E por isso que a faixa pode sair
    ASSIMETRICA agora (carta de um lado, midia mais larga do outro) - o `a`
    e o `b` vem de contas independentes, cada um do seu lado."""
    cc, ce = C["cartas"], C["cartas"]["enumeracao"]

    def ocupa(lado):
        if camadas is None:
            larg = cc["carta_altura"] * h * aspecto_carta() / max(1, w)
            return cc["carta_margem"] + larg
        do_lado = [c for c in camadas if c.get("lado") == lado]
        if not do_lado:
            return cc["carta_margem"]
        return max(
            cc["carta_margem"] + c.get("prof", 0) * ce["deslocamento"]
            + cc["carta_altura"] * h * aspecto_imagem(_imagem_camada(c)) / max(1, w)
            for c in do_lado)

    a = min(0.49, ocupa("esquerda"))
    b = 1 - min(0.49, ocupa("direita"))
    return a, b


def _janela_layout3(resumo, margem, livre=(0.293, 0.707), margem_lateral=0.0):
    """A janela de crop (fracao 0-1 do quadro) que poe a Mariana no meio,
    entre as duas cartas - o layout 3.

    Ate 09/09/2026 esta funcao devolvia a janela que cobre o VAIVEM do
    rosto com folga, e nunca foi olhada porque o reenquadramento nao
    acontecia (ver `_reenquadra`). Na primeira previa em que aconteceu ela
    cortou a testa da Mariana e ainda a deixou encostada na carta. As duas
    perguntas certas nao sao "onde o rosto anda":

    QUE TAMANHO. A janela e a MAIOR possivel (o menor zoom) que ainda
    deixa o rosto dentro da faixa que as cartas nao cobrem - a conta e um
    sistema de tres desigualdades em `l`, resolvido aqui de cabeca porque
    tem solucao fechada. Contra `p05`/`p95` do rastreio, e nao min/max: um
    segundo de inclinacao maxima nao manda no quadro do video inteiro.

    Mas ANTES DISSO ela tem de caber a cabeca inteira, do `topo_min` ao
    queixo, com folga - e essa e a restricao que ganha quando as duas se
    contradizem. Rosto encostando na borda da carta e feio; rosto cortado
    e defeito.

    ONDE. Horizontal: dentro do intervalo que as desigualdades deixam, o
    meio. Vertical: NAO se centraliza o rosto - `cy` e o CENTRO dele, e uma
    janela montada em volta disso corta a testa. A folga se mede do
    `topo_min`, o ponto mais alto que a cabeca alcanca.

    "Rosto encostando na borda da carta e feio" ficou so no papel ate
    09/09/2026: a conta pegava o MAIOR `l` que ainda cabia o vaivem dentro
    de `livre`, o que deixa `p05`/`p95` tocando `a`/`b` em cheio sempre que
    essa e a desigualdade que manda - zero folga, so ausencia de corte.
    MEDIDO no "3 de espadas" depois do conserto de `faixa_livre` (ver la):
    com a largura certa da midia a Mariana passou a ser reenquadrada, mas
    o `p95` (0,7182) ainda tocava `b` (0,6338) na ponta - a mesma queixa,
    so que 8 pontos percentuais mais apertada. `margem_lateral` encolhe
    `livre` pelos dois lados ANTES de resolver as tres desigualdades, e
    dai em diante e o `a`/`b` de sempre - reaproveita a mesma conta, nao
    inventa uma quarta. Metade da faixa livre no maximo, para uma faixa
    curta nunca virar `a > b`.

    A janela sai com a MESMA fracao nos dois eixos, e e isso que preserva o
    16:9 quando `_reenquadra` a devolve ao tamanho do quadro.

    rosto.json antigo nao tem `cx_p05`, `topo_min` nem `alt_med`; ai vale
    min/max e um rosto de 0,18 do quadro, que e a ordem de grandeza desta
    cena. Rodar `cartas`/`exportar --force` refaz o rastreio com os campos
    novos."""
    cx0, cx1 = resumo.get("cx_min", 0.4), resumo.get("cx_max", 0.6)
    cy0, cy1 = resumo.get("cy_min", 0.3), resumo.get("cy_max", 0.5)
    p5 = resumo.get("cx_p05", cx0)
    p95 = resumo.get("cx_p95", cx1)
    alt = resumo.get("alt_med", 0.18)
    topo = resumo.get("topo_min", cy0 - alt / 2)
    a, b = livre
    # a folga entra AQUI, encolhendo a faixa livre antes de qualquer conta -
    # dali para baixo e o `a`/`b` de sempre, e as tres desigualdades passam
    # a resolver "cabe com X de sobra" em vez de "cabe raspando".
    fl = max(0.0, min(margem_lateral, (b - a) / 2 - 1e-6))
    a, b = a + fl, b - fl

    # o maior `l` que ainda permite um `x` valido (as tres desigualdades:
    # p95 nao passa de `b`, p05 nao fica antes de `a`, a janela cabe no
    # quadro)
    l = min(1.0, p5 / max(1e-6, a), (1 - p95) / max(1e-6, 1 - b))
    # ...mas nunca menor que o que a faixa livre exige para caber o vaivem
    l = max(l, (p95 - p5) / max(1e-6, b - a))
    # ...e nunca menor que a cabeca inteira com folga. Esta ganha das
    # outras duas de proposito.
    l = max(l, (cy1 + alt / 2) - topo + margem)
    l = min(1.0, l)

    lo = max(0.0, p95 - b * l)
    hi = min(1.0 - l, p5 - a * l)
    x = (lo + hi) / 2 if hi >= lo else min(max(lo, 0.0), 1.0 - l)
    y = min(max(0.0, topo - margem), 1.0 - l)
    return x, y, l, l


def rosto_atras_da_carta(doc, janela, livre):
    """Que fracao do tempo o CENTRO do rosto fica atras de uma carta, com
    este enquadramento. Zero e o ideal e nem sempre existe.

    Nao decide nada - o `exportar` imprime. Existe porque o enquadramento e
    a unica parte deste programa que nao tem como se justificar sozinha na
    tela: quem olha a previa ve um quadro, e nao sabe se aquele quadro e o
    melhor que a geometria permitia."""
    sv = (doc or {}).get("suavizado") or []
    x, _, l, _ = janela
    a, b = livre
    if not sv or l <= 0:
        return None
    fora = sum(1 for s in sv if not (a <= (s["cx"] - x) / l <= b))
    return fora / len(sv)


def saida_hlg(meta):
    """A saida deste video sai marcada BT.2020/HLG (a cor do iPhone)?

    Um lugar so porque a resposta serve a DUAS perguntas que tem de dar a
    mesma: que tags de cor o `_corta_final` escreve, e em que espaco de
    cor `_img_para_hlg` poe a imagem. Se as duas divergissem, o arquivo
    diria "isto e HLG" sobre uma imagem que nao e - que e exatamente o
    defeito de 09/09/2026 (ver `_img_para_hlg`), so que reintroduzido pela
    porta de tras."""
    return "hevc" in meta.get("vcodec", "")


def _img_para_hlg(hlg):
    """O que uma imagem sRGB (carta, midia) precisa atravessar para virar
    quadro de um video HLG/BT.2020. Devolve o trecho de filtro que vai
    depois do `scale` do ramo da imagem - vazio quando a saida e SDR.

    Converte as PRIMARIAS (bt709 -> bt2020) e NAO converte a curva. Sao
    dois botoes independentes, e so um deles estava errado; mexer nos
    dois de uma vez foi o erro de 09/09/2026.

    O defeito (achado do Rafael em 09/09/2026, no "3 de espadas"): "as
    cores das imagens estao muito gritantes, como se alguem tivesse
    aumentado a saturacao e o contraste". A causa nao era o `overlay`,
    era o ROTULO. O `scale` do ffmpeg converte matriz e faixa, e NAO
    converte primarias nem funcao de transferencia: os valores sRGB do
    JPEG entravam no arquivo intactos e saiam de la marcados BT.2020/HLG
    pelo `-color_trc arib-std-b67` que o `_corta_final` escreve. O player
    le "BT.2020" e estica para o gamut da tela - e o que estava dentro ja
    era sRGB. Dai o gritante.

    A primeira correcao converteu as duas coisas, primarias E curva, com
    `npl=203`. Resposta do Rafael: "as cartas estao acinzentadas,
    dessaturadas" - errou para o outro lado. O metodo e que estava
    errado: a conferencia decodificava o quadro com a INVERSA exata da
    conversao que tinha acabado de ser aplicada, e isso devolve o
    original por construcao. Ela nunca mediu o player, media a si mesma.
    Nao repita esse teste - ele passa sempre.

    MEDIDO (10/09/2026) com o unico instrumento que serve, o olho no
    player onde o defeito aparece: `teste-cor-5-variantes.mov` poe a
    mesma carta cinco vezes lado a lado, um caminho de cor em cada, e o
    Rafael escolheu a 5a. Valores GRAVADOS por cada caminho, medidos
    contra o JPEG original (saturacao HSV, luma 709, desvio do luma):

      nada (o de ate 09/09)        sat    0%   luma    0%   contr    0%
      primarias + curva, npl=203        -33%         -0%         -22%
      primarias + curva, npl alto       -37%        +28%          -8%
      so curva, npl=203                 -18%         -3%         -22%
      SO PRIMARIAS (esta)               -21%         +3%          -0%

    Por que so as primarias: o HLG foi feito para ser compativel com SDR
    - a curva dele e proxima o bastante da gama de uma tela comum para o
    sinal aparecer quase certo sem conversao nenhuma -, e um player de
    SDR aproveita isso: passa o valor pela curva da tela e converte so o
    gamut. Nesse caminho a imagem sRGB ja esta certa DE CURVA, e
    converte-la para HLG antes so faz o player aplicar a curva da tela
    sobre valor HLG - que e o que escurece e achata. O gamut, ao
    contrario, ele converte de verdade, e por isso e o unico que tem de
    sair pre-convertido: assim o estica do player cai de volta em cima da
    cor original.

    A prova de que essa e a leitura certa esta no proprio video: ele e
    HLG/BT.2020 do iPhone e aparece CERTO nesse player. O player e o
    instrumento de medida e o video e o controle; o que falta a imagem e
    o mesmo tratamento que o video ja recebe. E por isso os tags do
    `_corta_final` continuam dizendo HLG: quem e HLG de verdade e o
    VIDEO, e mexer no rotulo dele para acomodar a imagem seria mudar a
    cor do video, exatamente o que o pedido proibia.

    O que isto NAO cobre, e vale saber antes de estranhar: num player que
    aplique a EOTF do HLG de verdade (tela HDR, ou Windows com HDR
    ligado) a imagem sai para o outro lado, clara demais. Nao existe
    arquivo que acerte nos dois, porque a diferenca esta no player e nao
    no arquivo; existe a escolha de qual acertar, e ela e a tela em que o
    curso e julgado e assistido.

    Nao ha ponto livre nenhum aqui, e por isso nao ha nada disto no
    `config.toml`. O `npl_grafico = 203` que morava la era o ponto livre
    da conversao de CURVA; sem ela o `npl` nao decide mais nada (o zscale
    so o usa em HLG/PQ), entao foi apagado e nao mudado de lugar - numero
    que nao decide mais nada e o comeco de um segundo lugar decidindo.

    `tin=iec61966-2-1` (sRGB de verdade), nao `bt709`: JPEG e PNG sao
    sRGB, a curva do bt709 e outra, e aqui a entrada e a saida usam a
    MESMA curva - dize-la errado converteria curva sem querer, que e
    justamente o que esta funcao deixou de fazer.

    `format=gbrp16` antes do zscale: em RGB nao ha matriz para errar - o
    JPEG chega marcado `bt470bg`, e deixar essa conversao para o swscale,
    que sabe le-la, e mais seguro que redizer a matriz para o zscale.
    Depois, `m=bt2020nc:r=tv` fecha matriz e faixa aqui, de uma vez, em
    vez de depender de o `-color_range tv` da saida se propagar de volta
    pelo grafo.

    Vazio quando `hlg` e falso, e e isso que mantem o video SDR byte a
    byte como sempre foi - inclusive a previa lo-res, que sai em h264: la
    o arquivo ja e bt709 e a imagem sRGB ja esta certa dos dois lados.

    O PRECO, MEDIDO (10/09/2026, carta de 918 px de altura): o ramo da
    imagem sozinho cai de 190 para 131 fps, 1,45x. Esse ramo nunca foi o
    gargalo - 131 fps contra os 3 a 7 fps do pipeline inteiro -, e o que
    ele soma sao ~2,4 ms por quadro. Nao vale pre-converter a imagem em
    arquivo para economizar isso: seria um JPEG com valores BT.2020
    dentro que ninguem consegue reler nem conferir a olho, e uma segunda
    verdade sobre qual imagem entra no video."""
    if not hlg:
        return ""
    return (",format=gbrp16,"
            "zscale=pin=bt709:tin=iec61966-2-1:min=bt709:rin=full:"
            "p=bt2020:t=iec61966-2-1:m=bt2020nc:r=tv")


def _reenquadra(L, entrada, janelas, resumo, w, h, margem, livre,
                margem_lateral=0.0):
    """Poe a Mariana no CENTRO durante `janelas` (o layout 3) e devolve o
    rotulo de saida. Sem janela nenhuma, nao acrescenta filtro algum.

    O jeito obvio - um `crop` so, com `w`/`h` como expressao de `t` que
    alterna entre o quadro inteiro e a janela do rosto - NAO FUNCIONA, e
    foi assim que este programa desenhou carta por uma semana. MEDIDO
    (09/09/2026) com o proprio ffmpeg 9.0.1 deste projeto: um
    `crop=w='if(between(t\\,2\\,4),320,640)':h='...'` sobre um testsrc2 de
    640x360 sai 640x360 do primeiro ao ultimo quadro. O `crop` avalia `x` e
    `y` a cada quadro, mas `w` e `h` UMA vez, na configuracao do filtro (em
    t=0, quando a condicao ainda e falsa) - e nao poderia ser diferente:
    quadro de tamanho variavel nao passa por `overlay` nem por encoder. O
    docstring antigo dizia que "o ffmpeg 9.x reavalia sozinho quadro a
    quadro"; nao reavalia, e o reenquadramento nunca aconteceu. O `x`/`y`
    variavel tambem nao movia nada, porque com `w` igual a largura inteira
    o proprio filtro prende o `x` em 0.

    O que funciona e ter o quadro reenquadrado como uma SEGUNDA imagem, de
    tamanho constante, ligada por cima quando a janela manda: recorta a
    janela do rosto, devolve ao tamanho do quadro e sobrepoe com `enable`.
    Sai identico ao layout 1 fora das janelas (o overlay fica em
    passagem), e trocado por inteiro dentro delas.

    O PRECO e um `scale` por quadro do video inteiro, e nao so dentro das
    janelas: o ramo do `split` recebe todos os quadros, esteja o `overlay`
    ligado ou nao. E por isso que quem chama so passa `janelas` quando
    existe carta a direita de verdade - sem elas nao ha `split`, nao ha
    `scale` e nao ha custo nenhum.

    Como `_janela_layout3` devolve a MESMA fracao para os dois eixos, a
    janela tem o aspecto do quadro e voltar ao tamanho cheio nao distorce -
    e a razao de ela ser escrita daquele jeito la."""
    if not janelas:
        return entrada
    x, y, lw, lh = _janela_layout3(resumo, margem, livre, margem_lateral)
    # A janela pode SER o quadro inteiro - acontece quando a cabeca so cabe
    # assim (MEDIDO no IMG_0246: l = 1,00). Ai nao ha reenquadramento
    # nenhum a fazer, e montar split+crop+scale para copiar o quadro
    # custaria um `scale` por quadro do video para nao mudar um pixel.
    if lw > 0.995 and lh > 0.995:
        return entrada
    # par nos dois eixos: yuv420p nao aceita croma de meio pixel
    cw = max(2, int(lw * w) // 2 * 2)
    ch = max(2, int(lh * h) // 2 * 2)
    cx, cy = int(x * w), int(y * h)
    ativo = "+".join(f"between(t\\,{a:.3f}\\,{b:.3f})" for a, b in janelas)
    L.append(f"[{entrada}]split[rq0][rq1];")
    L.append(f"[rq1]crop=w={cw}:h={ch}:x={cx}:y={cy},"
             f"scale={w}:{h}:flags=bicubic,setsar=1[rqz];")
    L.append(f"[rq0][rqz]overlay=x=0:y=0:enable='{ativo}'[rqbase];")
    return "rqbase"


def grafo_cartas(iv, C, cartas, resumo_rosto, w_fonte, h_fonte,
                  secundarias_imgs, escala=None, fps=None, hw=False,
                  hlg=False):
    """Estende _grafo_corte com o overlay das cartas e o reenquadramento
    da Mariana - so os layouts 1 e 3 (ver escopo combinado no plano:
    zoom e empilhamento ficam de fora por enquanto).

    Geometria (posicao, tamanho) e a MESMA para toda carta, principal ou
    secundaria - so O QUE aparece e QUANDO mudam. Inputs do ffmpeg, por
    convencao de quem chama isto: 0=video, 1=imagem da carta principal,
    2+=uma por carta secundaria DISTINTA, na ordem de `secundarias_imgs`
    (lista de (nome, Path), sem repetir carta).

    Reenquadramento: `_reenquadra`, que alterna entre o quadro inteiro
    (layout 1) e a janela de `_janela_layout3` (layout 3) - troca dura,
    sem pan suave, porque so muda quando uma carta secundaria entra ou
    sai, e isso ja tem o fade dela para disfarcar o corte. Ate 09/09/2026
    isto era um `crop` so com `w`/`h` em expressao de `t`, e o
    reenquadramento nunca aconteceu - a medicao e a explicacao estao la.

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

    secs = cartas["secundarias"]

    def _janela_ativa(spans):
        # `between(t\,a\,b)`: a virgula precisa vir escapada mesmo dentro
        # de aspas - e assim que todo exemplo de expressao condicional do
        # ffmpeg com mais de um argumento escreve (o parser do filtro
        # separa por virgula ANTES do eval da expressao entender que ali
        # e argumento de funcao, nao fim de opcao).
        return "+".join(f"between(t\\,{s['ini']:.3f}\\,{s['fim']:.3f})"
                         for s in spans)

    base = _reenquadra(L, ult_base, [(s["ini"], s["fim"]) for s in secs],
                       resumo_rosto, w_trab, h_trab, cc["crop_margem"],
                       faixa_livre(C, w_trab, h_trab), cc["crop_margem_lateral"])

    altura_px = max(2, int(h_trab * cc["carta_altura"]))
    margem_px = int(w_trab * cc["carta_margem"])
    y_carta = (h_trab - altura_px) // 2
    fade = cc["fade"]

    cor = _img_para_hlg(hlg)
    L.append(f"[1:v]scale=-2:{altura_px}{cor},format=yuva420p,"
             f"fade=t=in:st=0:d={fade}:alpha=1[cartaP];")
    L.append(f"[{base}][cartaP]overlay=x={margem_px}:y={y_carta}[c0];")

    ult = "c0"
    for i, (nome, _) in enumerate(secundarias_imgs, start=2):
        janela = _janela_ativa([s for s in secs if s["carta"] == nome])
        prox = f"c{i - 1}"
        L.append(f"[{i}:v]scale=-2:{altura_px}{cor}[carta{i}];")
        L.append(f"[{ult}][carta{i}]overlay=x=W-{margem_px}-w:"
                  f"y={y_carta}:enable='{janela}'[{prox}];")
        ult = prox

    # escala/fps ja foram aplicados cedo (antes do crop) quando pedidos -
    # so falta rotular a saida.
    L.append(f"[{ult}]null[vo]")
    return "\n".join(L)


# Entrada de carta quando quem chama nao pede fade nenhum: curta o
# bastante para nao se distinguir do corte seco que sempre foi a entrada
# da carta secundaria neste programa (`grafo_cartas`), e longa o bastante
# para RELIGAR o alfa da camada antes da proxima ocorrencia dela - ver a
# nota comprida em `grafo_cartas_pilhas`. E forma de regra, nao taxa: por
# isso fica aqui e nao no config.toml.
FADE_POP = 0.05


def _agrupa_camadas(eventos):
    """Eventos (um por MENCAO, ou uma linha de roteiro) -> camadas de
    overlay (uma por POSICAO de pilha distinta - lado+profundidade+carta).
    Nao e a mesma coisa: a
    mesma carta (ou categoria) pode voltar a ocupar a mesma posicao de
    pilha numa sessao de enumeracao POSTERIOR (dentro de uma sessao so,
    nunca se repete - profundidade so cresce, nunca volta atras), e cada
    ocorrencia continua precisando do seu proprio fade de saida.

    Evento `tipo == "full"` (imagem em tela cheia, ver `parse_roteiro`) nao
    entra aqui - nao tem lado nem profundidade, e quem desenha essa camada
    e outra parte de `grafo_cartas_pilhas`, nao esta.

    Por que uma camada por posicao, e nao uma por ocorrencia (a primeira
    versao desta funcao): o `overlay` deste ffmpeg e caro por CAMADA,
    nao so por pixel - MEDIDO no IMG_0246, o unico video de teste deste
    modo: 26 eventos (uma camada cada, sem esta funcao) renderizaram a
    0,6 fps, quase 50 min para 2 min de previa em 360p/15fps. As mesmas
    26 mencoes cabem em so 11 posicoes de pilha distintas nesse video -
    menos camadas, menos overlay, previa em minutos como o resto do
    modo com principal.

    Cada ocorrencia de uma camada precisa "religar" o alfa para 1 antes
    do proprio fade de saida - sem isso, o `fade=t=out` da ocorrencia
    anterior deixaria a imagem transparente para sempre, inclusive na
    ocorrencia seguinte. `grafo_cartas_pilhas` faz isso encadeando
    um `fade=t=in` bem curto (nao e um fade de estilo, so restaura o
    alfa) antes de cada `fade=t=out` - por isso devolve a lista de
    ocorrencias por camada, nao so a janela unida."""
    camadas, ordem = {}, []
    for ev in eventos:
        if ev.get("tipo") == "full":
            continue
        chave = (ev["lado"], ev["prof"], ev["carta"])
        if chave not in camadas:
            camadas[chave] = {"lado": ev["lado"], "prof": ev["prof"],
                               "carta": ev["carta"],
                               "tipo": ev.get("tipo", "carta"),
                               "ocorrencias": []}
            ordem.append(chave)
        camadas[chave]["ocorrencias"].append(
            {"ini": ev["ini"], "fim": ev["fim"],
             "fade_ini": ev["fade_ini"], "fade_dur": ev["fade_dur"],
             # o roteiro escrito a mao pede fade macio tambem na ENTRADA
             # (`[cartas].fade`); a enumeracao automatica nao pede nada e
             # fica com o corte seco de sempre.
             "fade_in": ev.get("fade_in", FADE_POP)})
    # Ordenada por PROFUNDIDADE, e nao por ordem de chegada: quem chama
    # monta a cadeia de overlay nesta ordem, e a carta mais funda na pilha
    # (a mais perto do centro) tem de ser desenhada por cima.
    #
    # Na enumeracao automatica os dois criterios dao a mesma lista - la a
    # profundidade so cresce (esq0, dir0, esq1, dir1...), e este sort e
    # estavel. No roteiro escrito a mao a pilha DESCE quando a carta de
    # baixo some, e ai uma camada rasa pode nascer depois de uma funda:
    # pela ordem de chegada ela seria desenhada por cima e taparia a de
    # dentro, invertendo a ilusao de profundidade.
    return sorted((camadas[k] for k in ordem), key=lambda c: c["prof"])


def grafo_cartas_pilhas(iv, C, camadas, janelas_sessao, resumo_rosto,
                         w_fonte, h_fonte, imgs, cheios=None, imgs_full=None,
                         escala=None, fps=None, hw=False, hlg=False):
    """O overlay em PILHAS: sem carta fixa embutida, duas pilhas que
    crescem para o centro, e o crop do layout 3 nas janelas que quem chama
    disser.

    Dois caminhos chegam aqui, e chegam de proposito no MESMO lugar:
    `cartas --sem-principal` (as pilhas montadas pela fala, layout 4, ver
    `_agrupa_cartas_enumeracao`) e o roteiro escrito a mao no modo cartas
    do editor (ver `parse_roteiro`). Sao a mesma imagem com dois autores;
    dois lugares montando esse filter_complex seriam dois lugares decidindo
    onde a carta pousa, que e o mesmo defeito de ter dois lugares decidindo
    o corte - so se descobre desalinhado tarde, olhando.

    No caminho do roteiro, a carta PRINCIPAL nao e um caso especial daqui:
    ela e simplesmente a camada esquerda de profundidade 0 que dura o video
    quase inteiro. Quem sabe que ela e a ancora e o `parse_roteiro`.

    Correcao do Rafael em 02/09, contra frame de video editado a mao: a
    1a versao empilhava verticalmente com a carta pequena (0,32 de
    altura) para caber 4 sem estourar o topo do quadro - errado. A pilha
    e HORIZONTAL: a carta usa o MESMO tamanho do layout 1 (`cartas.
    carta_altura`, quase borda a borda), e cada mencao nova desliza para
    o CENTRO em vez de subir - a pilha da esquerda cresce a partir da
    borda esquerda, a da direita a partir da borda direita. Altura
    constante (`y_base`), so o X muda com a profundidade.

    `camadas` vem de `_agrupa_camadas` (uma por POSICAO de
    pilha, nao por mencao - ver a nota la sobre por que isso importa
    para o tempo de render). `imgs` e um Path por camada, na MESMA
    ordem de `camadas` - convencao de quem chama isto: input 0=video,
    1..N=uma imagem por camada, nessa ordem.

    `cheios` (opcional) sao os eventos `tipo == "full"` de `parse_roteiro`
    - imagem que cobre quase o quadro inteiro, centralizada, por cima de
    tudo. `imgs_full` e o Path de cada um, na MESMA ordem, entrando como
    input N+1..M (depois de todas as camadas da pilha). Nao usam lado nem
    profundidade: cada uma e a sua propria camada, desenhada por ultimo -
    por cima das pilhas -, e o motivo de as pilhas nao aparecerem por
    baixo delas nao mora aqui, mora em `_recorta_blackout` (chamada por
    `parse_roteiro` antes de `camadas` existir): os eventos que chegam
    aqui ja saem com fade para fora bem antes de cada imagem full comecar,
    e voltam com fade so depois dela acabar.

    `janelas_sessao` e a lista de (inicio, fim) em que o crop do layout 3
    (Mariana centralizada) fica ativo - a mesma ideia de
    `grafo_cartas`/`_janela_layout3`, so que aqui uma janela pode ter
    varias cartas em vez de uma so secundaria. Quem as calcula e quem
    chama: `_agrupa_cartas_enumeracao` por sessao de fala, ou
    `janelas_reenquadra` por "ha carta a direita agora?".

    Mesmas notas de `hw`/`escala` de `grafo_cartas()` - nao duplicadas
    aqui, ver aquela docstring."""
    cc, ce = C["cartas"], C["cartas"]["enumeracao"]
    L = _grafo_corte(iv, C)

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

    def _janela(a, b):
        return f"between(t\\,{a:.3f}\\,{b:.3f})"

    base = _reenquadra(L, ult_base, janelas_sessao, resumo_rosto,
                       w_trab, h_trab, cc["crop_margem"],
                       faixa_livre(C, w_trab, h_trab, camadas),
                       cc["crop_margem_lateral"])

    # Mesmo tamanho da carta principal (layout 1) - ver nota no
    # docstring. `ce["carta_altura"]` nao existe mais de proposito: duas
    # constantes para o mesmo numero e como ter dois lugares que decidem
    # [corte], so que em pixel (mesmo raciocinio de `_grafo_corte`).
    altura_px = max(2, int(h_trab * cc["carta_altura"]))
    margem_px = int(w_trab * cc["carta_margem"])
    # fracao da LARGURA do quadro (nao da altura - a pilha e horizontal
    # agora), quanto cada carta desliza para o centro em relacao a
    # anterior do mesmo lado.
    deslocamento_px = int(w_trab * ce["deslocamento"])
    y_base = (h_trab - altura_px) // 2

    # O `fade=t=in` de cada ocorrencia faz DUAS coisas, e a segunda e a
    # que nao se pode tirar: ele e a entrada da carta (macia no roteiro
    # escrito a mao, um piscar de `FADE_POP` na enumeracao automatica, que
    # sempre entrou em corte seco) e ele RELIGA o alfa a 1 no comeco de
    # CADA ocorrencia da camada (ver `_agrupa_camadas`).
    #
    # `alpha=1` nos dois: sem isso `fade` desbota a COR (para preto), nao
    # o alfa - a carta vira um retangulo preto solido em vez de ficar
    # transparente. E os dois com `enable=` preso a janela DESSA MESMA
    # ocorrencia (nao so o `t=in`, ver nota antiga abaixo deste bloco):
    # sem isso, `fade=t=out` fica "alfa 0 para sempre" dali em diante -
    # nao so ate a proxima ocorrencia, mas alem dela tambem - e quando o
    # `t=in` seguinte termina sua propria rampa e volta a so repassar o
    # que vem de tras (e assim que `fade` funciona depois do fim da
    # rampa), o que ele repassa e esse alfa zerado, nunca o opaco. MEDIDO
    # no IMG_0246: com `t=in` sozinho guardado (t=out sem `enable`), a
    # 1a ocorrencia de cada camada aparecia certo e a 2a NUNCA aparecia -
    # so um flash de 50ms na propria rampa, sumindo de novo em seguida.
    # Com os dois presos a janela da ocorrencia, fora dela o par inteiro
    # vira passthrough e quem aparece por baixo e o `format=yuva420p` de
    # base (alfa 255 constante) - e o `enable` do PROPRIO overlay, nao
    # este alfa, quem decide se a camada e desenhada fora dali.
    cor = _img_para_hlg(hlg)
    ult = base
    for i, (camada, img) in enumerate(zip(camadas, imgs), start=1):
        # `camadas` ja vem ordenada por PROFUNDIDADE (`_agrupa_camadas`),
        # entao a carta mais funda de cada pilha - a mais perto do centro -
        # vem depois na cadeia de overlay, ou seja, e desenhada por cima da
        # que esta mais para a borda. A ordem se decide la e nao aqui
        # porque `imgs` vem casada com esta lista, uma entrada de ffmpeg
        # por camada, na mesma ordem.
        desloc = camada["prof"] * deslocamento_px
        xpos = str(margem_px + desloc) if camada["lado"] == "esquerda" else \
            f"W-{margem_px + desloc}-w"
        prox = f"ce{i}"

        fades, janelas = [], []
        for oc in camada["ocorrencias"]:
            janela_oc = _janela(oc["ini"], oc["fim"])
            fades.append(f"fade=t=in:st={oc['ini']:.3f}:"
                          f"d={oc['fade_in']:.3f}:alpha=1:"
                          f"enable='{janela_oc}'")
            fades.append(f"fade=t=out:st={oc['fade_ini']:.3f}:"
                          f"d={oc['fade_dur']:.3f}:alpha=1:"
                          f"enable='{janela_oc}'")
            janelas.append(janela_oc)

        L.append(f"[{i}:v]scale=-2:{altura_px}{cor},format=yuva420p,"
                  f"{','.join(fades)}[carta{i}];")
        L.append(f"[{ult}][carta{i}]overlay=x={xpos}:y={y_base}:"
                  f"enable='{'+'.join(janelas)}'[{prox}];")
        ult = prox

    # ---- imagens em TELA CHEIA, por cima de tudo -----------------------
    # Evento raro de proposito (pedido do Rafael, 09/09/2026): centralizada,
    # cobrindo o maior espaco possivel sem distorcer (`midia.max_largura`/
    # `max_altura`, decrease preserva o aspecto sem eu precisar saber o
    # tamanho da imagem aqui - o ffmpeg calcula em cima do proprio input).
    # Corte seco na propria entrada/saida (FADE_POP, fixado por
    # `parse_roteiro`) - o fade macio de verdade e o das cartas/midias em
    # volta sumindo e voltando, ja embutido no tempo de cada ocorrencia
    # acima por `_recorta_blackout`.
    if cheios:
        cm = cc["midia"]
        maxw = max(2, int(w_trab * cm["max_largura"]) // 2 * 2)
        maxh = max(2, int(h_trab * cm["max_altura"]) // 2 * 2)
        base_idx = len(camadas) + 1
        for j, (cheio, img) in enumerate(zip(cheios, imgs_full), start=base_idx):
            janela_oc = _janela(cheio["ini"], cheio["fim"])
            L.append(f"[{j}:v]scale=w={maxw}:h={maxh}:"
                      f"force_original_aspect_ratio=decrease{cor},"
                      f"format=yuva420p,"
                      f"fade=t=in:st={cheio['ini']:.3f}:"
                      f"d={cheio['fade_in']:.3f}:alpha=1:enable='{janela_oc}',"
                      f"fade=t=out:st={cheio['fade_ini']:.3f}:"
                      f"d={cheio['fade_dur']:.3f}:alpha=1:enable='{janela_oc}'"
                      f"[cheia{j}];")
            prox = f"cf{j}"
            L.append(f"[{ult}][cheia{j}]overlay=x=(W-w)/2:y=(H-h)/2:"
                      f"enable='{janela_oc}'[{prox}];")
            ult = prox

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
    # O `or` que morava aqui sobre lista vazia era mais um caminho para
    # julgamento humano sumir em silencio, so que ao contrario: APAGAR
    # todas as anotacoes do arquivo fazia o programa cair na copia velha
    # gravada dentro do stream.json e reimpor exatamente as anotacoes que
    # a pessoa acabara de tirar. Se o arquivo existe, ele manda - mesmo
    # vazio. A copia do stream.json so vale quando nao ha arquivo nenhum.
    anot = (carrega_anotacoes(d) if (d / "anotacoes.json").exists()
            else S.get("anotacoes", []))
    anot = encaixa_anotacoes(stream, anot)
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


def enterradas_pelo_whisper(d, C, stream, iv):
    """Palavras que era para FICAR e que, pelo relogio do WHISPER, estao
    dentro de um pedaco removido. -> [(fundura, fracao viva, s, e, texto)]

    Todo o resto do programa raciocina sobre os tempos do ALINHADOR. Quando
    eles estao errados, o erro e coerente do comeco ao fim: o corte cai onde
    o fluxo mandou, `_respeita_palavras` confere contra os mesmos numeros
    tortos, e ninguem reclama. Foi assim que o corte do IMG_0247 comeu "que
    muitas" - pelo alinhador a frase comecava em 603,3, depois da emenda;
    pelo audio ela comeca em 600,1, dentro dela.

    Aqui a pergunta e feita com a unica medida INDEPENDENTE que existe, a do
    Whisper. Nao conserta nada - so mede. O conserto e mudar a anotacao (ou
    a borda no editor), e isso quem escreve e uma pessoa.

    A pergunta mora aqui, uma vez so, porque quem pergunta sao DOIS: o
    `_plano`, que avisa antes de gastar minutos gerando audio ou video, e o
    `achados_corte`, que lista para quem esta conferindo (comando e editor).
    Era so do `_plano` ate 04/09/2026 - e o `conferir`, o comando que a doc
    manda rodar ANTES de ouvir justamente para nao depender do ouvido, era o
    unico lugar onde o aviso nao aparecia. MEDIDO nos 7 fluxos de work/:
    dispara em TODOS os 7, 26 palavras (2 a 7 por video); a disputa
    "lamina"/"mental," que o Rafael ouviu como um "ou" estranho no meio do
    audio estava entre elas, e para chegar nela era preciso gerar o audio."""
    p = d / "words.json"
    if not p.exists():
        return []
    W = jload(p)["words"]
    pal = [t for t in stream if t["k"] == "palavra"]
    if len(W) != len(pal):
        return []           # sem correspondencia 1:1 nao da para conferir

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
            suspeitas.append((fund, viva / (e0 - s0), s0, e0, w["w"]))
    suspeitas.sort(reverse=True)
    return suspeitas


def confere_pelo_whisper(d, C, stream, iv, rotulo):
    """A mesma medida de `enterradas_pelo_whisper`, dita no console de quem
    esta prestes a gastar minutos gerando audio ou video."""
    suspeitas = enterradas_pelo_whisper(d, C, stream, iv)
    if not suspeitas:
        return 0
    print(f"[{rotulo}] AVISO: {len(suspeitas)} palavra(s) que deviam FICAR "
          f"estao enterradas dentro de um corte, pelos tempos do Whisper.")
    print(f"[{rotulo}] o alinhamento desse trecho provavelmente errou. "
          f"Ouça esses pontos antes de aceitar o corte (o `conferir` lista "
          f"todas, com o tempo na previa):")
    for fund, frac, s0, _e0, txt in suspeitas[:8]:
        print(f"    {s0:8.2f}s  {txt!r}  ({fund:.1f}s dentro do corte, "
              f"sobra {frac:.0%})")
    if len(suspeitas) > 8:
        print(f"    ... e mais {len(suspeitas) - 8}")
    return len(suspeitas)


def _plano(a, rotulo):
    """Etapas comuns a render, audio, previa e exportar: aplica as decisoes
    humanas e calcula os trechos a manter.

    Devolve o `stream` junto porque o `exportar` precisa dele para a
    legenda - o texto que sobra depois do corte. Buscar o fluxo por fora
    seria le-lo do disco SEM as tres camadas de decisao humana empilhadas,
    e a legenda passaria a dizer coisa que o video nao diz."""
    C, d, stream, dur, db, hop, anot, mont, corr = _base(
        a.video, rotulo, getattr(a, "ignorar_transcript", False))
    iv = intervalos(stream, C, dur, db, hop, anot, mont)
    # O `conferir` ja fazia esta pergunta, e so quem lembrava de roda-lo
    # ouvia a resposta. Aqui ela e feita no caminho que TODO mundo passa
    # antes de gastar minutos: e o unico defeito deste programa que o
    # ouvido nao pega, porque o que sumiu nao faz barulho nenhum.
    da_mao = spans_da_mao(stream, mont, C["corte"]["folga_palavra"])
    for x, y, vivo, motivo, mao in anotacoes_desobedecidas(
            anot, iv, mao=da_mao):
        if mao:
            print(f"[{rotulo}] a anotacao {x:.2f}-{y:.2f} foi reaberta "
                  f"no editor - {vivo:.2f}s de volta: {motivo[:50]}")
        else:
            print(f"[{rotulo}] AVISO: a anotacao {x:.2f}-{y:.2f} nao foi "
                  f"obedecida - {vivo:.2f}s continuam no video: "
                  f"{motivo[:60]}")
    # Mesma regra da anotacao desobedecida, para a outra metade do
    # julgamento: o `conferir` ja faz esta pergunta e so quem lembrava de
    # roda-lo ouvia a resposta. Aqui ela e feita no caminho por onde TODO
    # mundo passa antes de gerar audio ou video.
    sem, _ = pendencias_sem_resposta(
        pendencias(stream, C, carrega_vocabulario(d)),
        carrega_respostas(d), iv, da_mao)
    for q in sem[:6]:
        print(f"[{rotulo}] AVISO: {q['falta']} em {q['s']:.2f}s "
              f"- {q['rot']}: {q['desc'][:50]}")
    if sem:
        print(f"[{rotulo}] {len(sem)} pendencia(s) do revisao.txt sem "
              f"resposta. Decida cada uma e escreva em "
              f"{d/'respostas.json'} (ou corte o trecho).")
    confere_pelo_whisper(d, C, stream, iv, rotulo)
    # Corte que nao deixa nada nao e corte, e o ffmpeg so diz isso com um
    # erro de `concat=n=0` que nao ajuda ninguem. Acontece quando uma
    # anotacao (ou uma sequencia delas) cobre o video inteiro.
    if not iv:
        sys.exit(f"[{rotulo}] abortado: o corte nao deixou nenhum trecho. "
                 f"Provavelmente uma anotacao cobre o video inteiro - veja "
                 f"{d/'anotacoes.json'}.")
    return C, d, stream, dur, iv


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
    outro, com o indice ORIGINAL de cada um em `st`.

    `fora_de_ordem` pega o alinhador embaralhando o INICIO da palavra.
    Isto pega o sintoma irmao, que nao mexe em nenhum inicio: dois tokens
    cobrindo o mesmo intervalo.

    A CAUSA, que este docstring atribuia a uma disputa entre
    `resolve_suspeitos` e `marca_ecos`, foi MEDIDA nos 10 fluxos de
    `work/` e e outra: das 31 invasoes, 27 tem dentro uma palavra cujo
    tempo o proprio alinhador recusou (`align_rejeitado` deriva/ordem) -
    e a que sai com `~` no revisao.txt. Nao ha duas etapas brigando; ha
    uma palavra no lugar errado, e todo mundo que le o tempo dela erra
    junto. Por isso a saida daqui alimenta `zonas_suspeitas`, ao lado do
    `~`, e nao uma pendencia propria.

    E das 31, 8 sao entre dois tokens que ja saem inteiros (nao sobra
    nada para dar errado) e so 3 produzem a consequencia que se temia -
    a ilha curta do "em" solto do IMG_0246_bruto ("das mulheres,
    principalmente, nessas cartas, em que..."). Essas 3 o `conferir` ja
    lista como ILHA CURTA, medidas contra o corte de verdade; a nota de
    disputa entra ali para dizer de onde a ilha veio. Perguntar as 31 ao
    passe de julgamento era pedir julgamento de conteudo sobre geometria,
    e o proprio /revisar mandava nao responder.

    `folga=0.5` veio de rodar contra os quatro stream.json disponiveis
    (IMG_0246, IMG_0246_bruto, IMG_0247, IMG_0248): abaixo de ~0,3s e so
    jitter comum entre duas palavras reais vizinhas (visto ate 0,44s em
    trecho limpo, sem nenhum defeito); a 0,5s sobram so os pares que batem
    em casos ja documentados no codigo - o "do" que _respeita_palavras
    cita (IMG_0247, 49,81-53,08 x 50,64-50,90) e o "principalmente"
    acima, presentes tanto no bruto quanto no IMG_0246 ja julgado (onde a
    disputa se resolveu diferente: os dois tokens saem cut=True - sinal de
    que o resultado desse canto do pipeline nao e estavel entre rodadas)."""
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


def zonas_suspeitas(st, W, choques, junta=3.0, larg_min=1.0):
    """Faixas de tempo em que NENHUMA borda pode ser ancorada.

    Junta as palavras cujo tempo o alinhador recusou (`deriva`/`ordem`, as
    que saem com `~`) com as que invadem o tempo uma da outra
    (`sobrepostos`), e funde o que esta perto. As duas listas sao o mesmo
    defeito visto de dois lados - MEDIDO nos 10 fluxos de work/: 27 dos 31
    pares de `sobrepostos` tem uma palavra `deriva`/`ordem` dentro.

    Existe porque `~` solto nao responde a pergunta que a pessoa tem na
    mao: "da para ancorar aqui?". Tres `~` espalhados numa linha podem ser
    tres palavras ruins com fala boa no meio, ou 7 segundos seguidos sem
    uma fronteira confiavel - e so o segundo caso obriga a anotar a regiao
    inteira. No IMG_0247 sao 49,8-57,4s e 437,1-441,3s; no IMG_0248,
    169,3-175,2s e 244,0-247,9s.

    `larg_min` corta a zona de uma palavra so: ali o `~` no texto ja diz
    tudo, e a faixa nao acrescenta nada. Nos 10 fluxos isso deixa 4 a 7
    faixas por video."""
    pal_i = [i for i, t in enumerate(st) if t["k"] == "palavra"]
    ruins = ({pal_i[k] for k, w in enumerate(W)
              if w.get("align_rejeitado") in ("deriva", "ordem")}
             if len(pal_i) == len(W) else set())
    for ia, ib in choques:
        ruins.update((ia, ib))
    z = []
    for a, b in sorted((st[i]["s"], st[i]["e"]) for i in ruins):
        if z and a - z[-1][1] <= junta:
            z[-1][1] = max(z[-1][1], b)
            z[-1][2] += 1
        else:
            z.append([a, b, 1])
    return [(a, b, n) for a, b, n in z if n >= 2 and b - a >= larg_min]


# `why` de quem decidiu olhando o TEXTO, e nao a acustica nem o relogio.
# Forma de regra, nao taxa - por isso mora aqui e nao no config.toml.
#
# A distincao existe por causa de uma pergunta so: quando o tempo removido
# nao cabe no texto que a regra leu, o texto e que esta errado. Isso so faz
# sentido de perguntar a quem decidiu POR texto. As decisoes acusticas
# ("voz sem texto", "pausa", "nao e fala N car/s") ja mediram o tempo por
# conta propria e nao tem o que estranhar nele; "arrastada" e o proprio
# relogio julgando; "anotado:" e uma pessoa.
CORTE_POR_TEXTO = ("alongamento", "retomada colapsada", "cacoete",
                   "muleta isolada", "gaguejada", "tentativa",
                   "ela mandou cortar", "hesitação recuperada")

EXPLICA_PENDENCIA = {
    "texto nao explica o tempo":
        "a regra cortou por causa do TEXTO, e o texto nao da conta do "
        "tempo que sai. Onde o Whisper nao conseguiu ler, ele escreve "
        "um cacoete plausivel e curto no lugar de fala que existe: "
        "ouca o trecho antes de deixar sair.",
    "texto reconstruido":
        "leitura de baixa confianca de um trecho que o Whisper nao "
        "leu. Se nao faz sentido no assunto da aula, e ELE que sai.",
    "som alto sem texto":
        "ninguem conseguiu ler e o programa manteve de proposito, "
        "para voce decidir. Sai ou fica?",
    "fala rarefeita":
        "o que SOBRA ali e pouca fala em muito tempo: parada longa, "
        "retomada, ou ela se perdendo.",
    "retomada?":
        "ela repete duas palavras pouco depois. Pode ser tentativa "
        "abandonada (corte a PRIMEIRA, fique com a ultima) ou anafora "
        "de proposito ('na carta da Rainha... na carta do Rei'). So o "
        "sentido separa as duas - o programa nao corta isto sozinho.",
}


def pendencias(st, C, vocab):
    """A lista fechada do que o programa sabe que NAO sabe resolver.

    Mora aqui, uma vez so, porque tres partes precisam dela: o `revisar`,
    que numera e ancora cada uma no texto; o `conferir`, que cobra a
    resposta; e o `_plano`, que avisa antes de gastar minutos gerando
    audio ou video.

    Cada item leva o trecho de audio de que ele TRATA (`x`, `y`), e nao so
    a ancora: e por ele que `pendencias_sem_resposta` sabe se a pergunta
    ainda existe depois do corte.

    TRES CATEGORIAS SAIRAM DAQUI, e as tres por medicao nos 10 fluxos de
    `work/` (268 pendencias no total antes, 55 depois - de 7-50 por video
    para 0-12):

      `fala nao transcrita` (106, a maior de todas) nao dizia nada que o
      token VIZINHO ja nao dissesse. Ela apontava a PALAVRA em que o audio
      estava grudado, mas `resolve_suspeitos` ja separou esse audio num
      token proprio antes do revisao.txt existir: nos 106 casos o vizinho
      ou ja esta cortado (90 - o audio esta indo embora) ou ja e outra
      pendencia, `som alto sem texto` (14) ou `texto reconstruido` (2).
      Era a mesma pergunta feita duas vezes, do lado errado.

      `cauda arrastada` (76) era pergunta sobre GEOMETRIA feita a quem le
      texto - e sem os numeros que permitiriam responde-la. Virou medida
      no `conferir` (ARRASTO SOBREVIVE): das 76, 64 deixam 0,06-0,14s de
      arrasto - a folga que `_respeita_palavras` devolve por construcao -
      e 11 deixam o arrasto INTEIRO, porque a poda (0,25-0,30s) nao coube
      no `minimo`. Mais 3 sobram por outro motivo, em regiao que o
      alinhador embaralhou. Perguntar as 76 escondia essas 14.

      `disputa` (31) o proprio /revisar mandava nao responder ("nao e
      julgamento de conteudo... e achado para o programador"). E a causa
      no docstring de `sobrepostos` estava errada: 27 das 31 envolvem uma
      palavra cujo tempo o proprio alinhador recusou (`deriva`/`ordem`),
      que ja aparece como `~` no texto; 8 sao entre dois tokens que ja
      saem; e a unica consequencia que ela previa - ilha curta - aparece
      em 3, e nessas 3 o `conferir` ja lista ILHA CURTA, medida contra o
      corte de verdade em vez de adivinhada."""
    pend = []

    def pergunta(i, rot, desc, x=None, y=None):
        pend.append({"i": i, "rot": rot, "desc": desc, "s": st[i]["s"],
                     "x": st[i]["s"] if x is None else x,
                     "y": st[i]["e"] if y is None else y})

    for x, y, sob, n in rarefeitos(st, C["revisao"]):
        # ancora no primeiro token MANTIDO que cai dentro: e o unico ponto
        # do texto corrido onde ele pode aparecer.
        for i, t in enumerate(st):
            if not t["cut"] and t["e"] > x and t["s"] < y:
                pergunta(i, "fala rarefeita",
                         f"{sob:.0f}s com so {n} palavra(s)", x, y)
                break

    # Retomada que o corte automatico NAO pegou. `detecta_tentativas` corta
    # com `min_ngram` do config (3 palavras identicas); aqui ela roda de
    # novo com `min_ngram_aviso` (2) so para PERGUNTAR.
    #
    # Por que perguntar em vez de cortar: MEDIDO nos 10 fluxos de work/, o
    # limiar de 2 acha 9 das retomadas que uma pessoa anotou a mao no
    # IMG_0246/0247/0248/7 de espadas/as de espadas - "personagens
    # politicos ou da cultura, enfim,", "pessoas mesmo, que aparecem",
    # "uma coisa sendo representada,", "na vida do consulante", "nao
    # precisa, nao precisa" - e mais 4 que sao ANAFORA, nao retomada: "na
    # Carta da Rainha, por exemplo, ... na Carta do Rei e o Rio", "nao e
    # isolado, nao e so ler", "estando em troca, estando em relacao". As
    # quatro sao conteudo de verdade e cortar qualquer uma seria o pior
    # defeito deste programa. A diferenca entre as duas coisas nao esta na
    # forma - esta no sentido, e quem le o sentido e o passe de julgamento.
    #
    # Sobram 0 a 4 perguntas por video, cada uma com o texto citado.
    #
    # `respiro_gap` alcanca a retomada RAPIDA, que o `min_ngram` sozinho
    # nao ve: pausa abaixo de `pausa_intocavel` nao vira token, e sem token
    # os filtros 1 e 3 leem a retomada como paralelismo (ver
    # `detecta_tentativas`). O numero e o `gap_min` do [silencio] porque e
    # a mesma pergunta que ele ja responde - "este vao entre duas palavras
    # conta como lacuna?" -, e um segundo numero para decidir isso seria um
    # segundo lugar decidindo a mesma coisa.
    #
    # Custo MEDIDO nos 11 fluxos de work/: as perguntas `retomada?` vao de
    # 10 para 14 no total - 1,3 por video, pior caso 4, dentro do teto que
    # este bloco registra. Sao 8 cadeias novas, mas metade cai em trecho
    # que ja esta saindo e o filtro logo abaixo a descarta sozinho.
    for c in detecta_tentativas(st, C["tentativa"],
                                C["tentativa"]["min_ngram_aviso"],
                                respiro_gap=C["silencio"]["gap_min"]):
        pals = [st[j] for j in range(c["ini"], c["fim"])
                if st[j]["k"] == "palavra"]
        if not pals or sum(1 for t in pals if not t["cut"]) <= len(pals) / 2:
            continue        # ja esta saindo: nao ha o que perguntar
        pergunta(c["ini"], "retomada?",
                 f"volta a dizer {c['repetido']!r} {c['dur']:.1f}s depois. "
                 f"Descartaria: {c['descartado'][:70]!r}",
                 st[c["ini"]]["s"], st[c["fim"] - 1]["e"])

    # Corte automatico cujo TEMPO o proprio texto que o justificou nao
    # explica. E a unica pendencia sobre audio que JA SAI, e existe porque
    # o defeito que ela pega nao deixa marca em lugar nenhum: no "as de
    # espadas" um corte de 27,5s (35,46-62,92s) saiu inteiro sem uma
    # pergunta, e o Rafael so o descobriu ouvindo a FONTE - dos 27,5s, 9,6
    # eram mesmo hesitacao e 17,9 eram fala que o Whisper nao transcreveu.
    # Nao havia `[??]` (que `protege_naolidos` cobre), nao havia `[rec:]`,
    # nao havia `VOZ` na marca: o corte tinha texto, so que um texto
    # inventado - "éé..." repetido 200 vezes e "profunda, mais potente.".
    #
    # A pergunta e a mesma que `marca_arrastadas` ja faz para podar cauda
    # ("este tempo cabe no que esta escrito?"), com os MESMOS numeros dela,
    # e falhou ali por duas razoes independentes: `marca_cacoetes` corre
    # antes e ja tinha cortado o token (e `marca_arrastadas` pula token
    # cortado), e mesmo se olhasse, `letras_faladas` conta 200 letras num
    # "éé..." de uma vogal so - o que fazia 20,5s de um som parecerem
    # ritmo de fala normal. Por isso aqui o texto passa por
    # `letras_uma_vez`.
    #
    # MEDIDO nos 7 fluxos de work/, sobre 216 cortes automaticos de >=1s:
    # dispara 2 vezes, as duas no MESMO corte de 27,5s (o "éé..." de 20,54s
    # e o "profunda, mais potente." de 6,06s). Sem o piso de
    # `corte_por_texto_min` seriam 8, e as outras 6 (1,40 a 2,27s) sao
    # cacoete de verdade - "Eeeeee" sustentado, "É... isso.".
    med = ritmo_mediano(st)
    for i, t in enumerate(st if med is not None else []):
        if not t["cut"] or t["k"] == "silencio":
            continue
        why = str(t.get("why", ""))
        dur = t["e"] - t["s"]
        if (dur < C["revisao"]["corte_por_texto_min"]
                or not why.startswith(CORTE_POR_TEXTO)):
            continue
        cabe = med * letras_uma_vez(t["t"])
        if dur < cabe * C["arrastada"]["fator"]:
            continue
        pergunta(i, "texto nao explica o tempo",
                 f"{dur:.1f}s saem como {why[:26]!r}, e o texto "
                 f"({t['t'][:24]!r}) da conta de {cabe:.1f}s",
                 t["s"], t["e"])
        # A unica pendencia que nasce sobre trecho JA CORTADO, e por isso a
        # unica em que o corte nao e resposta. Ver `pendencias_sem_resposta`.
        pend[-1]["cortada"] = True
        pend[-1]["proprio"] = True

    for i, t in enumerate(st):
        if t["cut"] or t["k"] != "hesitacao":
            continue
        if t.get("mao_texto"):
            # o texto deste token foi escrito A MAO no editor, ouvindo. Nao
            # ha o que perguntar: "texto reconstruido" e a duvida sobre uma
            # LEITURA de confianca baixa do Whisper, e aqui nao houve
            # leitura nenhuma. Sem esta linha, escrever o que se ouve num
            # vao criava na hora uma pendencia sobre a propria frase que a
            # pessoa acabou de digitar - alarme disparando onde alguem ja
            # decidiu, que e o defeito que este arquivo mais persegue.
            continue
        if str(t["t"]).startswith("??"):
            pergunta(i, "som alto sem texto", f"{t['e']-t['s']:.1f}s")
        else:
            fora = fora_do_vocabulario(t["t"], vocab)
            extra = ("  <== " + ", ".join(fora) + " nao existe no "
                     "material do curso: leitura provavelmente errada"
                     if fora else "")
            if t.get("retomada_de"):
                # a troca que `marca_ecos` fez: a pergunta tem de dizer
                # qual copia saiu, senao quem julga ve so a que ficou e nao
                # tem como saber que havia escolha.
                extra += (f"  <== retomada: ficou esta, saiu a de antes "
                          f"{t['retomada_de'][:50]!r}")
            pergunta(i, "texto reconstruido",
                     f"{t['t']!r} ({t['e']-t['s']:.1f}s){extra}")
        # so aqui o token ancora E o trecho em questao - `proprio`. E o que
        # deixa `pendencias_sem_resposta` cobrar as duas coisas que so
        # fazem sentido quando trecho e pergunta coincidem: manter a mao
        # vale por resposta, e responder "sai" obriga o trecho a sair.
        # Numa `retomada?` nenhuma das duas valeria - a cadeia vai da
        # primeira tentativa ate a versao boa, que fica.
        pend[-1]["proprio"] = True
        pend[-1]["mao"] = t["why"] in MAO_MANTEM

    pend.sort(key=lambda q: (q["s"], q["i"]))
    for n, q in enumerate(pend, 1):
        q["n"] = n
    return pend


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
    C = cfg()
    bagunca = fora_de_ordem(st)
    choques = sobrepostos(st)
    W = jload(d / "words.json").get("words", [])
    zonas = zonas_suspeitas(st, W, choques)
    pend = pendencias(st, C, carrega_vocabulario(d))
    resp = carrega_respostas(d)

    L = [f"# {Path(a.video).stem} | {hhmmss(dur)} | tempos em segundos",
         "# [-N.Ns] = ja marcado para corte, trecho quieto. Texto solto =",
         "# sera mantido.",
         "# [-N.Ns VOZ] = marcado para corte MESMO havendo som alto ali",
         "# dentro. O transcritor nao leu, mas o microfone ouviu: pode ser",
         "# frase inteira prestes a sumir. Confira antes de deixar passar.",
         "# [rec: ...] = texto RECONSTRUIDO. Nao saiu da transcricao do",
         "# video: o programa re-transcreveu aquele pedaco isolado porque",
         "# o Whisper nao tinha lido nada ali. E a leitura de MENOR",
         "# confianca do arquivo inteiro, e e onde bocejo e resmungo se",
         "# disfarcam de palavra. Va sempre pelo sentido: se nao encaixa",
         "# no assunto da aula, o candidato a sair e ele, nao o vizinho.",
         "# palavra~ = o tempo dessa palavra nao e confiavel (o alinhador",
         "# recusou o proprio resultado e caiu no tempo grosseiro do",
         "# Whisper). NAO ancore borda de corte numa palavra com ~: ela",
         "# pode estar segundos longe de onde aparece aqui.",
         "# [Pn] = pendencia numero n, listada em PENDENCIAS logo abaixo.",
         "# Procure, alem das pendencias: retomadas com outras palavras,",
         "# interrupcoes, trechos em que ela se perde. Responda com",
         "# trechos [inicio, fim, motivo]."]
    if bagunca:
        L.append(f"# aviso: {bagunca} palavras com tempo fora de ordem - o "
                 f"alinhador embaralhou")
        L.append("# esses trechos, e ali o [-N.Ns] pode faltar mesmo "
                 "havendo parada.")
    if zonas:
        # O `~` marca a palavra; isto marca a REGIAO. Sem ela a pessoa
        # descobre que nao ha fronteira confiavel ali depois de escrever
        # a anotacao, quando o `conferir` acusa que ela nao foi obedecida.
        L.append("# faixas em que o alinhador nao acertou o tempo de "
                 "NENHUMA palavra por perto:")
        for a, b, n in zonas:
            L.append(f"#   {a:.1f}-{b:.1f}s ({n} palavras) - para cortar "
                     f"aqui, anote a faixa inteira; nao ancore dentro.")

    # ------------------------------------------------------ pendencias
    # Tudo o que o programa sabe que NAO sabe resolver sozinho vira um
    # item numerado aqui em cima, alem de aparecer ancorado no texto.
    #
    # Antes cada uma destas marcas so existia no meio do texto corrido, e
    # a instrucao "toda ocorrencia precisa de uma resposta sua" dependia
    # de quem lia ir catando marca por marca ate o fim. Nao foi o que
    # aconteceu: no 7 de espadas um bocejo inteiro atravessou o passe de
    # julgamento disfarcado de palavra reconstruida e so apareceu quando
    # o Rafael ouviu o audio. Lista numerada se confere; marca solta no
    # meio de 200 linhas de texto nao se confere.
    #
    # QUAIS perguntas entram na lista e decisao de `pendencias()`, nao
    # daqui: o `conferir` e o `_plano` cobram a resposta de cada uma, e
    # duas listas diferentes seriam duas perguntas diferentes com o mesmo
    # nome. A explicacao de cada categoria vai UMA vez, no cabecalho do
    # bloco, e so das categorias que apareceram - repeti-la em cada item
    # custava 100 caracteres por linha num arquivo que existe para ser
    # pequeno.
    def ancora(i):
        """O token MANTIDO mais proximo de `i`, para onde a marca `[Pn]`
        vai no texto corrido.

        Token cortado nao e impresso, entao marca posta nele some sem
        deixar rastro - e ja sumiu: era o caso de toda `[disputa]`, que
        `sobrepostos` devolvia sem olhar `cut`."""
        for k in range(i, len(st)):
            if not st[k]["cut"]:
                return k
        for k in range(i, -1, -1):
            if not st[k]["cut"]:
                return k
        return None

    # Uma pendencia ja respondida no `respostas.json` continua na lista,
    # marcada: some-la seria esconder o julgamento de quem reler o arquivo
    # daqui a seis meses, e reabri-la sem marca faria a pessoa responder
    # duas vezes a mesma coisa.
    respondida, casadas = {}, set()
    for q in pend:
        k = casa_resposta(q, resp)
        if k is not None:
            respondida[q["n"]] = resp[k]
            casadas.add(k)
    orfas = [r for k, r in enumerate(resp) if k not in casadas]

    marca = {}
    for q in pend:
        j = ancora(q["i"])
        if j is not None:
            marca[j] = marca.get(j, "") + f"[P{q['n']}]"

    if pend:
        aberta = [q for q in pend if q["n"] not in respondida]
        L.append("")
        L.append(f"## PENDENCIAS ({len(pend)}, {len(aberta)} sem resposta) "
                 f"- responda cada uma em {d.name}/respostas.json e no "
                 f"relatorio: sai, fica ou ouvido, e por que")
        for rot in sorted({q["rot"] for q in pend}):
            L.append(f"#  {rot} = {EXPLICA_PENDENCIA[rot]}")
        for q in pend:
            r = respondida.get(q["n"])
            selo = f"  [ja respondida: {r['resposta']}]" if r else ""
            L.append(f"P{q['n']:<3d} {q['s']:8.2f}s  "
                     f"{q['rot']}: {q['desc']}{selo}")
    L.append("")

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
    pal_i = [i for i, t in enumerate(st) if t["k"] == "palavra"]
    duvida = ({pal_i[k] for k, w in enumerate(W)
               if w.get("align_rejeitado") in ("deriva", "ordem")}
              if len(pal_i) == len(W) else set())

    # O microfone e a segunda opiniao sobre o que esta indo embora: um
    # [-1.8s] de silencio e um [-1.8s] de frase eram impressos igual.
    db, hop, _ = envelope(d / "audio.wav")
    limiar = limiar_voz(db, C["silencio"]["limiar_fala_db"])

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
        elif t["k"] == "silencio":
            linha.append(f"[pausa {t['e']-t['s']:.1f}s]")
        else:
            # texto reconstruido vai INTEIRO. Truncar em 14 caracteres era
            # esconder metade da prova: o que denuncia bocejo lido como
            # palavra e a frase nao fechar sentido, e meia frase nao fecha
            # sentido nenhum por definicao.
            linha.append(f"[rec: {t['t']}]")
        if len(" ".join(linha)) > 130:
            L.append(f"{ini:6.1f} " + " ".join(linha))
            linha, ini = [], None
    if linha:
        L.append(f"{ini:6.1f} " + " ".join(linha))

    p = d / "revisao.txt"
    p.write_text("\n".join(L) + "\n", encoding="utf-8")
    n = len("\n".join(L))
    print(f"[revisar] {p}  ({n} caracteres, ~{n//4} tokens)")
    aberta = len(pend) - len(respondida)
    print(f"[revisar] {len(pend)} pendencia(s); {aberta} ainda sem resposta")
    if orfas:
        # Resposta que nao casa com pendencia nenhuma quase sempre e tempo
        # digitado errado - e o sintoma dela e justamente o contrario do
        # que a pessoa espera: a pendencia continua na lista de abertas.
        for r in orfas:
            print(f"[revisar] resposta em {r['em']} nao casa com pendencia "
                  f"nenhuma (tempo errado, ou ela ja saiu no corte)")
    print(f"[revisar] escreva o julgamento em {d/'anotacoes.json'} e as "
          f"respostas em {d/'respostas.json'}")


def somas_previa(iv):
    """O tempo de PREVIA em que cada trecho de `iv` comeca.

    E a tabela de conversao entre os dois relogios, e ela e uma so: o
    `_mapa_previa` a usa nas duas direcoes e o editor a manda inteira para a
    tela, que precisa dela uma vez por marca da regua da onda e nao pode
    pedir um POST por marca. Uma segunda soma corrida feita no navegador "so
    para desenhar a regua" seria a mesma conta dita duas vezes, e a que
    diverge calada e sempre a que ninguem esta olhando."""
    p, out = 0.0, []
    for a, b in iv:
        out.append(p)
        p += b - a
    return out


def _mapa_previa(iv):
    """Tempo da PREVIA -> tempo da FONTE, e vice-versa.

    O Rafael ouve o .m4a e reclama em "00:14". Todo o resto do programa
    pensa em tempo de fonte. Traduzir isso a mao, um defeito de cada vez,
    foi o trabalho manual de duas rodadas de revisao inteiras."""
    somas = somas_previa(iv)
    total = somas[-1] + (iv[-1][1] - iv[-1][0]) if iv else 0.0

    def para_fonte(t):
        for (a, b), p in zip(iv, somas):
            if t <= p + (b - a):
                return a + (t - p)
        return iv[-1][1] if iv else 0.0

    def para_previa(t):
        for (a, b), p in zip(iv, somas):
            if t < a:
                return p                    # cai dentro de um corte
            if t <= b:
                return p + (t - a)
        return total
    return para_fonte, para_previa


def achados_corte(stream, C, iv, anot, mont, pend, resp, d):
    """Todo suspeito que a GEOMETRIA deste corte produz, numa lista so.

    Ficou fora do `cmd_conferir` quando o editor passou a fazer a mesma
    pergunta na tela. Antes disso eram dois mundos: o `conferir` achava 25
    pontos no IMG_0248 e o painel do editor mostrava zero deles, porque o
    painel so sabia de anotacao desobedecida e de palavra que a pessoa
    mandou sumir e nao sumiu. Quem estava editando nao tinha como saber que
    havia 25 coisas para ouvir sem sair do editor, rodar um comando e ler
    um txt em segundos da fonte - numeros que a folha nao mostra em lugar
    nenhum.

    E a mesma razao pela qual `intervalos()` e uma funcao so: duas
    implementacoes da mesma pergunta divergem, e a que diverge em silencio
    e sempre a que ninguem esta olhando.

    `d` e a pasta de trabalho, e e obrigatorio de proposito: sem ela nao da
    para ler o `words.json`, e a pergunta que depende dele - a palavra
    enterrada, item 6 - sumiria de quem esquecesse de passar a pasta. Um
    parametro opcional aqui seria uma lista de achados que diverge da outra
    em silencio, que e exatamente o que esta funcao existe para impedir.

    Devolve (achados, orfas). Cada achado e a tupla ordenavel
    (tempo na previa, rotulo, ini na fonte, fim na fonte, texto)."""
    achados = []
    _, para_previa = _mapa_previa(iv)

    def diz(rot, x, y, txt):
        achados.append((para_previa(x), rot, x, y, txt))

    # 0. Pendencia do revisao.txt que ninguem respondeu.
    #
    #    "Responda toda pendencia" era regra do prompt do /revisar e de
    #    mais ninguem: o programa listava, numerava e ancorava cada `Pn`, e
    #    depois nao perguntava se alguma tinha ficado sem resposta. E o
    #    mesmo buraco de `anotacoes_desobedecidas` antes de ela existir -
    #    julgamento que ninguem cobra e julgamento que um dia nao acontece.
    #
    #    Uma pendencia se responde de dois jeitos, e os dois valem: cortando
    #    o trecho (a resposta e o proprio corte, e por isso `iv` entra na
    #    conta) ou escrevendo no `respostas.json` - o unico jeito de dizer
    #    "fica", que nao deixa marca em lugar nenhum.
    da_mao = spans_da_mao(stream, mont, C["corte"]["folga_palavra"])
    sem, orfas = pendencias_sem_resposta(pend, resp, iv, da_mao)
    for q in sem:
        diz(q["falta"], q["s"], q["y"], f"{q['rot']}: {q['desc'][:64]}")

    # 1. Anotacao desobedecida. E o pior de todos: o julgamento humano foi
    #    escrito, foi lido, e sumiu na geometria sem uma linha de aviso.
    for x, y, vivo, motivo, mao in anotacoes_desobedecidas(
            anot, iv, mao=da_mao):
        diz("ANOTACAO REABERTA A MAO" if mao
            else "ANOTACAO NAO OBEDECIDA", x, y,
            f"sobra {vivo:.2f}s de {y-x:.2f}s | {motivo[:50]}")

    # 2. Fala marcada para sair que continua tocando. Silencio nao entra:
    #    metade da pausa fica colada em cada lado de proposito
    #    (pausa_alvo/2), entao ele "sobrevive" sempre, por desenho.
    #    O limite de 0,15s tem um cego, e ele e exatamente o caso que o
    #    editor avisa na tela: palavra mais curta que as duas folgas de
    #    0,06 sobrevive INTEIRA sem nunca chegar a 0,15s. MEDIDO nos 10
    #    fluxos de work/: dois casos, um `eu` de 0,08s (as de espadas,
    #    516,0s) e um `na` de 0,06s dentro de uma anotacao (IMG_0247,
    #    351,1s) - poucos, e os dois sao a palavra inteira continuando a
    #    tocar depois de alguem mandar tira-la. So a passada mecanica nao
    #    via: `Sessao.avisos` pergunta isso desde sempre, mas so de quem
    #    abre o editor.
    for t in stream:
        if not t["cut"] or t["k"] == "silencio":
            continue
        vivo = sum(max(0.0, min(t["e"], b) - max(t["s"], aa)) for aa, b in iv)
        larg = t["e"] - t["s"]
        inteira = larg > 0.02 and vivo >= larg - 0.02
        if vivo > 0.15 or inteira:
            diz("FALA CORTADA SOBREVIVE", t["s"], t["e"],
                f"{vivo:.2f}s{' INTEIRA' if inteira else ''} | "
                f"{t.get('why','')[:24]} | {t['t'][:30]!r}")

    # 3. Ilha curta. Nem sempre e defeito - uma palavra sozinha entre dois
    #    cortes pode ser exatamente o que a anotacao pediu -, mas e sempre
    #    duas emendas em volta de pouca coisa, e vale um ouvido.
    #    `sobrepostos` entra aqui, e nao no revisao.txt, porque e aqui que
    #    ele tem consequencia. MEDIDO nos 10 fluxos: das 31 invasoes, 3
    #    produzem ilha curta - e sao essas 3 que alguem precisa ouvir. As
    #    outras 28 eram pergunta sem resposta possivel no passe de
    #    julgamento (27 delas envolvem palavra que o alinhador ja marcou
    #    com `~`; 8 sao entre dois tokens que ja saem inteiros).
    choques = sobrepostos(stream)
    for x, y in iv:
        if y - x < C["corte"]["ilha_minima"]:
            pals = " ".join(t["t"] for t in stream if t["k"] == "palavra"
                            and not t["cut"] and t["e"] > x and t["s"] < y)
            briga = any(min(stream[ia]["e"], stream[ib]["e"]) > x
                        and max(stream[ia]["s"], stream[ib]["s"]) < y
                        for ia, ib in choques)
            diz("ILHA CURTA", x, y, f"{y-x:.2f}s | {pals[:40]!r}"
                + (" | duas recuperacoes disputam este audio" if briga
                   else ""))

    # 3b. Arrasto que se mandou podar e continua tocando.
    #
    #     `marca_arrastadas` corta a CAUDA da palavra arrastada, mas esse
    #     corte passa pelo `minimo` como qualquer outro: cauda menor que
    #     `minimo` + `folga_palavra` (0,34s) e descartada, e o arrasto fica
    #     inteiro sem que nada diga.
    #
    #     MEDIDO nos 10 fluxos de work/, 76 caudas: 64 deixam 0,06-0,14s,
    #     que e a folga que `_respeita_palavras` devolve por construcao; 1
    #     sai inteira; e 11 sobrevivem 100% - todas com cauda de 0,25-0,30s,
    #     abaixo do piso geometrico. Mais 3 sobram por outro motivo (0,36 a
    #     0,47s, em regiao que o alinhador embaralhou). Sao essas 14 que
    #     esta linha acha, e era por elas que a categoria `cauda arrastada`
    #     perguntava as 76 no revisao.txt - perguntar todas escondia estas.
    #
    #     O limite e o mesmo 0,15s de `anotacoes_desobedecidas`, e pela
    #     mesma razao: a folga de borda de cada lado soma ~0,12s por
    #     desenho, e acima disso e outra coisa.
    for t in stream:
        if t["cut"] or not t.get("cauda"):
            continue
        ca, cb = t["cauda"]
        vivo = sum(max(0.0, min(cb, b) - max(ca, aa)) for aa, b in iv)
        if vivo > 0.15:
            diz("ARRASTO SOBREVIVE", ca, cb,
                f"{vivo:.2f}s de {cb-ca:.2f}s | {t['t'][:20]!r} | "
                f"{t.get('why','')[:20]}")

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

    # 5. Gagueira que sobra AUDIVEL. `marca_cacoetes` pergunta isso no
    #    fluxo, antes de qualquer geometria; aqui a pergunta e refeita
    #    sobre o corte PRONTO, porque metade destes casos nao existia no
    #    fluxo - foi o corte que criou a vizinhanca. MEDIDO: no IMG_0248
    #    a anotacao de 343,7-350,4s ("tres tentativas da mesma oracao")
    #    encosta o "que" de antes no "que" de depois, 0,12s de distancia
    #    na previa e 6,4s na fonte; no IMG_0247 a de 249,7s faz o mesmo
    #    com dois "e". Nenhuma das duas aparece em lugar nenhum hoje.
    #
    #    A distancia se mede na PREVIA, nao na fonte: e la que a pessoa
    #    ouve as duas palavras coladas. Por `chave_ac`, com acento, pela
    #    mesma razao da `marca_cacoetes` ("e é" nao e repeticao).
    vivas = []
    for t in stream:
        if t["cut"] or t["k"] != "palavra":
            continue
        largura = fim_util(t) - t["s"]
        if largura <= 0:
            continue
        vivo = sum(max(0.0, min(fim_util(t), b) - max(t["s"], aa))
                   for aa, b in iv)
        if vivo > largura * 0.5:
            vivas.append(t)
    for x, y in zip(vivas, vivas[1:]):
        k = chave_ac(x["t"])
        if not k or k != chave_ac(y["t"]) or chave(x["t"]) in ENFASE:
            continue
        if (para_previa(y["s"]) - para_previa(fim_util(x))
                > C["cacoete"]["gaguejada_max"]):
            continue
        diz("REPETICAO AUDIVEL", x["s"], y["e"],
            f"{x['t']!r} + {y['t']!r} | {y['s']-x['e']:.2f}s na fonte")

    # 6. Palavra mantida que, pelo relogio do WHISPER, esta enterrada dentro
    #    do corte. Os outros cinco itens medem o corte contra os tempos do
    #    ALINHADOR - os mesmos com que ele foi calculado. Quando e o
    #    alinhador que erra, todos eles concordam com o erro e nao ha o que
    #    reclamar; a unica segunda opiniao que existe neste programa e a do
    #    Whisper, e ela ja estava escrita em `enterradas_pelo_whisper`.
    #
    #    So que a resposta so aparecia no console de quem gerava audio ou
    #    video. Quem roda o `conferir` para nao depender do ouvido - que e o
    #    que a doc manda fazer ANTES de ouvir - nao via nenhuma. MEDIDO nos
    #    7 fluxos: 26 palavras, 2 a 7 por video, todas invisiveis aqui.
    #
    #    O tempo da FONTE aqui e o do Whisper, e nao o do alinhador: quando
    #    os dois discordam, e o do alinhador que esta sob suspeita. Na
    #    previa cai na emenda mais proxima, que e onde se ouve o buraco.
    for fund, frac, s0, e0, txt in enterradas_pelo_whisper(d, C, stream, iv):
        diz("PALAVRA ENTERRADA", s0, e0,
            f"{txt[:20]!r} | {fund:.1f}s dentro do corte, sobra {frac:.0%} "
            f"| tempo do Whisper, o alinhador discorda")

    # ORDEM DE ESCUTA, nao ordem de categoria. Quem confere ouve o .m4a do
    # comeco ao fim uma vez so; uma lista agrupada por tipo de defeito obriga
    # a pular para tras seis vezes.
    achados.sort()
    return achados, orfas


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
        t = le_tempo(a.em)
        if t is None:
            sys.exit(f"[conferir] nao entendi o tempo {a.em!r} - use MM:SS")
        print(f"[conferir] previa {hhmmss(t)} = fonte {para_fonte(t):.2f}s "
              f"({hhmmss(para_fonte(t))})")
        return

    pend = pendencias(stream, C, carrega_vocabulario(d))
    achados, orfas = achados_corte(stream, C, iv, anot, mont, pend,
                                   carrega_respostas(d), d)
    L = [f"# {Path(a.video).stem} | {len(iv)} emendas | "
         f"{hhmmss(mantido)} de {hhmmss(dur)}",
         "# ouca na ordem: a coluna PREVIA e o tempo no .m4a.",
         "# PENDENCIA SEM RESPOSTA = o revisao.txt perguntou e ninguem "
         "respondeu; o primeiro numero da coluna `fonte` e o `em` do "
         "respostas.json.",
         "# PENDENCIA NAO OBEDECIDA = voce respondeu 'sai' e o trecho "
         "continua tocando - o respostas.json registra, quem corta e a "
         "anotacao.",
         "# ANOTACAO NAO OBEDECIDA = voce mandou cortar e nao saiu. Sempre bug.",
         "# ANOTACAO REABERTA A MAO = voce mesmo trouxe de volta, no editor.",
         "# FALA CORTADA SOBREVIVE = o programa marcou para sair e continua ali.",
         "# ILHA CURTA = pouca coisa entre dois cortes; nem sempre e defeito.",
         "# PALAVRA RASPADA = borda comeu pedaco de palavra mantida.",
         "# REPETICAO AUDIVEL = a mesma palavra duas vezes seguidas no "
         "corte; o corte pode ter criado a gagueira.",
         "# ARRASTO SOBREVIVE = a poda de cauda arrastada nao coube no "
         "corte; o rabo continua audivel (conserto: montagem.json).",
         "# PALAVRA ENTERRADA = pelo relogio do WHISPER essa palavra "
         "mantida esta dentro do corte, longe da emenda. Quem errou ali e "
         "o alinhador, nao o corte: a borda foi ancorada num tempo que a "
         "palavra nao ocupa (conserto: montagem.json, ouvindo).",
         ""]
    for p, rot, x, y, txt in achados:
        L.append(f"{hhmmss(p)}  {rot:24s} fonte {x:8.2f}-{y:<8.2f} {txt}")
    if not achados:
        L.append("nada suspeito")

    for r in orfas:
        L.append(f"# nota: a resposta em {r['em']} nao casa com pendencia "
                 f"nenhuma - tempo errado, ou ela ja saiu no corte.")

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
    _, d, _, dur, iv = _plano(a, "previa")
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
    C, d, _, dur, iv = _plano(a, "audio")
    meta = jload(d / "meta.json")
    ca = C["audio"]
    fade = ca["fade"]

    L = [trecho_audio(i, x, y, fade) for i, (x, y) in enumerate(iv)]
    L.append("".join(f"[a{i}]" for i in range(len(iv)))
             + f"concat=n={len(iv)}:v=0:a=1[ac]")
    g = d / "graph_audio.txt"
    with open(g, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(L))

    # Em export/prévias e nao ao lado do bruto: ver pastas.py. O editor
    # procura o .m4a pela mesma funcao, para dizer quando ele envelheceu.
    saida = Path(a.out) if a.out else pastas.onde(
        meta["src"], Path(a.video).stem, pastas.AUDIO)
    saida.parent.mkdir(parents=True, exist_ok=True)
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
        bruto.unlink(missing_ok=True)
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
    esticado por causa de outra coisa cortada no meio).

    Pedido do Rafael em 02/09: a carta entra `atraso` DEPOIS do fim da
    mencao (nao antes - a antiga `antecipa` ia na direcao contraria) e
    fica ate `persistencia` depois do fim da ULTIMA mencao da mesma
    carta em sequencia continua. Cada mencao nova dentro da janela de
    persistencia da anterior funde na mesma exibicao e reinicia o
    relogio a partir dela - e por isso que a fusao (que olha so a
    proximidade entre mencoes) e o rabo depois da ultima (que decide
    quando a carta some) usam a MESMA constante agora: as duas
    respondem "ha quanto tempo a carta nao e mencionada?"."""
    pontos = sorted(
        (_tempo_saida(m["s"], iv), _tempo_saida(m["e"], iv), m["carta"])
        for m in mencoes)

    atraso, persist = cc["atraso"], cc["persistencia"]

    grupos = []
    for ini, fim, carta in pontos:
        if (grupos and grupos[-1]["carta"] == carta
                and ini - grupos[-1]["ultima_fim"] <= persist):
            grupos[-1]["ultima_fim"] = fim
        else:
            grupos.append({"carta": carta, "primeira_fim": fim,
                            "ultima_fim": fim})

    spans = [{"carta": g["carta"],
              "ini": g["primeira_fim"] + atraso,
              "fim": g["ultima_fim"] + persist}
             for g in grupos]

    # Sem empilhamento (layout 4 ficou de fora por decisao do Rafael - ver
    # plano): se a persistencia de uma carta invade a proxima, a de tras
    # cede - a carta que comecou depois e a que esta sendo falada agora.
    for i in range(len(spans) - 1):
        if spans[i]["fim"] > spans[i + 1]["ini"]:
            spans[i]["fim"] = spans[i + 1]["ini"]

    return [s for s in spans if s["fim"] > s["ini"]]


def _agrupa_cartas_enumeracao(mencoes, iv, cc, ce):
    """Mencoes (tempo de FONTE) -> eventos de exibicao em DUAS pilhas
    (esquerda/direita), tempo de SAIDA. So para o modo `--sem-principal`
    (ver CLAUDE.md / cmd_cartas): Mariana enumerando varias cartas ou
    TIPOS de carta em sequencia, sem nenhuma ser o tema do video - por
    isso nao ha carta fixa a esquerda, e cada mencao nova alterna de
    lado e empilha sobre a anterior do MESMO lado.

    "Sessao de enumeracao": mencoes cujo intervalo desde a anterior (de
    QUALQUER carta - a pergunta e "ha quanto tempo nenhuma carta e
    mencionada", nao mais por identidade como em `_agrupa_spans_cartas`)
    fica dentro de `persistencia` ficam juntas. A 1a mencao de uma sessao
    vai para esquerda/profundidade 0, a 2a para direita/0, a 3a para
    esquerda/1 (empilha sobre a 1a), a 4a para direita/1, e por diante.
    Uma sessao nova comeca do zero porque a anterior ja sumiu da tela ha
    `persistencia` segundos sem mencao - nao ha pilha para continuar.

    Mencao de uma carta que JA esta empilhada nesta sessao (pedido do
    Rafael em 02/09: nao pode repetir carta na tela ao mesmo tempo) e
    ignorada - nao gera evento, nao consome profundidade nem lado. So
    volta a poder aparecer numa sessao NOVA (a anterior ja sumiu de vez).

    Saida em duas fases, pedido do Rafael em 02/09: a pilha da DIREITA
    (a mais nova) fica so ate `persistencia` depois da ultima mencao da
    sessao e comeca a sumir; a da ESQUERDA (a mais velha) so comeca a
    sumir quando a direita ja tiver sumido de vez, `fade_saida` segundos
    depois. Risco conhecido, nao medido no unico video de teste: se uma
    sessao nova comecar antes da anterior terminar de sumir (gap so um
    pouco maior que `persistencia`), as duas pilhas se sobrepoem por um
    instante - a de cima (a nova, desenhada por cima) cobre a que ainda
    esta sumindo embaixo, entao o efeito visivel e pequeno, mas fica
    registrado aqui para quem for medir isso num video que bata nesse
    caso."""
    pontos = sorted(
        (_tempo_saida(m["s"], iv), _tempo_saida(m["e"], iv), m["carta"])
        for m in mencoes)

    persist, atraso, fade_saida = cc["persistencia"], cc["atraso"], ce["fade_saida"]

    sessoes = []
    for ini, fim, carta in pontos:
        if sessoes and ini - sessoes[-1][-1]["fim"] <= persist:
            sessoes[-1].append({"carta": carta, "ini": ini, "fim": fim})
        else:
            sessoes.append([{"carta": carta, "ini": ini, "fim": fim}])

    LADOS = ("esquerda", "direita")
    eventos, janelas_sessao = [], []
    for sessao in sessoes:
        ultima_fim = sessao[-1]["fim"]
        dir_ini = ultima_fim + persist
        dir_fim = dir_ini + fade_saida
        esq_ini = dir_fim
        esq_fim = esq_ini + fade_saida
        fade_por_lado = {"direita": (dir_ini, dir_fim),
                          "esquerda": (esq_ini, esq_fim)}

        prof = {"esquerda": 0, "direita": 0}
        # Pedido do Rafael em 02/09: a MESMA carta nao pode estar na tela
        # duas vezes ao mesmo tempo (acontecia quando ela voltava a ser
        # mencionada depois de ja empilhada nesta sessao - as duas
        # ocorrencias ficam visiveis juntas ate o fim da sessao, ver
        # docstring). `na_pilha` e por SESSAO, nao por lado: a pergunta e
        # "essa carta ja esta em alguma das duas pilhas", nao qual lado.
        # A mencao repetida e so ignorada - nao consome profundidade nem
        # alterna o lado (`n_exibidas`, nao `i`, decide o lado de quem
        # realmente aparece), como se ela nao tivesse acontecido para
        # fins de empilhamento. Sessao NOVA (a de tras ja sumiu da tela)
        # zera `na_pilha` de novo - repetir la e uma carta nova entrando.
        na_pilha, n_exibidas = set(), 0
        for ev in sessao:
            if ev["carta"] in na_pilha:
                continue
            lado = LADOS[n_exibidas % 2]
            fade_ini, fim_lado = fade_por_lado[lado]
            eventos.append({"carta": ev["carta"], "lado": lado,
                             "prof": prof[lado], "ini": ev["fim"] + atraso,
                             "fim": fim_lado, "fade_ini": fade_ini,
                             "fade_dur": fade_saida})
            prof[lado] += 1
            na_pilha.add(ev["carta"])
            n_exibidas += 1
        janelas_sessao.append((sessao[0]["fim"] + atraso, esq_fim))

    eventos = [e for e in eventos if e["fim"] > e["ini"]]
    return eventos, janelas_sessao


# ------------------------------------------------ roteiro de cartas (mao)
#
# O outro caminho das cartas, e o que o Rafael pediu em 09/09/2026: em vez
# de o programa adivinhar pela fala (`cmd_cartas`, que continua existindo),
# ELE escreve o roteiro no "modo cartas" do editor, uma linha por carta:
#
#     [1:29] Rainha de Copas; direita; 12s
#
# A diferenca com `cmd_cartas` nao e de mecanismo - a geometria, a pilha e
# o reenquadramento sao os MESMOS (`grafo_cartas_pilhas`) - e de quem
# decide. La e regra, aqui e gente; e por isso o que sai daqui e sagrado
# como `anotacoes.json` e o que sai de la e proposta.
#
# TODO tempo do roteiro e do relogio da SAIDA (depois do corte). E o unico
# relogio que existe para quem assiste, e o mesmo do .m4a que ele ouve e do
# .srt que sai junto. Se o corte mudar depois de o roteiro ser escrito,
# cada tempo escorrega - a mesma familia do selo do transcript.md. Por isso
# o `mantido` do dia fica gravado no arquivo e `roteiro_caducou` pergunta.

# [1:29] carta; lado; tempo. Colchete opcional na leitura (a tela sempre
# escreve com ele); lado e tempo, tambem - sem lado a carta vai para o lado
# que a principal nao ocupa, sem tempo ela fica ate `rabo_final`.
#
# Duas outras formas da mesma linha, para MIDIA avulsa (foto, grafico - nao
# carta do baralho, ver catalogo_midias()). Midia nao aceita lado: so existe
# a coluna da direita para ela.
#   [1:29] grafico.png; 12s              - entra na pilha da direita, MESMO
#                                           mecanismo de empilhar/descer/
#                                           reenquadrar da carta secundaria
#   [1:29] mapa.jpg; full; 8s            - tela cheia, centralizada; TODA
#                                           carta/midia em tela some com
#                                           fade antes dela e volta com
#                                           fade depois (`_recorta_blackout`)
# `full` exige `Xs` - sem duracao declarada uma imagem de tela cheia
# tomaria conta do video ate o rabo_final, escondendo carta por acidente.
RE_LINHA_ROTEIRO = re.compile(r"^\[?\s*([0-9:.]+)\s*\]?\s*(.*)$")
RE_SEGUNDOS = re.compile(r"^([0-9]+(?:[.,][0-9]+)?)\s*s?$")
PALAVRA_FULL = "full"

# So dois lados, e o autocomplete da tela escreve os dois por extenso. As
# abreviacoes existem para quem digita a linha na mao.
LADO_ALT = {"esquerda": "esquerda", "esq": "esquerda",
            "direita": "direita", "dir": "direita"}


def _chave_carta(s):
    """Chave de comparacao de nome (carta OU midia): sem acento, sem caixa,
    sem espaco dobrado. O espaco dobrado importa - ha arquivo com dois
    espacos no nome ("10 -  a roda da fortuna.jpg") e o nome de exibicao
    herda a forma do arquivo. Serve para midia tambem: o ponto da extensao
    nao esta no conjunto que `strip()` corta, entao "grafico.png" nunca
    colide com uma chave de carta."""
    import baralho
    return " ".join(baralho.chave(str(s)).split())


def catalogo_cartas():
    """O nome de exibicao das 78 cartas, na ordem do arquivo.

    A lista que o editor oferece no autocomplete, a que o roteiro aceita e
    a de onde sai a imagem do overlay sao a MESMA - `baralho.catalogo()`,
    que le a pasta. E por isso que errar o nome de uma carta na tela e
    impossivel: as duas pontas leem o mesmo disco."""
    import baralho
    return [c["nome"] for c in baralho.catalogo()]


def catalogo_midias():
    """Os arquivos de `midias/`, na ordem do disco - a mesma garantia da
    carta, so que para foto/grafico avulso: autocomplete, parser do
    roteiro e imagem do overlay leem daqui, entao "sera impossivel
    adicionar uma imagem que nao esteja na pasta" (pedido do Rafael,
    09/09/2026).

    Pasta ausente devolve lista vazia em vez de estourar - a maioria dos
    videos nao usa midia nenhuma, e criar `midias/` vazia nao deveria ser
    pre-requisito para abrir o editor."""
    if not MIDIAS.is_dir():
        return []
    return sorted(p.name for p in MIDIAS.iterdir()
                  if p.suffix.lower() in EXT_MIDIA)


def _corta_ev_full(ev, corte_ini, corte_fim, fade_fora):
    """Um evento de carta/midia (da pilha) cortado contra a janela de uma
    imagem 'full' - devolve 0, 1 ou 2 pedacos. `corte_ini` e `fade_fora`
    segundos ANTES da imagem full comecar (onde o fade de saida tem que
    COMECAR, para terminar bem na hora); `corte_fim` e exatamente onde a
    imagem full ACABA (onde o fade de entrada de volta comeca). Quem monta
    esses dois numeros e `_recorta_blackout` - ver la o porque de nao
    serem simetricos.

    O pedido e que TODA carta/midia em tela suma ANTES da imagem full
    comecar e volte DEPOIS dela acabar - nao importa quando o evento mesmo
    ia sumir ou aparecer. Por isso o corte usa sempre `fade_fora`, nunca o
    fade original do evento, nos dois lados do buraco:

    - se o evento ja estava vivo ANTES do buraco (`ini < corte_ini`), sai
      um pedaco "antes" cujo fade de saida e forcado para terminar
      exatamente em `corte_ini` (o instante em que a imagem full comeca),
      com `fade_fora` segundos de rampa.
    - se o evento ia continuar vivo DEPOIS do buraco (`fim` original
      passa de `corte_fim`, ou o proprio fade dele ainda nao tinha
      comecado a essa altura), sai um pedaco "depois" que renasce
      exatamente em `corte_fim` (o instante em que a imagem full acaba),
      com fade de ENTRADA de `fade_fora` segundos, e mantem a MESMA
      duracao de saida que o evento original tinha.
    - inteiramente dentro do buraco (nasce e morre no meio dele) nao sai
      pedaco nenhum: e exatamente o "nao aparece durante a imagem full"."""
    ini, fade_ini, fim = ev["ini"], ev["fade_ini"], ev["fim"]
    if fim <= corte_ini or ini >= corte_fim:
        return [ev]                       # nao toca o buraco, sai igual
    pedacos = []
    if ini < corte_ini:
        pedacos.append({**ev, "fade_ini": round(corte_ini, 3),
                        "fade_dur": fade_fora,
                        "fim": round(corte_ini + fade_fora, 3)})
    if fim > corte_fim or fade_ini >= corte_fim:
        novo_fade_ini = max(fade_ini, corte_fim)
        pedacos.append({**ev, "ini": round(corte_fim, 3),
                        "fade_in": fade_fora,
                        "fade_ini": round(novo_fade_ini, 3),
                        "fim": round(novo_fade_ini + ev["fade_dur"], 3)})
    return pedacos


def _recorta_blackout(eventos, cheios, fade_fora):
    """`eventos` (a pilha de cartas/midias) cortados contra CADA imagem
    'full' de `cheios`, na ordem em que elas aparecem - ver
    `_corta_ev_full` para a regra de cada corte. Varias imagens full
    seguidas cortam em sequencia, uma de cada vez; `cheios` ja veio sem
    sobreposicao (`parse_roteiro` recusa duas imagens full que se cobrem).

    A janela que passa para `_corta_ev_full` NAO e simetrica de proposito:
    `corte_ini` ja vem com a folga de `fade_fora` subtraida (o fade de
    saida tem que TERMINAR em `c["ini"]`, entao tem que COMECAR
    `fade_fora` segundos antes); `corte_fim` e o fim da imagem full sem
    folga nenhuma (o fade de entrada tem que COMECAR em `c["fim"]` - a
    folga entra depois, como duracao desse fade, dentro de
    `_corta_ev_full`). Sem essa assimetria a carta ficaria fora de tela
    `fade_fora` segundos A MAIS depois que a imagem full ja tinha sumido,
    o que nao e "retornar com fade-in apos ela desaparecer" - e "retornar
    fade_fora segundos depois disso"."""
    janelas = sorted((c["ini"] - fade_fora, c["fim"]) for c in cheios)
    for corte_ini, corte_fim in janelas:
        novo = []
        for ev in eventos:
            novo.extend(_corta_ev_full(ev, corte_ini, corte_fim, fade_fora))
        eventos = novo
    return eventos


def parse_roteiro(texto, principal, dur_saida, C, nomes=None, midias=None):
    """O roteiro escrito a mao -> os eventos de carta/midia, em tempo de
    SAIDA.

    Devolve `(eventos, erros, notas)`, e as tres listas importam:

    - `eventos` e o que vai para a tela do video, ja com lado e
      profundidade de pilha resolvidos;
    - `erros` sao linhas que nao viraram carta/midia nenhuma. O export
      RECUSA enquanto houver uma - metade de um roteiro renderizado em
      silencio seria decisao humana sumindo, o defeito que este programa
      persegue;
    - `notas` sao linhas que viraram carta/midia com alguma diferenca do
      que estava escrito (aparada porque invadia os `rabo_final` segundos
      finais). Nao impedem nada, e sao ditas na tela pela mesma razao: o
      programa nao muda o pedido de ninguem calado.

    A PROFUNDIDADE nao esta escrita no roteiro, e nao deve estar: "carta
    adicionada ao mesmo lado enquanto houver outra da stack nessa carta" e
    uma regra sobre o QUE JA ESTA NA TELA, entao ela se conta, nao se
    digita. Quem entra conta quantas cartas do mesmo lado estao vivas
    naquele instante - e e por isso que a pilha desce sozinha quando a de
    baixo some.

    A carta PRINCIPAL e o caso especial de uma linha so: esquerda,
    profundidade 0, do `principal_entra` ate o `rabo_final`. Ela e a ancora
    - fica no fundo da pilha esquerda o video inteiro -, e por isso e a
    primeira da lista mesmo que alguma linha do roteiro comece antes dela.

    MIDIA (foto/grafico avulso, catalogo_midias() - nao carta do baralho)
    entra na MESMA pilha da direita pelo MESMO mecanismo (empilha, desce,
    abre janela de reenquadramento) - so nao aceita 'esquerda'/'direita'
    escrito, porque so existe um lado para ela. `full` e um evento
    diferente de proposito: nao empilha (fica centralizado, cobre quase o
    quadro inteiro), exige `Xs`, nao aceita se sobrepor a outra imagem full,
    e apaga (com fade, `_recorta_blackout`) TUDO que estiver na pilha
    enquanto dura - pedido do Rafael em 09/09/2026, evento raro de
    proposito ("as vezes preciso mostrar uma imagem que nao e carta")."""
    cc = C["cartas"]
    nomes = catalogo_cartas() if nomes is None else nomes
    midias = catalogo_midias() if midias is None else midias
    por_chave = {_chave_carta(n): n for n in nomes}
    por_chave_midia = {_chave_carta(m): m for m in midias}
    fade = cc["fade"]
    # depois disto nao entra carta/midia nenhuma: "os ultimos 10 segundos
    # sao so a Mariana falando". E o fim de tudo que nao declara tempo.
    corta_em = dur_saida - cc["rabo_final"]

    eventos, erros, notas, cheios_brutos = [], [], [], []

    def evento(carta, lado, ini, fade_ini, linha, escrito, tipo="carta",
               fade_dur=None, fade_in=None):
        fd = fade if fade_dur is None else fade_dur
        fi = fade if fade_in is None else fade_in
        return {"carta": carta, "lado": lado, "prof": 0, "tipo": tipo,
                "ini": round(ini, 3), "fade_ini": round(fade_ini, 3),
                "fim": round(min(dur_saida, fade_ini + fd), 3),
                "fade_dur": fd, "fade_in": fi,
                "linha": linha, "escrito": escrito}

    def erro(linha, texto, msg):
        erros.append({"linha": linha, "texto": texto, "erro": msg})

    if principal:
        nome = por_chave.get(_chave_carta(principal))
        if not nome:
            erro(0, principal, f"'{principal}' nao e uma carta do baralho")
        elif corta_em <= cc["principal_entra"]:
            erro(0, principal,
                 f"o corte tem {dur_saida:.0f}s: nao cabe a carta principal "
                 f"entre {cc['principal_entra']:.0f}s e os "
                 f"{cc['rabo_final']:.0f}s finais")
        else:
            eventos.append(evento(nome, "esquerda", cc["principal_entra"],
                                  corta_em, 0, nome))

    for n, bruta in enumerate(texto.splitlines(), start=1):
        crua = bruta.strip()
        if not crua or crua.startswith("#"):
            continue
        m = RE_LINHA_ROTEIRO.match(crua)
        t = le_tempo(m.group(1)) if m else None
        if t is None:
            erro(n, crua, "a linha tem de comecar com o tempo: "
                          "[1:29] Rainha de Copas; direita")
            continue
        campos = [x.strip() for x in m.group(2).split(";")]
        if not campos[0]:
            erro(n, crua, "falta o nome da carta ou da midia depois do tempo")
            continue

        nome_carta = por_chave.get(_chave_carta(campos[0]))
        nome_midia = por_chave_midia.get(_chave_carta(campos[0]))
        if not nome_carta and not nome_midia:
            erro(n, crua, f"'{campos[0]}' nao e carta do baralho nem midia "
                          f"da pasta {MIDIAS.name}/")
            continue
        # o nome bater nos dois catalogos ao mesmo tempo nao deveria
        # acontecer (extensao de arquivo separa os dois), mas se acontecer
        # a carta manda - e o catalogo mais antigo e mais restrito.
        eh_midia = nome_midia is not None and nome_carta is None
        nome = nome_midia if eh_midia else nome_carta

        if not eh_midia:
            # ---- carta do baralho: caminho de sempre, intocado ---------
            lado, segundos, ruim = None, None, False
            for extra in campos[1:]:
                if not extra:
                    continue
                k = _chave_carta(extra)
                if k in LADO_ALT:
                    lado = LADO_ALT[k]
                elif RE_SEGUNDOS.match(k):
                    segundos = float(RE_SEGUNDOS.match(k).group(1).replace(",", "."))
                else:
                    erro(n, crua, f"nao entendi '{extra}' - depois da carta "
                                  f"so cabe 'esquerda'/'direita' e um tempo "
                                  f"como '12s'")
                    ruim = True
                    break
            if ruim:
                continue
            # Sem lado escrito: a principal ja ocupa a esquerda, entao a
            # carta nova vai para o lado que sobra. Sem principal, a
            # esquerda e o lado de comecar. O autocomplete da tela escreve
            # o lado sempre - este padrao e para quem digita a linha na mao.
            if lado is None:
                lado = "direita" if principal else "esquerda"

            if t >= corta_em:
                erro(n, crua, f"{hhmmss(t)} ja esta nos {cc['rabo_final']:.0f}s "
                              f"finais do corte ({hhmmss(dur_saida)}), onde "
                              f"nao entra carta")
                continue
            if t < 0:
                erro(n, crua, "tempo negativo")
                continue
            if segundos is None:
                fade_ini = corta_em
            else:
                fade_ini = t + segundos
                if fade_ini > corta_em:
                    notas.append({"linha": n, "nota":
                                  f"aparei para {corta_em - t:.0f}s: os "
                                  f"{cc['rabo_final']:.0f}s finais do video "
                                  f"sao sem carta"})
                    fade_ini = corta_em
            eventos.append(evento(nome, lado, t, fade_ini, n, campos[0]))
            continue

        # ---- midia: sem lado (so a direita), 'full' opcional -----------
        full, segundos, ruim = False, None, False
        for extra in campos[1:]:
            if not extra:
                continue
            k = _chave_carta(extra)
            if k == PALAVRA_FULL:
                full = True
            elif RE_SEGUNDOS.match(k):
                segundos = float(RE_SEGUNDOS.match(k).group(1).replace(",", "."))
            else:
                erro(n, crua, f"nao entendi '{extra}' - midia so aceita "
                              f"'full' e um tempo como '12s' (ela vai "
                              f"sempre para a direita, nunca escreva o lado)")
                ruim = True
                break
        if ruim:
            continue
        if t < 0:
            erro(n, crua, "tempo negativo")
            continue
        if full and segundos is None:
            erro(n, crua, "imagem em tela cheia precisa dizer quantos "
                          "segundos fica ('full; 8s') - sem isso ela "
                          "tomaria conta do video ate o rabo_final")
            continue
        if t >= corta_em:
            erro(n, crua, f"{hhmmss(t)} ja esta nos {cc['rabo_final']:.0f}s "
                          f"finais do corte ({hhmmss(dur_saida)}), onde nao "
                          f"entra midia")
            continue

        if full:
            fade_ini = t + segundos
            if fade_ini > corta_em:
                notas.append({"linha": n, "nota":
                              f"aparei para {corta_em - t:.0f}s: os "
                              f"{cc['rabo_final']:.0f}s finais do video sao "
                              f"sem carta/midia"})
                fade_ini = corta_em
            # pop curto (FADE_POP), nao o fade macio das cartas: quem some
            # e volta suavemente sao as cartas/midias em volta dela
            # (`_recorta_blackout`, mais abaixo) - a imagem full mesma
            # entra e sai em corte seco, como a carta secundaria automatica.
            cheios_brutos.append(evento(nome, "tela cheia", t, fade_ini, n,
                                        campos[0], tipo="full",
                                        fade_dur=FADE_POP, fade_in=FADE_POP))
            continue

        # midia comum: MESMA regra da carta sem lado escrito - vai para a
        # direita (ponto 2 do pedido: "nao tem direita/esquerda como
        # parametro"). Dali para frente - pilha, reenquadramento, tamanho
        # da imagem - e o MESMO mecanismo da carta secundaria.
        if segundos is None:
            fade_ini = corta_em
        else:
            fade_ini = t + segundos
            if fade_ini > corta_em:
                notas.append({"linha": n, "nota":
                              f"aparei para {corta_em - t:.0f}s: os "
                              f"{cc['rabo_final']:.0f}s finais do video sao "
                              f"sem carta/midia"})
                fade_ini = corta_em
        eventos.append(evento(nome, "direita", t, fade_ini, n, campos[0],
                              tipo="midia"))

    # ---- a pilha se conta, nao se digita -----------------------------
    # A principal (linha 0) na frente de tudo: ela e o fundo da pilha
    # esquerda por definicao, mesmo que alguma linha do roteiro comece
    # antes dos `principal_entra` segundos dela.
    ordem = sorted(eventos, key=lambda e: (e["linha"] != 0, e["ini"], e["linha"]))
    fim = []
    for ev in ordem:
        vivos = [x for x in fim if x["fim"] > ev["ini"] + 1e-6]
        if ev["linha"] != 0:
            # a MENOR vaga livre daquele lado, e nao "quantas ha" nem "a
            # mais funda + 1": e o que faz a pilha DESCER quando a carta de
            # baixo sai, em vez de marchar para o centro a cada carta nova
            # de um video comprido. A posicao de quem ficou nao se mexe.
            ocupadas = {x["prof"] for x in vivos if x["lado"] == ev["lado"]}
            ev["prof"] = next(k for k in range(len(ocupadas) + 1)
                              if k not in ocupadas)
        # A MESMA carta/midia duas vezes na tela ao mesmo tempo e a mesma
        # coisa que o modo enumeracao ja recusava (pedido do Rafael em
        # 02/09): duas copias da mesma ilustracao deslocadas de 1% da
        # largura parecem defeito de render, nao empilhamento.
        repetida = next((x for x in vivos if x["carta"] == ev["carta"]), None)
        if repetida:
            erro(ev["linha"], ev["escrito"],
                 f"'{ev['carta']}' ja esta na tela desde {hhmmss(repetida['ini'])} "
                 f"- de um tempo para a primeira sair antes")
            continue
        choque = next((x for x in vivos if x["lado"] == ev["lado"]
                       and x["prof"] == ev["prof"]), None)
        if choque:
            erro(ev["linha"], ev["escrito"],
                 f"cai em cima de '{choque['carta']}' na mesma posicao da "
                 f"pilha - troque o lado ou o tempo")
            continue
        fim.append(ev)

    # ---- tela cheia: no maximo uma por vez, nunca duas se cobrindo ----
    # Nao entra na pilha acima - nao tem lado nem profundidade, fica
    # centralizada por cima de tudo -, entao a checagem e so tempo contra
    # tempo, sem dimensao nenhuma de posicao.
    cheios = []
    for ev in sorted(cheios_brutos, key=lambda e: (e["ini"], e["linha"])):
        choque = next((c for c in cheios if c["fim"] > ev["ini"] + 1e-6), None)
        if choque:
            erro(ev["linha"], ev["escrito"],
                 f"'{ev['carta']}' cai em cima de '{choque['carta']}', que "
                 f"ainda esta em tela cheia ate {hhmmss(choque['fim'])} - de "
                 f"um tempo para a primeira sair antes")
            continue
        cheios.append(ev)

    # ---- tela cheia apaga (e depois devolve) tudo que estava em tela --
    # Pedido do Rafael, 09/09/2026: nenhuma carta/midia da pilha continua
    # visivel enquanto a imagem de tela cheia esta em cena - ela cobre
    # quase o quadro inteiro (`midia.max_largura`/`max_altura`), e a
    # fresta que sobra nao e lugar para uma carta espiar. `_recorta_blackout`
    # forca a saida/volta com `midia.fade_cartas` segundos de fade, nao
    # importa o que a carta/midia mesma ia fazer naquele instante - ver a
    # funcao para o porque disso ser mais seguro que respeitar o fade
    # original de cada uma.
    if cheios:
        fade_fora = cc["midia"]["fade_cartas"]
        fim = _recorta_blackout(fim, cheios, fade_fora)
    fim += cheios

    # em ordem de TEMPO (a principal sempre primeiro, empate de proposito
    # com o resto da pilha - mesma chave do sort acima): a lista vai para
    # a margem do editor, que le de cima para baixo como quem assiste.
    erros.sort(key=lambda e: e["linha"])
    fim.sort(key=lambda e: (e["linha"] != 0, e["ini"], e["linha"]))
    return fim, erros, notas


def janelas_reenquadra(eventos, junta):
    """As janelas em que a Mariana fica CENTRALIZADA (o crop do layout 3).

    A regra e a que o Rafael ditou: "adicionar uma carta a direita quer
    dizer necessariamente mudar para o layout onde ela fica ao centro". Ou
    seja, a pergunta e uma so - ha carta a direita agora? -, e nao ha
    terceira resposta.

    Duas janelas separadas por menos de `junta` viram uma. Sem isso, duas
    cartas a direita a um segundo uma da outra fariam o enquadramento
    voltar ao quadro inteiro e fechar de novo - o zoom piscaria por nada, e
    seria o unico movimento de camera do video inteiro a nao dizer nada."""
    pontos = sorted((e["ini"], e["fim"]) for e in eventos
                    if e["lado"] == "direita")
    out = []
    for a, b in pontos:
        if out and a - out[-1][1] <= junta:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def roteiro_caducou(doc, mantido, tol=0.15):
    """O corte mudou depois de o roteiro ser escrito?

    Mesma pergunta, mesmo numero e mesma cegueira do `previa_no_disco` do
    editor e da fase 0 do `/anotar`: compara a duracao do corte de entao
    com a de agora, com 0,15 s de folga, e nao percebe quem tira 200 ms num
    lugar e devolve 200 ms noutro.

    Existe porque o roteiro fala no relogio da SAIDA. Um `[1:29]` escrito
    contra um corte e apontando para outro nao erra por pouco: erra tudo o
    que vem depois do ponto que mudou, e erra calado."""
    velho = (doc or {}).get("mantido")
    if velho is None:
        return None
    return {"antes": round(velho, 2), "agora": round(mantido, 2),
            "velho": abs(velho - mantido) > tol}


def carrega_roteiro(d):
    p = d / "cartas-roteiro.json"
    return jload(p) if p.exists() else None


# ---------------------------------------------------------------- legenda
#
# O .srt sai do MESMO corte que o video, e por isso ele nao pode ser feito
# do `transcript.md`: aquele arquivo e a transcricao ipsis litteris da
# fonte, com o que foi cortado ainda dentro. A legenda e o que SOBROU, no
# relogio de quem assiste.

RE_FIM_FRASE = re.compile(r"[.!?…]['\"”’)\]]*$")


def _texto_legenda(t):
    """O que este token diz na legenda, ou "" se ele nao diz nada.

    Fala recuperada de lacuna entra - e fala, e esta no video. O rotulo
    "hesitação" e o `?? N.Ns` nao entram: sao etiqueta do programa, nao
    palavra dita. E a mesma pergunta que o `pecas` do editor faz para
    decidir se um token e vao, e pela mesma razao - ali ninguem leu nada.

    A corrida de letra sai encurtada aqui tambem, e nao so no `analyze`:
    os `stream.json` analisados antes de 04/10/2026 ainda trazem o "ééé"
    de 200 letras, e uma legenda nao tem como quebrar uma palavra so."""
    txt = encurta_alongamento(str(t.get("t") or "").strip())
    if t.get("k") == "palavra":
        return txt
    if not txt or txt == "hesitação" or txt.startswith("??"):
        return ""
    return txt


def monta_legendas(stream, iv, C):
    """Os cartoes do .srt: o que sobrou do texto, no relogio da SAIDA.

    Uma palavra entra quando MAIS DA METADE dela sobrevive ao corte. Nao e
    numero novo: e a mesma regra da metade do token com que o editor decide
    o que um arrasto pegou (CLAUDE.md, "Selecionar por tempo casa por
    METADE do token"), e ela se paga aqui pelo mesmo motivo - 18 ms de uma
    palavra raspada na emenda nao sao a palavra, e a palavra inteira e.

    Onde quebrar vem do `[legenda]` do config: largura do cartao, teto de
    tempo, a pausa que fecha a frase e o piso de texto para um ponto final
    poder fechar. Nenhum desses numeros e medicao contra este projeto - nao
    ha video legendado a mao para medir - e o comentario la diz isso.

    O `s`/`e` de cada cartao e de FONTE, e nao de saida, de proposito: e
    por ele que uma legenda editada a mao se reencontra depois (mesma regra
    do `montagem.json` e da resposta de pendencia - tempo de fonte nao
    caduca, numero de ordem sim), e e por ele que o botao de play do editor
    sabe o que tocar. Um cartao pode atravessar um corte: ai o intervalo de
    fonte contem material cortado, e tocar por `iv` dentro dele devolve
    exatamente o que se ouve - que e o ponto."""
    cl = C["legenda"]
    total = sum(b - a for a, b in iv)

    palavras = []
    for t in stream:
        txt = _texto_legenda(t)
        if not txt:
            continue
        dur = max(1e-6, t["e"] - t["s"])
        vivo = sum(max(0.0, min(b, t["e"]) - max(a, t["s"])) for a, b in iv)
        if vivo <= dur / 2:
            continue
        palavras.append({"txt": txt, "s": t["s"], "e": t["e"],
                         "ini": _tempo_saida(t["s"], iv),
                         "fim": _tempo_saida(t["e"], iv)})

    cenas = []
    for p in palavras:
        c = cenas[-1] if cenas else None
        if c is None:
            quebra = True
        else:
            largura = len(c["txt"]) + 1 + len(p["txt"])
            quebra = (p["ini"] - c["fim"] > cl["pausa_quebra"]
                      or largura > cl["max_caracteres"]
                      or p["fim"] - c["ini"] > cl["max_segundos"]
                      or (c["fecha"] and len(c["txt"]) >= cl["min_caracteres"]))
        if quebra:
            cenas.append({"txt": p["txt"], "s": p["s"], "e": p["e"],
                          "ini": p["ini"], "fim": p["fim"],
                          "fecha": bool(RE_FIM_FRASE.search(p["txt"])),
                          "palavras": [p]})
        else:
            c["txt"] += " " + p["txt"]
            c["e"], c["fim"] = p["e"], p["fim"]
            c["fecha"] = bool(RE_FIM_FRASE.search(p["txt"]))
            c["palavras"].append(p)

    # O piso de tempo so ESTICA o fim, e nunca por cima do cartao seguinte:
    # legenda que pisca nao se le, legenda que se sobrepoe some no player.
    for i, c in enumerate(cenas):
        if c["fim"] - c["ini"] < cl["min_segundos"]:
            teto = cenas[i + 1]["ini"] if i + 1 < len(cenas) else total
            c["fim"] = min(teto, c["ini"] + cl["min_segundos"])

    saida = []
    for n, c in enumerate(cenas, start=1):
        saida.append({"n": n, "s": round(c["s"], 3), "e": round(c["e"], 3),
                      "ini": round(c["ini"], 3), "fim": round(c["fim"], 3),
                      "auto": c["txt"], "txt": c["txt"], "mao": False,
                      # tempo de FONTE de cada palavra do cartao, so para o
                      # editor mover o verde palavra a palavra ao tocar - so
                      # vale enquanto "mao" for False, porque legenda escrita
                      # a mao troca o texto e a lista deixa de bater com ele.
                      "palavras": [{"txt": w["txt"], "s": round(w["s"], 3),
                                    "e": round(w["e"], 3)}
                                   for w in c["palavras"]]})
    return saida


def amplia_audio_legendas(cenas, dur):
    """O trecho de fonte que o BOTAO DE TOCAR de cada cartao usa - maior
    que o [s,e] que o cartao MOSTRA, de proposito.

    [s,e] e o envelope das palavras que sobrevivem a regra da metade do
    token (`monta_legendas`, acima), e tem de ficar do jeito que esta:
    `aplica_legendas` casa a edicao a mao por ele e `escreve_srt` tira o
    tempo do .srt dele. Mas audio MANTIDO nao respeita esse envelope -
    um corte de milissegundo (`montagem.json`) ou uma palavra cujo tempo
    o alinhador errou podem deixar som que sobrevive ao corte colado a
    um dos dois lados de um cartao, sem pertencer ao texto de nenhum.

    MEDIDO no "3 de espadas" (achado do Rafael, ouvindo o modo cartas):
    depois da palavra 'capacidade' (316,06-317,26s) vem uma retomada
    cortada, 'com essa capacidade.' (317,26-319,12s) - e um restauro de
    0,80s dentro DELA (318,36-319,16s) sobrevive ao corte sem que
    NENHUM cartao alcance esse tempo: o cartao de 'capacidade' para no
    fim da propria palavra (317,26s), o proximo so comeca em 319,22s
    ('de'). No "4 de espadas", 'elaborar,' e seguido de uma hesitacao
    resgatada de 3,03s onde so 1,03s sobrevive (34%, abaixo dos 50% que
    dao entrada na legenda) - esse segundo de fala fica sem cartao
    nenhum que o toque, embora esteja em `iv` como qualquer outro trecho
    mantido. O modo onda alcanca esse audio (arrasta ou seleciona por
    tempo, sem depender de cartao nenhum); o modo cartas, tocando
    cartao por cartao, nunca chegava la - a mesma familia de defeito de
    sempre (julgamento humano que sobrevive ao corte e ninguem alcanca),
    por um caminho novo, e por isso os dois modos tem de tocar o MESMO
    audio: o modo cartas e a ultima olhada antes do render de verdade.

    A regra: cada cartao toca da sua PROPRIA palavra inicial (`s`) ate
    onde o PROXIMO cartao comeca - nunca so o que ele escreve. Isso
    reparte a fonte inteira sem sobra e sem buraco: cartao por cartao,
    em ordem, toca exatamente `iv` uma vez cada pedaco, a mesma coisa
    que o botao de tocar tudo toca de uma vez so (`audio_e` de um e
    `audio_s` do proximo saem do MESMO numero - nunca ha vao nem
    repeticao entre dois cartoes vizinhos). Nas pontas (antes do
    primeiro cartao, depois do ultimo) o limite e a propria ponta do
    video, nunca a palavra: so ali pode haver ponta reconstruida
    (`parte_ponta`) sem palavra nenhuma pendurando a metade do token."""
    n = len(cenas)
    for i, c in enumerate(cenas):
        c["audio_s"] = c["s"] if i > 0 else 0.0
        c["audio_e"] = cenas[i + 1]["s"] if i + 1 < n else dur
    return cenas


def aplica_legendas(cenas, itens, tol=0.05):
    """Poe por cima de cada cartao o texto que uma pessoa escreveu.

    Casa pelo TEMPO DE FONTE do cartao, nunca pelo numero: um corte novo no
    meio do video renumera todos os cartoes de baixo, e a legenda escrita
    para um passaria a valer para outro. Mesma regra do `s`/`e` do
    `montagem.json` e da resposta de pendencia, pela mesma razao.

    Devolve as ORFAS - as edicoes que nao acharam cartao nenhum. Elas
    acontecem quando o corte mudou a ponto de o cartao nao existir mais, e
    dizer isso e obrigatorio: sumir com o texto que alguem escreveu, calado,
    e o defeito que este programa inteiro persegue.

    `orig` fica guardado para o editor poder mostrar quando o texto DE
    BAIXO mudou depois da edicao (uma correcao de palavra, um corte novo) -
    a edicao continua valendo, por ser a mais explicita, mas quem olha
    merece saber que ela ficou para tras."""
    livres = list(itens or [])
    for c in cenas:
        casa = None
        for it in livres:
            if (abs(it.get("s", -9) - c["s"]) <= tol
                    and abs(it.get("e", -9) - c["e"]) <= tol):
                casa = it
                break
        if casa is None:
            continue
        livres.remove(casa)
        c["txt"] = casa.get("novo", c["auto"])
        c["mao"] = True
        c["orig"] = casa.get("orig", "")
    return livres


def carrega_legendas(d):
    p = d / "legendas.json"
    return jload(p) if p.exists() else None


def srt_tempo(t):
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def quebra_legenda(txt, larg):
    """Uma linha, ou duas quebradas no espaco mais perto do meio.

    Duas e o teto de propósito: a terceira linha come a imagem, e num
    cartao com o teto de `max_caracteres` ela nao chega a ser necessaria.
    Quando nem duas cabem (uma palavra enorme, um texto colado a mao maior
    que o cartao), sai como esta - aparar o que uma pessoa escreveu seria
    pior que a linha comprida."""
    txt = " ".join(str(txt).split())
    if len(txt) <= larg:
        return txt
    meio, melhor, dist = len(txt) // 2, None, len(txt)
    for i, ch in enumerate(txt):
        if ch == " " and abs(i - meio) < dist:
            melhor, dist = i, abs(i - meio)
    if melhor is None:
        return txt
    return txt[:melhor] + "\n" + txt[melhor + 1:]


def escreve_srt(cenas, path, C):
    """O .srt, em UTF-8 com BOM e fim de linha CRLF.

    Os dois sao do formato, nao gosto: e um formato de Windows, e o player
    velho que abre um .srt sem BOM em cp1252 mostra "espadas" com o acento
    quebrado. Quem le direito (YouTube, Premiere, VLC, CapCut) aceita os
    dois jeitos - so um dos dois lados erra, entao vale o lado seguro."""
    L = []
    for c in cenas:
        L.append(str(c["n"]))
        L.append(f"{srt_tempo(c['ini'])} --> {srt_tempo(c['fim'])}")
        L.append(quebra_legenda(c["txt"], C["legenda"]["max_linha"]))
        L.append("")
    with open(path, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write("\n".join(L))
    return path


def _resolve_carta(texto):
    import baralho
    alvo = baralho.chave(texto)
    for c in baralho.catalogo():
        if baralho.chave(c["nome"]) == alvo:
            return c["nome"]
    sys.exit(f"[cartas] '{texto}' nao bate com nenhuma carta do catalogo "
             f"(veja baralho.py --carta imprime a lista)")


def _catalogo_img(nome):
    """Imagem para o OVERLAY, nao para o casador de mencao: le de
    tarodeyala-borda/ (copia com 1px de borda preta, gerada por
    `bordar_cartas.py`), nunca de tarodeyala/ - `baralho.catalogo()`
    continua apontando pro original porque e dali que vem o nome de
    arquivo que casa com a fala (ver docstring de baralho.py)."""
    import baralho
    for c in baralho.catalogo():
        if c["nome"] == nome:
            img = c["img"].parent.parent / "tarodeyala-borda" / c["img"].name
            if not img.exists():
                sys.exit(f"[cartas] falta borda para '{nome}' ({img}) - "
                         f"rode bordar_cartas.py")
            return img
    sys.exit(f"[cartas] carta '{nome}' sumiu do catalogo entre a deteccao "
             f"e o render - baralho completo/ mudou no meio do caminho?")


def _midia_img(nome):
    """Imagem para o overlay de uma midia avulsa - direto de `MIDIAS`, sem
    borda nenhuma (essa e uma decisao estetica do baralho, ver
    `_catalogo_img`, e nao faz sentido para foto/grafico qualquer)."""
    img = MIDIAS / nome
    if not img.exists():
        sys.exit(f"[cartas] midia '{nome}' sumiu da pasta {MIDIAS.name}/ "
                 f"entre a escrita do roteiro e o render")
    return img


def _imagem_camada(camada):
    """A imagem de UMA camada da pilha (carta ou midia) - o dispatch que
    faz `grafo_cartas_pilhas` nao precisar saber de onde cada uma vem."""
    return (_midia_img(camada["carta"]) if camada.get("tipo") == "midia"
            else _catalogo_img(camada["carta"]))


def _previa_lores(d, C, a, meta, gscript_hw, gscript_sw, entradas, mantido,
                   saida, rotulo):
    """A previa barata: video pequeno, AUDIO FINAL. Depois abre o mpv.

    Serve aos dois caminhos de carta - o `cartas` (mencao achada na fala) e
    o `exportar --previa` (roteiro escrito a mao) -, e por isso e um lugar
    so: por que 360p/15fps, por que dois grafos, por que QSV primeiro e a
    mesma explicacao para os dois, nao duas.

    O audio recebe o MESMO loudnorm de duas passadas do arquivo de
    qualidade final - pedido do Rafael ao encomendar o modo cartas: "uma
    previa de baixissima resolucao (apenas o audio bom)". Custa uma medicao
    e um remux com `-c:v copy`, poucos segundos, e e o que faz a previa
    valer como julgamento do que vai sair - a mesma razao pela qual
    `trecho_audio` mora num lugar so."""
    bruto = d / "_previa_bruta.mp4"

    def _saidas(gscript):
        return (["-/filter_complex", str(gscript), "-map", "[vo]", "-map", "[ao]",
                 "-t", f"{mantido:.3f}",
                 "-c:v", "libx264", "-preset", "fast", "-crf", "28",
                 "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(bruto)])

    print(f"[{rotulo}] gerando previa lo-res...", flush=True)
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
    if r.returncode != 0 or not bruto.exists():
        sys.exit(f"[{rotulo}] ffmpeg falhou na previa - veja os graph_*.txt")

    ca = C["audio"]
    print(f"[{rotulo}] medindo loudness...", flush=True)
    af, entrada_lufs = _loudnorm(bruto, ca)
    if entrada_lufs is not None:
        print(f"[{rotulo}] {entrada_lufs} LUFS -> {ca['lufs_alvo']} LUFS")
    r = subprocess.run([FFMPEG, "-y", "-v", "error", "-stats", "-i", str(bruto),
                        "-c:v", "copy", "-af", af, "-c:a", "aac",
                        "-b:a", "192k", "-ar", "48000", str(saida)])
    bruto.unlink(missing_ok=True)
    if r.returncode != 0 or not saida.exists():
        sys.exit(f"[{rotulo}] ffmpeg falhou na normalizacao da previa")
    print(f"[{rotulo}] previa pronta: {saida}  "
          f"({saida.stat().st_size/1e6:.1f} MB)")
    mpv = _find_mpv()
    if mpv and not a.sem_abrir:
        subprocess.run([mpv, "--force-window=yes",
                        f"--force-media-title=previa {rotulo} "
                        f"{Path(a.video).name}", str(saida)])


def cmd_cartas(a):
    """Checkpoint 3 + 4 do plano 'adicionar cartas': acha as mencoes de
    carta na fala e renderiza uma previa barata (baixa resolucao) para
    aprovar antes do `render` de verdade bakear isso em qualidade final.
    Video sem carta reconhecida na fala nao escreve `cartas.json` nem
    gera previa - fica como generalistico, fora do escopo desta feature
    por ora.

    Dois modos, mutuamente exclusivos:

    - Padrao: decide uma carta PRINCIPAL (a mais mencionada, ou `--carta`)
      e fica fixa a esquerda a partir do fade de entrada; secundarias
      entram a direita quando mencionadas (`grafo_cartas`, layouts 1/3).

    - `--sem-principal`: pedido do Rafael em 02/09, para video que
      enumera VARIAS cartas ou TIPOS de carta (rainhas, reis, pajens...)
      sem nenhuma ser o tema - nao ha "a mais mencionada" que faca
      sentido fixar. Cada mencao alterna de lado e empilha sobre a
      anterior do mesmo lado (`grafo_cartas_pilhas`, layout 4)."""
    import baralho
    C, d, stream, dur, db, hop, anot, mont, corr = _base(a.video, "cartas")
    iv = intervalos(stream, C, dur, db, hop, anot, mont)

    if a.carta and a.sem_principal:
        sys.exit("[cartas] --carta e --sem-principal se contradizem: um "
                 "fixa a principal, o outro diz que nao ha principal")

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

    meta = jload(d / "meta.json")
    w_fonte, h_fonte = meta["w"], meta["h"]

    import rosto
    rjson = d / "rosto.json"
    if not rjson.exists() or a.force:
        rosto.rastreia(Path(a.video), d)
    resumo_rosto = jload(rjson)["resumo"]

    if a.sem_principal:
        cc, ce = C["cartas"], C["cartas"]["enumeracao"]
        eventos, janelas_sessao = _agrupa_cartas_enumeracao(mencoes, iv, cc, ce)

        if not eventos:
            print("[cartas] nenhuma sequencia de enumeracao sobrou depois "
                  "do atraso/persistencia - o video fica no enquadramento "
                  "original o tempo todo")
            (d / "cartas.json").unlink(missing_ok=True)
            return

        print(f"[cartas] --sem-principal: {len(eventos)} carta(s) em "
              f"{len(janelas_sessao)} sequencia(s) de enumeracao "
              f"(tempo de SAIDA, ja cortado):")
        for e in eventos:
            print(f"  {hhmmss(e['ini'])} -> {hhmmss(e['fim'])}  "
                  f"{e['lado']:8s} prof {e['prof']}  {e['carta']}")

        cartas = {"modo": "enumeracao", "eventos": eventos,
                  "sessoes": [[a, b] for a, b in janelas_sessao]}
        jdump(cartas, d / "cartas.json")

        # Uma camada de overlay por POSICAO de pilha, nao por mencao - ver
        # a nota em `_agrupa_camadas` sobre por que isso e o
        # que faz esta previa terminar em minutos, nao quase uma hora.
        camadas = _agrupa_camadas(eventos)
        print(f"[cartas] {len(eventos)} mencao(oes) cabem em "
              f"{len(camadas)} posicao(oes) de pilha distintas "
              f"({len(camadas)} camada(s) de overlay)")
        imgs = [_imagem_camada(c) for c in camadas]
        gscript_hw = d / "graph_cartas_qsv.txt"
        gscript_hw.write_text(
            grafo_cartas_pilhas(iv, C, camadas, janelas_sessao,
                                 resumo_rosto, w_fonte, h_fonte, imgs,
                                 escala=360, fps=15, hw=True,
                                 hlg=saida_hlg(meta)),
            encoding="utf-8")
        gscript_sw = d / "graph_cartas.txt"
        gscript_sw.write_text(
            grafo_cartas_pilhas(iv, C, camadas, janelas_sessao,
                                 resumo_rosto, w_fonte, h_fonte, imgs,
                                 escala=360, fps=15, hw=False,
                                 hlg=saida_hlg(meta)),
            encoding="utf-8")

        mantido = sum(b - x for x, b in iv)
        entradas = ["-i", str(meta["src"])]
        for img in imgs:
            entradas += ["-loop", "1", "-i", str(img)]

        _previa_lores(d, C, a, meta, gscript_hw, gscript_sw, entradas, mantido,
                  d / "cartas_previa.mp4", "cartas")
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
                     secundarias_imgs, escala=360, fps=15, hw=True,
                     hlg=saida_hlg(meta)),
        encoding="utf-8")
    gscript_sw = d / "graph_cartas.txt"
    gscript_sw.write_text(
        grafo_cartas(iv, C, cartas, resumo_rosto, w_fonte, h_fonte,
                     secundarias_imgs, escala=360, fps=15, hw=False,
                     hlg=saida_hlg(meta)),
        encoding="utf-8")

    mantido = sum(b - x for x, b in iv)
    entradas = (["-i", str(meta["src"]), "-loop", "1", "-i", str(principal_img)]
                + [x for _, p in secundarias_imgs
                   for x in ("-loop", "1", "-i", str(p))])

    _previa_lores(d, C, a, meta, gscript_hw, gscript_sw, entradas, mantido,
                  d / "cartas_previa.mp4", "cartas")


def _corta_final(d, C, meta, g_hw, g_sw, entradas, mantido, saida, rotulo,
                 emendas):
    """O arquivo de qualidade final: corte pelo grafo, depois loudnorm de
    duas passadas. Devolve o Path do que ficou pronto.

    Extraida de `cmd_render` para o `exportar` nao ter uma SEGUNDA receita
    de codificacao: o video com carta e o video sem carta tem de sair com o
    mesmo codec, a mesma qualidade, os mesmos tags de cor e o mesmo
    tratamento de audio - a unica diferenca entre eles e o que o
    filter_complex desenha por cima. Duas receitas seriam duas respostas
    para "como este projeto codifica video", e a que diverge calada e
    sempre a que ninguem esta olhando (mesmo raciocinio do `_grafo_corte`
    e do `trecho_audio`).

    Dois grafos, QSV primeiro e software se aquele falhar: com decode QSV o
    [0:v] pode chegar aos filtros em memoria de GPU, e so o grafo `hw=True`
    poe o `hwdownload` que isso pede (ver `grafo_cartas`). Sem carta os
    dois sao o mesmo arquivo, e a queda para software so tira o
    `-hwaccel`."""
    cr = C["render"]
    corte = d / "_corte.mov"
    hevc = saida_hlg(meta)
    enc = ["-c:v", "hevc_qsv" if hevc else "h264_qsv",
           "-global_quality", str(cr["qualidade"]),
           "-pix_fmt", "p010le" if hevc else "nv12"]
    if not cr["gpu"]:
        enc = ["-c:v", "libx265" if hevc else "libx264",
               "-crf", str(cr["qualidade"]), "-preset", "medium",
               "-pix_fmt", "yuv420p10le" if hevc else "yuv420p"]
    # O formato do encoder e PINADO, nunca negociado. A carta entra no
    # grafo como `yuva420p` - ela precisa do alfa para o fade -, e o que
    # sai do `overlay` chega ao encoder ainda carregando esse alfa. Cada
    # encoder reage de um jeito, e os dois errados:
    #
    #   libx265 RECUSA ("Loaded libx265 does not support alpha layer
    #   encoding") e o `exportar` de qualidade morre sem escrever um
    #   quadro - por isso a queda para software nao salvava nada aqui.
    #
    #   hevc_qsv ACEITA, porque a lista dele tem `bgra` e `vuyx`, e
    #   escolhe 4:4:4. MEDIDO no "2 de espadas": o arquivo saiu
    #   `HEVC Rext / yuv444p / gbr`. O ffmpeg reabre esse arquivo sem
    #   reclamar e o mpv toca; o Reprodutor do Windows mostra um mosaico
    #   colorido, porque Rext 4:4:4 nao e perfil que decodificador de
    #   consumo saiba ler. Os quadros estavam certos - o perfil e que era
    #   impossivel, e nada no caminho perguntava qual perfil ia sair.
    #
    # `-color_range tv` vem do mesmo lugar: a carta e um JPEG
    # `yuvj420p`, de faixa cheia, e o grafo adotava a faixa dela. MEDIDO
    # no mesmo quadro, o luma medio sai 679 (min 0, max 1023) sem esta
    # linha contra 644 com ela - o mesmo video com carta saia mais
    # contrastado que sem carta.
    #
    # O `render` nao sofria de nenhum dos dois: sem carta no grafo ele ja
    # saia `Main 10 / yuv420p10le / tv` sozinho. MEDIDO: framemd5
    # identico nos 180 quadros com e sem o pino. Pinar so no caminho das
    # cartas seria a segunda receita de codificacao que esta funcao
    # existe para nao ter.
    enc += ["-color_range", "tv"]
    cor = ["-color_primaries", "bt2020", "-color_trc", "arib-std-b67",
           "-colorspace", "bt2020nc", "-tag:v", "hvc1"] if hevc else []

    def _cmd(dec, g):
        # "-/filter_complex arquivo" le o grafo de um arquivo. Necessario:
        # sao centenas de emendas, nao cabem na linha de comando. (O antigo
        # -filter_complex_script saiu no ffmpeg 9.)
        # O `-t` nao e cinto de seguranca: e o unico fim que este grafo
        # tem. Carta e midia entram como `-loop 1 -i carta.png`, que e
        # entrada INFINITA, e o `overlay` do ffmpeg 9 nao acaba quando o
        # video principal acaba - ele repete o ultimo quadro e segue no
        # ritmo da imagem (25 fps) para sempre. MEDIDO no "2 de espadas":
        # o corte de 00:02:14 saiu com 170.948 quadros e 01:53:30 de video
        # (o audio, que vem do concat, tinha os 134,65 s certos), depois de
        # 1h02 de ffmpeg e sem nada reclamar. O `_previa_lores` sempre teve
        # este mesmo `-t` - por isso a previa das cartas nunca sofreu disso
        # e so o arquivo de qualidade sofria, que e o unico que ninguem
        # roda duas vezes. Sem carta a entrada ja e finita e o `-t` nao
        # muda um byte; poe-lo so no caminho das cartas seria a segunda
        # receita de codificacao que esta funcao existe para nao ter.
        return ([FFMPEG, "-y", "-v", "error", "-stats"] + dec + entradas
                + ["-/filter_complex", str(g), "-map", "[vo]", "-map", "[ao]",
                   "-t", f"{mantido:.3f}"]
                + enc + cor
                + ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(corte)])

    print(f"[{rotulo}] cortando ({emendas} emendas)...", flush=True)
    r = None
    if cr["gpu"]:
        # `-hwaccel_output_format qsv` nao e opcional: sem ele o ffmpeg 9
        # ainda entrega quadro em memoria de GPU por padrao, e um grafo de
        # software (o `scale` comum) quebra com "Impossible to convert
        # between the formats" - foi assim que a tentativa QSV falhava
        # sempre, 100% das vezes, e o render caia pro software calado.
        r = subprocess.run(_cmd(["-hwaccel", "qsv",
                                  "-hwaccel_output_format", "qsv"], g_hw))
        if r.returncode != 0:
            print(f"[{rotulo}] o caminho QSV falhou; refazendo em software",
                  flush=True)
    if r is None or r.returncode != 0:
        r = subprocess.run(_cmd([], g_sw))
    if r.returncode != 0 or not corte.exists():
        sys.exit(f"[{rotulo}] ffmpeg falhou no corte")

    # ---- audio: loudnorm de duas passadas -----------------------------
    ca = C["audio"]
    print(f"[{rotulo}] medindo loudness...", flush=True)
    af, entrada_lufs = _loudnorm(corte, ca)
    print(f"[{rotulo}] entrada {entrada_lufs} LUFS -> alvo "
          f"{ca['lufs_alvo']} LUFS" if entrada_lufs is not None
          else f"[{rotulo}] medicao falhou; loudnorm em passada unica")
    print(f"[{rotulo}] normalizando audio...", flush=True)
    r = subprocess.run([FFMPEG, "-y", "-v", "error", "-stats", "-i",
                        str(corte), "-c:v", "copy", "-af", af,
                        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                        str(saida)])
    if r.returncode != 0:
        sys.exit(f"[{rotulo}] ffmpeg falhou no audio")
    corte.unlink(missing_ok=True)
    return saida


def cmd_render(a):
    C, d, stream, dur, iv = _plano(a, "render")
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
    # Dois grafos, nao um: com decode QSV o [0:v] so serve ao grafo `hw=True`
    # (ver a nota em grafo()) - mandar o mesmo arquivo pros dois lados era o
    # bug que fazia a tentativa QSV falhar sempre e o render cair pro
    # software calado.
    gscript_hw = d / "graph_qsv.txt"
    gscript_hw.write_text(
        grafo(iv, C, escala, fps, hw=True, w_fonte=meta["w"], h_fonte=meta["h"]),
        encoding="utf-8")
    gscript_sw = d / "graph.txt"
    gscript_sw.write_text(grafo(iv, C, escala, fps, hw=False), encoding="utf-8")

    # Em export/finais, junto com o do `exportar`: e o mesmo video pronto,
    # so que sem carta - ver pastas.py.
    saida = Path(a.out) if a.out else pastas.onde(
        meta["src"], Path(a.video).stem, pastas.CORTADO)
    saida.parent.mkdir(parents=True, exist_ok=True)
    # `meta["src"]`, nao `a.video`: ver a mesma nota no `cmd_audio`.
    _corta_final(d, C, meta, gscript_hw, gscript_sw, ["-i", str(meta["src"])],
                 mantido, saida, "render", len(iv))
    mb = saida.stat().st_size / 1e6
    print(f"[render] pronto: {saida}  ({hhmmss(mantido)}, {mb:.0f} MB)")

def _monta_cartas(C, eventos, d, meta, force, rotulo):
    """O que o ffmpeg precisa para desenhar um roteiro: as camadas da
    pilha (uma entrada de imagem cada), as imagens em tela cheia (idem),
    as janelas de reenquadramento e o resumo do rosto.

    O rastreio de rosto so roda quando ha carta a DIREITA - e o unico caso
    em que a Mariana e reenquadrada (layout 3). Sem isso, um video que so
    usa a carta principal pagaria ~1,5 min de deteccao para alimentar um
    `crop` que a expressao `if(0, ...)` nunca liga. Imagem em tela cheia
    nao entra nessa conta - ela nao abre janela de reenquadramento (nao
    tem lado), so cobre o quadro por cima de tudo por um tempo."""
    camadas = _agrupa_camadas(eventos)
    imgs = [_imagem_camada(c) for c in camadas]
    cheios = [ev for ev in eventos if ev.get("tipo") == "full"]
    imgs_full = [_midia_img(c["carta"]) for c in cheios]
    janelas = janelas_reenquadra(eventos, C["cartas"]["junta_janela"])
    resumo = {}
    if janelas:
        import rosto
        rjson = d / "rosto.json"
        if not rjson.exists() or force:
            print(f"[{rotulo}] medindo onde o rosto anda (uma vez por "
                  f"video)...", flush=True)
            rosto.rastreia(Path(meta["src"]), d)
        doc = jload(rjson)
        resumo = doc["resumo"]
        livre = faixa_livre(C, meta["w"], meta["h"], camadas)
        jan = _janela_layout3(resumo, C["cartas"]["crop_margem"], livre,
                              C["cartas"]["crop_margem_lateral"])
        fora = rosto_atras_da_carta(doc, jan, livre)
        # A unica parte disto que nao se justifica sozinha na previa: quem
        # olha ve UM enquadramento e nao sabe se era o melhor possivel.
        print(f"[{rotulo}] reenquadramento: zoom {1/max(1e-6, jan[2]):.2f}x, "
              f"faixa livre entre as cartas {livre[0]:.2f}-{livre[1]:.2f}"
              + (f", o rosto passa atras de uma carta em {fora*100:.0f}% do "
                 f"tempo" if fora is not None else ""))
    return camadas, imgs, cheios, imgs_full, janelas, resumo


def cmd_exportar(a):
    """O video pronto do modo cartas: o corte, as cartas do roteiro e o
    .srt, tudo de uma vez.

    E o fim do caminho que comeca no editor: `cartas-roteiro.json` diz que
    carta aparece quando e de que lado, `legendas.json` diz o que cada
    cartao de legenda escreve, e este comando desenha os dois sobre o corte
    que o `montagem.json` deixou pronto.

    Duas saidas, a mesma decisao: `--previa` da 360p/15fps com o AUDIO
    FINAL, para conferir a dança das cartas em minutos; sem ela sai o
    arquivo de qualidade, na resolucao do original (ou no `[render].perfil`
    que estiver escolhido). As duas leem o mesmo roteiro e passam pelo
    mesmo `grafo_cartas_pilhas` - se a previa esta certa, o final esta.

    RECUSA quando o roteiro tem linha ilegivel, e nao pula a linha: meio
    roteiro renderizado calado seria decisao humana sumindo pelo caminho
    novo, que e o defeito que este programa persegue desde o primeiro dia.
    Erro de roteiro, porem, nao e erro de corte - o editor mostra os
    mesmos erros vermelhos na tela, na linha em que estao, antes de alguem
    chegar aqui."""
    C, d, stream, dur, iv = _plano(a, "exportar")
    meta = jload(d / "meta.json")
    mantido = sum(b - x for x, b in iv)
    stem = Path(a.video).stem

    doc = carrega_roteiro(d) or {}
    eventos, erros, notas = parse_roteiro(doc.get("roteiro", ""),
                                          doc.get("principal"), mantido, C)
    for n in notas:
        print(f"[exportar] linha {n['linha']}: {n['nota']}")
    if erros:
        for e in erros:
            onde = ("a carta principal" if e["linha"] == 0
                    else f"linha {e['linha']}")
            print(f"[exportar] {onde}: {e['erro']}")
        sys.exit("[exportar] abortado: o roteiro de cartas tem linha que o "
                 "programa nao sabe desenhar. Conserte no modo cartas do "
                 "editor - la o erro aparece na propria linha.")

    caducou = roteiro_caducou(doc, mantido)
    if caducou and caducou["velho"]:
        print(f"[exportar] AVISO: o roteiro foi escrito contra um corte de "
              f"{hhmmss(caducou['antes'])} e o de agora tem "
              f"{hhmmss(caducou['agora'])}. O relogio do roteiro e o da "
              f"SAIDA: todo tempo depois da primeira diferenca escorregou. "
              f"Abra o modo cartas e confira antes de publicar.")

    escala = fps = None
    if a.previa:
        escala, fps = 360, 15
    elif C["render"]["perfil"] == "1080p30":
        escala, fps = 1080, 30

    # A previa em export/prévias, o final em export/finais, e o .srt ao lado
    # de cada um (ver pastas.py).
    saida = Path(a.out) if a.out else pastas.onde(
        meta["src"], stem, pastas.PREVIA_CARTAS if a.previa else pastas.FINAL)
    saida.parent.mkdir(parents=True, exist_ok=True)

    # ---- legenda ------------------------------------------------------
    if not a.sem_legenda:
        cenas = monta_legendas(stream, iv, C)
        orfas = aplica_legendas(
            cenas, (carrega_legendas(d) or {}).get("itens", []))
        for o in orfas:
            print(f"[exportar] AVISO: a legenda escrita a mao em "
                  f"{o.get('s', 0):.2f}s da fonte nao casa com cartao "
                  f"nenhum - o corte mudou depois dela: {o.get('novo','')[:50]}")
        srt = saida.with_suffix(".srt")
        escreve_srt(cenas, srt, C)
        n_mao = sum(1 for c in cenas if c.get("mao"))
        print(f"[exportar] legenda: {len(cenas)} cartoes "
              f"({n_mao} escritos a mao) -> {srt}")

    # ---- video --------------------------------------------------------
    if eventos:
        camadas, imgs, cheios, imgs_full, janelas, resumo = _monta_cartas(
            C, eventos, d, meta, a.force, "exportar")
        print(f"[exportar] {len(eventos)} carta(s)/midia(s) em "
              f"{len(camadas)} camada(s) de overlay, "
              f"{len(janelas)} janela(s) de reenquadramento"
              + (f", {len(cheios)} em tela cheia" if cheios else ""))
        for e in eventos:
            print(f"  {hhmmss(e['ini'])} -> {hhmmss(e['fim'])}  "
                  f"{e['lado']:8s} pilha {e['prof']}  {e['carta']}")
        g_hw, g_sw = d / "graph_export_qsv.txt", d / "graph_export.txt"
        for p, hw in ((g_hw, True), (g_sw, False)):
            p.write_text(
                grafo_cartas_pilhas(iv, C, camadas, janelas, resumo,
                                    meta["w"], meta["h"], imgs,
                                    cheios=cheios, imgs_full=imgs_full,
                                    escala=escala, fps=fps, hw=hw,
                                    hlg=saida_hlg(meta)),
                encoding="utf-8")
        entradas = (["-i", str(meta["src"])]
                    + [x for img in imgs for x in ("-loop", "1", "-i", str(img))]
                    + [x for img in imgs_full for x in ("-loop", "1", "-i", str(img))])
    else:
        # Sem roteiro nenhum o `exportar` continua servindo: e o `render`
        # com a legenda junto. Nao ha carta para desenhar, entao nao ha
        # razao para o grafo caro.
        print("[exportar] roteiro vazio: sai o corte sem carta nenhuma")
        g_hw = g_sw = d / "graph_export.txt"
        g_hw.write_text(grafo(iv, C, escala, fps), encoding="utf-8")
        entradas = ["-i", str(meta["src"])]

    print(f"[exportar] {len(iv)} trechos, {hhmmss(mantido)} de {hhmmss(dur)} "
          f"(corta {100*(1-mantido/dur):.0f}%)")
    if a.previa:
        _previa_lores(d, C, a, meta, g_hw, g_sw, entradas, mantido, saida,
                      "exportar")
    else:
        _corta_final(d, C, meta, g_hw, g_sw, entradas, mantido, saida,
                     "exportar", len(iv))
        print(f"[exportar] pronto: {saida}  ({hhmmss(mantido)}, "
              f"{saida.stat().st_size/1e6:.0f} MB)")


# ---------------------------------------------------------------------- lote

EXTS = {".mov", ".mp4", ".mkv", ".m4v", ".avi"}

# A ordem do pipeline, uma vez so. Ja morou em tres lugares - a lista de
# nomes do `lote`, a lista de pares logo abaixo dela e o `choices` do
# argparse -, e acrescentar etapa exigia lembrar dos tres.
ETAPAS = [("ingest", cmd_ingest), ("transcribe", cmd_transcribe),
          ("align", cmd_align), ("analyze", cmd_analyze),
          ("draft", cmd_draft), ("render", cmd_render)]


def etapa_isolada(nome, a):
    """Roda UMA etapa num processo Python proprio. -> codigo de saida.

    O `run` rodava as cinco etapas no mesmo processo, e a maquina tem 7,9
    GB: o Whisper do `transcribe`, o wav2vec2 do `align` e o Whisper de
    novo no `analyze` iam se empilhando, e o `mkl_malloc` matava ora o
    `transcribe`, ora o `analyze` - com saida diferente a cada vez, e as
    vezes (exit 255) sem mensagem nenhuma. A receita que funcionava era
    rodar as etapas uma a uma, a mao. Processo que termina devolve toda a
    memoria ao sistema; e isso que esta funcao faz por quem chama.

    E o codigo de saida e conferido: uma etapa que morre para a fila ali,
    em vez de o `draft` seguinte escrever um transcript.md por cima de um
    `stream.json` velho (visto em 01/10/2026 no "arcanos menores 2")."""
    import os
    cmd = [sys.executable, str(Path(__file__).resolve()), nome, str(a.video)]
    if nome == "render":
        if getattr(a, "ignorar_transcript", False):
            cmd.append("--ignorar-transcript")
    else:
        if getattr(a, "force", False):
            cmd.append("--force")
        if getattr(a, "model", None):
            cmd += ["--model", a.model]
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    sys.stdout.flush()
    return subprocess.run(cmd, env=env).returncode


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
                                ignorar_transcript=a.ignorar_transcript)
        # cada etapa num processo proprio: ver `etapa_isolada`
        for nome, _ in ETAPAS[:ate + 1]:
            rc = etapa_isolada(nome, ns)
            if rc:
                print(f"[lote] FALHOU em {v.name}, na etapa {nome} "
                      f"(codigo {rc})")
                falhas.append(v.name)
                break
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
        if nome == "analyze":
            # so o `analyze` le isto, e de proposito o `run` nao repassa:
            # quem quer refazer a analise de um video editado tem de pedir
            # com todas as letras, uma etapa so (ver `edicao_a_mao`)
            p.add_argument("--apesar-da-edicao", action="store_true")
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
    ct.add_argument("--sem-principal", action="store_true",
                    help="layout 4 (empilhamento): sem carta fixa, para "
                         "video que enumera varias cartas/tipos sem "
                         "nenhuma ser o tema (ex.: rainhas, reis, pajens)")
    ct.add_argument("--force", action="store_true",
                    help="refaz o rastreio de rosto mesmo se ja tiver cache")
    ct.add_argument("--sem-abrir", action="store_true",
                    help="gera a previa mas nao abre o mpv sozinho")
    ct.set_defaults(fn=cmd_cartas)

    ex = sub.add_parser("exportar",
                        help="o video pronto: corte + cartas do roteiro + "
                             ".srt (o que o modo cartas do editor exporta)")
    ex.add_argument("video")
    ex.add_argument("--previa", action="store_true",
                    help="360p/15fps com o audio final, para conferir as "
                         "cartas em minutos em vez de horas")
    ex.add_argument("--sem-legenda", action="store_true",
                    help="nao escreve o .srt ao lado do video")
    ex.add_argument("--sem-abrir", action="store_true",
                    help="gera a previa mas nao abre o mpv sozinho")
    ex.add_argument("--out", default=None)
    ex.add_argument("--force", action="store_true",
                    help="refaz o rastreio de rosto mesmo se ja tiver cache")
    ex.add_argument("--ignorar-transcript", action="store_true")
    ex.set_defaults(fn=cmd_exportar)

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
        # cada etapa num processo proprio (ver `etapa_isolada`): cinco
        # modelos empilhados no mesmo processo nao cabiam em 7,9 GB
        for nome, _ in ETAPAS[:-1]:
            rc = etapa_isolada(nome, a)
            if rc:
                sys.exit(f"[run] a etapa '{nome}' falhou (codigo {rc}). As "
                         f"anteriores ficaram em cache: rode de novo e o "
                         f"`run` recomeca dali.")
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
