# 永恒轮回 —— 尼采最沉重的思想

一部约 6 分 20 秒、1080p 的哲学讲解短片，全部由代码生成：旁白使用离线中文语音合成，画面由 skia 逐帧程序绘制。

## 内容结构

| 章节 | 内容 |
| --- | --- |
| 序 · 湖畔的岩石 | 1881 年 8 月，西尔斯玛利亚湖畔，思想的诞生 |
| 一 · 恶魔的耳语 | 《快乐的科学》§341 原文：存在的永恒沙漏 |
| 二 · 两种回答 | 诅咒，还是"你是神"——"最重的分量" |
| 三 · 不是宇宙论，而是审判 | 轮回作为思想实验与自我审判 |
| 四 · 从直线到圆 | 彼岸、上帝之死、虚无主义，以及翻转出的肯定 |
| 五 · 名为瞬间的门 | 《查拉图斯特拉》中"瞬间"之门与两条永恒之路 |
| 六 · 牧人与黑蛇 | "咬下去！"——吞下全部重量后依然大笑 |
| 七 · 热爱命运 | Amor fati，与"认命"的区别 |
| 八 · 那么，苦难呢 | 万物相连：肯定一刻即肯定全部 |
| 九 · 最重的分量 | 昆德拉的"轻与重"，以及每天都可以问的问题 |
| 终 · 再来一次 | 这一生，你愿意再来一次吗？ |

## 如何重新生成

```bash
pip install sherpa-onnx soundfile numpy skia-python pillow
# skia 需要 libEGL：apt-get install -y libegl1

# 素材（不入库）：
mkdir -p assets && cd assets
curl -LO https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/vits-melo-tts-zh_en.tar.bz2
tar xjf vits-melo-tts-zh_en.tar.bz2
curl -LO https://raw.githubusercontent.com/notofonts/noto-cjk/main/Serif/SubsetOTF/SC/NotoSerifSC-Regular.otf
curl -LO https://raw.githubusercontent.com/notofonts/noto-cjk/main/Serif/SubsetOTF/SC/NotoSerifSC-Bold.otf
cd ..

python3 tts.py      # 合成旁白 → build/voice.wav, build/timeline.json
python3 render.py   # 渲染画面并合成 → output/eternal_recurrence.mp4
```

- `script.py`：旁白文本与场景划分，改这里即可改写内容。
- `tts.py`：逐句合成语音并生成时间轴（字幕与画面事件都按它对齐）。
- `render.py`：每个场景一个绘制函数，多进程渲染后拼接，并叠加程序生成的配乐。
  调试单帧：`python3 render.py --still gateway 12.5`。
