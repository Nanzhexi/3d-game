"""第一步：用离线中文语音模型（sherpa-onnx + MeloTTS）合成旁白，并生成时间轴。

输出：
  build/voice.wav      —— 整条旁白音轨（44.1kHz 单声道）
  build/timeline.json  —— 每个场景/每句旁白的起止时间，供 render.py 使用
"""
import json
import os
import re
import sys

import numpy as np
import sherpa_onnx
import soundfile as sf

from script import SCENES

ASSETS = os.environ.get("ASSETS", "assets")
BUILD = "build"
MODEL = os.path.join(ASSETS, "vits-melo-tts-zh_en")

LEAD_IN = 1.0      # 场景开始到第一句的留白
GAP = 0.6          # 句与句之间
TAIL = 1.6         # 最后一句之后到下一场景
SPEED = 0.9


def normalize(text):
    text = re.sub(r"[：；、—]", "，", text)
    text = re.sub(r"[？！]", "。", text)
    return re.sub(r"[《》“”]", "", text)


def main():
    os.makedirs(BUILD, exist_ok=True)
    cfg = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                model=f"{MODEL}/model.onnx",
                lexicon=f"{MODEL}/lexicon.txt",
                tokens=f"{MODEL}/tokens.txt",
                dict_dir=f"{MODEL}/dict",
            ),
            num_threads=4,
        ),
        rule_fsts=f"{MODEL}/date.fst,{MODEL}/phone.fst,{MODEL}/number.fst",
    )
    tts = sherpa_onnx.OfflineTts(cfg)

    sr = None
    chunks, timeline, t = [], [], 0.0
    for si, scene in enumerate(SCENES):
        start = t
        lines = []
        if scene["lines"]:
            t += LEAD_IN
            for li, text in enumerate(scene["lines"]):
                audio = tts.generate(normalize(text), sid=0, speed=SPEED)
                sr = sr or audio.sample_rate
                samples = np.asarray(audio.samples, dtype=np.float32)
                # 去掉首尾静音，时间轴更准
                nz = np.nonzero(np.abs(samples) > 0.01)[0]
                if len(nz):
                    samples = samples[max(nz[0] - 400, 0): nz[-1] + 2000]
                dur = len(samples) / sr
                chunks.append((t, samples))
                lines.append(dict(text=text, start=round(t - start, 3), dur=round(dur, 3)))
                t += dur + (GAP if li < len(scene["lines"]) - 1 else TAIL)
                print(f"[{si:02d}.{li}] {dur:5.2f}s  {text}", file=sys.stderr)
        else:
            t += scene["hold"]
        timeline.append(dict(key=scene["key"], chapter=scene["chapter"],
                             start=round(start, 3), dur=round(t - start, 3), lines=lines))

    total = t
    voice = np.zeros(int(total * sr) + sr, dtype=np.float32)
    for at, samples in chunks:
        i = int(at * sr)
        voice[i:i + len(samples)] += samples
    peak = np.max(np.abs(voice)) or 1.0
    voice *= 0.89 / peak
    sf.write(os.path.join(BUILD, "voice.wav"), voice, sr)
    with open(os.path.join(BUILD, "timeline.json"), "w") as f:
        json.dump(dict(total=round(total, 3), sample_rate=sr, scenes=timeline),
                  f, ensure_ascii=False, indent=1)
    print(f"total {total:.1f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
