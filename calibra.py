import subprocess, sys, wave, os, shutil
from pathlib import Path
import numpy as np

FF = shutil.which("ffmpeg") or next(
    (str(p) for p in Path(os.environ["LOCALAPPDATA"]).glob(
        "Microsoft/WinGet/Packages/Gyan.FFmpeg*/**/bin/ffmpeg.exe")), None)

def perfil(src, rotulo):
    tmp = Path(os.environ["TEMP"]) / "_cal.wav"
    subprocess.run([FF, "-y", "-v", "error", "-i", str(src), "-vn",
                    "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                    str(tmp)], check=True)
    w = wave.open(str(tmp), "rb"); sr = w.getframerate()
    pcm = np.frombuffer(w.readframes(w.getnframes()),
                        dtype=np.int16).astype(np.float32) / 32768.0
    w.close()
    dur = len(pcm) / sr
    hop = 0.02; n = int(hop * sr)
    fr = pcm[:len(pcm)//n*n].reshape(-1, n)
    db = 20*np.log10(np.sqrt((fr**2).mean(axis=1) + 1e-12))
    piso = np.percentile(db, 10); voz = db > piso + 12

    # blocos contiguos de silencio
    sil, cur = [], 0
    for v in voz:
        if not v: cur += 1
        elif cur: sil.append(cur*hop); cur = 0
    if cur: sil.append(cur*hop)
    longos = [s for s in sil if s > 0.6]

    r = subprocess.run([FF, "-hide_banner", "-nostats", "-i", str(src),
                        "-vn", "-af", "ebur128", "-f", "null", "-"],
                       capture_output=True, text=True, errors="replace")
    lufs = "?"
    for ln in r.stderr.splitlines():
        if ln.strip().startswith("I:") and "LUFS" in ln:
            lufs = ln.split()[1]
    print(f"{rotulo:<34} {dur/60:5.1f}min  voz {100*voz.mean():3.0f}%  "
          f"silencio {100*(1-voz.mean()):3.0f}%  "
          f"pausas>0.6s: {len(longos):3d} ({sum(longos):5.0f}s = "
          f"{100*sum(longos)/dur:3.0f}%)  maior {max(sil) if sil else 0:4.1f}s  "
          f"{lufs} LUFS")
    tmp.unlink(missing_ok=True)

print(f"{'':34} {'dur':>6}   {'':13} {'':13} pausas longas")
print("=== EDITADOS A MAO (gabarito) ===")
for f in sorted(Path("/c/Users/marip/Videos".replace("/c/","C:/")).glob("*.mov")):
    if not f.name.startswith("IMG_02"):
        perfil(f, f.stem)
print("=== BRUTOS ===")
for f in sorted(Path("C:/Users/marip/Videos").glob("IMG_02*.MOV")):
    perfil(f, f.stem)
