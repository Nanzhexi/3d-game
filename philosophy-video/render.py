"""第二步：根据 build/timeline.json 逐帧绘制画面（skia），并与旁白、配乐合成为成片。

    python3 render.py            # 渲染全部场景并合成 output/eternal_recurrence.mp4
    python3 render.py --still lake 12.5   # 只导出某场景某一秒的静帧，便于调试
"""
import json
import math
import os
import random
import subprocess
import sys
from multiprocessing import Pool

import numpy as np
import skia

ASSETS = os.environ.get("ASSETS", "assets")
BUILD = "build"
OUT = "output"
W, H, FPS = 1920, 1080, 30
CX, CY = W / 2, H / 2

# ---------------------------------------------------------------- 字体与色彩
TF_SERIF = skia.Typeface.MakeFromFile(os.path.join(ASSETS, "NotoSerifSC-Regular.otf"))
TF_SERIF_B = skia.Typeface.MakeFromFile(os.path.join(ASSETS, "NotoSerifSC-Bold.otf"))
TF_ITALIC = skia.Typeface.MakeFromFile("/usr/share/fonts/truetype/freefont/FreeSerifItalic.ttf")

GOLD = (0.88, 0.72, 0.43)
PALE = (0.93, 0.90, 0.85)
CRIMSON = (0.62, 0.16, 0.14)
INK = (0.03, 0.035, 0.05)


def font(size, tf=TF_SERIF):
    f = skia.Font(tf, size)
    f.setSubpixel(True)
    f.setEdging(skia.Font.Edging.kAntiAlias)
    return f


# ---------------------------------------------------------------- 小工具
def clamp(x, a=0.0, b=1.0):
    return a if x < a else b if x > b else x


def smooth(x):
    x = clamp(x)
    return x * x * (3 - 2 * x)


def ease_io(x):
    x = clamp(x)
    return 0.5 - 0.5 * math.cos(math.pi * x)


def ramp(t, a, b):
    """t 在 [a, b] 之间平滑地从 0 走到 1。"""
    return smooth((t - a) / (b - a)) if b > a else float(t >= a)


def window(t, a, b, fade=0.6):
    return ramp(t, a, a + fade) * (1 - ramp(t, b - fade, b))


def lerp(a, b, k):
    return a + (b - a) * k


def mix(c1, c2, k):
    return tuple(lerp(x, y, k) for x, y in zip(c1, c2))


def paint(color=PALE, a=1.0, stroke=None, blur=0.0, shader=None):
    p = skia.Paint(AntiAlias=True)
    p.setColor4f(skia.Color4f(color[0], color[1], color[2], clamp(a)))
    if stroke:
        p.setStyle(skia.Paint.kStroke_Style)
        p.setStrokeWidth(stroke)
        p.setStrokeCap(skia.Paint.kRound_Cap)
        p.setStrokeJoin(skia.Paint.kRound_Join)
    if blur > 0:
        p.setMaskFilter(skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, blur))
    if shader is not None:
        p.setShader(shader)
    return p


def c4(color, a=1.0):
    return skia.Color4f(color[0], color[1], color[2], clamp(a)).toColor()


def text(cv, s, x, y, size, color=PALE, a=1.0, tf=TF_SERIF, align="center", glow=0.0, spacing=0.0):
    if a <= 0.003:
        return
    f = font(size, tf)
    if spacing:
        widths = [f.measureText(ch) for ch in s]
        total = sum(widths) + spacing * (len(s) - 1)
        x0 = x - total / 2 if align == "center" else x
        for ch, w in zip(s, widths):
            text(cv, ch, x0, y, size, color, a, tf, "left", glow)
            x0 += w + spacing
        return
    w = f.measureText(s)
    x0 = x - w / 2 if align == "center" else x - w if align == "right" else x
    if glow:
        cv.drawString(s, x0, y, f, paint(color, a * 0.8, blur=glow))
    cv.drawString(s, x0, y, f, paint(color, a))


def background(cv, top, bottom):
    sh = skia.GradientShader.MakeLinear([(0, 0), (0, H)], [c4(top), c4(bottom)])
    cv.drawRect(skia.Rect(0, 0, W, H), paint(shader=sh))


def vignette(cv, strength=0.75):
    sh = skia.GradientShader.MakeRadial((CX, CY), W * 0.62,
                                        [c4(INK, 0), c4(INK, 0), c4(INK, strength)], [0, 0.55, 1])
    cv.drawRect(skia.Rect(0, 0, W, H), paint(shader=sh))


def glow_dot(cv, x, y, r, color, a=1.0):
    sh = skia.GradientShader.MakeRadial((x, y), r, [c4(color, a), c4(color, a * 0.25), c4(color, 0)], [0, 0.3, 1])
    cv.drawCircle(x, y, r, paint(shader=sh))


# ------------------------------------------------------------ 共享的随机素材
RNG = random.Random(341)
STARS = [(RNG.random() * W, RNG.random() * H * 0.62, RNG.random() ** 3 * 2.2 + 0.4, RNG.random() * 6.28)
         for _ in range(260)]
DUST = [(RNG.random(), RNG.random(), RNG.random() * 6.28, RNG.random() * 0.6 + 0.4) for _ in range(90)]


def stars(cv, t, a=1.0):
    for x, y, r, ph in STARS:
        tw = 0.55 + 0.45 * math.sin(t * 1.3 + ph * 3)
        cv.drawCircle(x, y, r, paint(PALE, a * tw * 0.8))


def dust(cv, t, a=0.5, color=GOLD, rise=1.0):
    for u, v, ph, sp in DUST:
        x = (u * W + math.sin(t * 0.3 * sp + ph) * 40) % W
        y = (v * H - t * 18 * sp * rise) % H
        tw = 0.5 + 0.5 * math.sin(t * 2 * sp + ph)
        cv.drawCircle(x, y, 1.6 * sp, paint(color, a * tw))


def ridge(seed, base, amp, n=7):
    rng = random.Random(seed)
    waves = [(rng.uniform(0.002, 0.012) * (k + 1) ** 0.6, rng.random() * 6.28, amp / (k + 1) ** 0.9)
             for k in range(n)]
    return [(x, base - sum(a * (0.5 + 0.5 * math.sin(f * x + p)) for f, p, a in waves))
            for x in range(-20, W + 40, 12)]


MOUNTAINS = [ridge(1, 660, 330), ridge(2, 690, 220), ridge(3, 710, 120)]


# ================================================================== 各场景
class Ctx:
    """当前场景的时间信息。"""

    def __init__(self, scene, t):
        self.t = t
        self.dur = scene["dur"]
        self.lines = scene["lines"]

    def L(self, i):
        """第 i 句旁白开始的时间（i 可为负，越界则取场景结尾）。"""
        if -len(self.lines) <= i < len(self.lines):
            return self.lines[i]["start"]
        return self.dur

    def Lend(self, i):
        return self.lines[i]["start"] + self.lines[i]["dur"]


def scene_title(cv, c):
    t = c.t
    background(cv, (0.02, 0.025, 0.04), (0.05, 0.04, 0.05))
    dust(cv, t, 0.35)
    sweep = ease_io(t / 3.2) * 360
    r = 330
    if sweep > 0.5:
        path = skia.Path()
        path.addArc(skia.Rect(CX - r, CY - r - 20, CX + r, CY + r - 20), -90, sweep)
        cv.drawPath(path, paint(GOLD, 0.35, stroke=6, blur=8))
        cv.drawPath(path, paint(GOLD, 0.9, stroke=1.6))
        ang = math.radians(-90 + sweep)
        glow_dot(cv, CX + r * math.cos(ang), CY - 20 + r * math.sin(ang), 26, GOLD, 1 - ramp(t, 3.0, 3.6))
    a = ramp(t, 1.4, 3.0)
    text(cv, "永恒轮回", CX, CY + 20, 150, PALE, a, TF_SERIF_B, glow=14, spacing=24)
    text(cv, "Die ewige Wiederkunft des Gleichen", CX, CY + 110, 38, GOLD, ramp(t, 2.4, 3.8) * 0.85, TF_ITALIC)
    text(cv, "尼  采  ·  最  沉  重  的  思  想", CX, CY + 400, 30, PALE, ramp(t, 3.2, 4.6) * 0.6)
    vignette(cv)


def draw_lake(cv, t, rock_glow=0.0, beam=0.0, sky_text=0.0, zoom=1.0):
    cv.save()
    cv.translate(CX, CY)
    cv.scale(zoom, zoom)
    cv.translate(-CX, -CY)
    background(cv, (0.035, 0.05, 0.11), (0.30, 0.20, 0.27))
    stars(cv, t, 0.9)
    horizon = 720
    # 远山与倒影
    shades = [(0.11, 0.11, 0.18), (0.07, 0.075, 0.12), (0.04, 0.045, 0.07)]
    for pts, col in zip(MOUNTAINS, shades):
        path = skia.Path()
        path.moveTo(-20, horizon)
        for x, y in pts:
            path.lineTo(x, y)
        path.lineTo(W + 40, horizon)
        path.close()
        cv.drawPath(path, paint(col))
    # 水面
    water = skia.GradientShader.MakeLinear([(0, horizon), (0, H)], [c4((0.16, 0.12, 0.18)), c4((0.02, 0.025, 0.05))])
    cv.drawRect(skia.Rect(0, horizon, W, H), paint(shader=water))
    for pts, col in zip(MOUNTAINS, shades):
        path = skia.Path()
        path.moveTo(-20, horizon)
        for i, (x, y) in enumerate(pts):
            path.lineTo(x + 4 * math.sin(t * 1.2 + i * 0.7), horizon + (horizon - y) * 0.55)
        path.lineTo(W + 40, horizon)
        path.close()
        cv.drawPath(path, paint(col, 0.55))
    for k in range(26):
        y = horizon + 8 + k ** 1.45 * 4.2
        x0 = (k * 397 + t * (20 + k * 3)) % W
        cv.drawLine(x0, y, x0 + 120 + k * 9, y, paint(PALE, 0.05 + 0.03 * math.sin(t + k), stroke=1.2))
    # 金字塔形的岩石
    rx, ry = 1340, horizon + 6
    rock = skia.Path()
    rock.moveTo(rx - 150, ry)
    rock.lineTo(rx - 40, ry - 210)
    rock.lineTo(rx + 10, ry - 232)
    rock.lineTo(rx + 160, ry)
    rock.close()
    if beam > 0:
        sh = skia.GradientShader.MakeLinear([(0, 0), (0, ry)], [c4(GOLD, 0), c4(GOLD, 0.28 * beam)])
        b = skia.Path()
        b.moveTo(rx - 30, 0)
        b.lineTo(rx + 40, 0)
        b.lineTo(rx + 130, ry)
        b.lineTo(rx - 120, ry)
        b.close()
        cv.drawPath(b, paint(shader=sh, blur=18))
    if rock_glow > 0:
        cv.drawPath(rock, paint(GOLD, 0.6 * rock_glow, stroke=10, blur=16))
    cv.drawPath(rock, paint((0.02, 0.02, 0.03)))
    if rock_glow > 0:
        cv.drawPath(rock, paint(GOLD, 0.8 * rock_glow, stroke=1.5))
    if sky_text > 0:
        r = 150
        cv.drawCircle(CX - 260, 300, r, paint(GOLD, 0.5 * sky_text, stroke=1.4))
        cv.drawCircle(CX - 260, 300, r, paint(GOLD, 0.25 * sky_text, stroke=8, blur=10))
        text(cv, "永恒轮回", CX - 260, 318, 64, PALE, sky_text, TF_SERIF_B, glow=10, spacing=8)
    cv.restore()


def scene_lake(cv, c):
    t = c.t
    draw_lake(cv, t, rock_glow=ramp(t, c.L(1), c.L(1) + 1.5), beam=ramp(t, c.L(2), c.L(2) + 2.0),
              sky_text=ramp(t, c.L(3), c.L(3) + 1.5), zoom=1.0 + 0.06 * t / c.dur)
    dust(cv, t, 0.25)
    vignette(cv, 0.6)


def hourglass_shape(cv, sand_top, sand_bot, stream, a=1.0):
    """以原点为中心绘制沙漏。sand_* 取 0~1。"""
    hw, hh, neck = 150, 210, 9
    glass = skia.Path()
    glass.moveTo(-hw, -hh)
    glass.cubicTo(-hw, -60, -neck, -30, -neck, 0)
    glass.cubicTo(-neck, 30, -hw, 60, -hw, hh)
    glass.lineTo(hw, hh)
    glass.cubicTo(hw, 60, neck, 30, neck, 0)
    glass.cubicTo(neck, -30, hw, -60, hw, -hh)
    glass.close()
    cv.drawPath(glass, paint(PALE, 0.04 * a))
    # 沙
    cv.save()
    cv.clipPath(glass, skia.ClipOp.kIntersect, True)
    sand = paint(GOLD, 0.85 * a)
    if sand_top > 0.001:
        level = -hh * 0.06 - (hh * 0.85) * sand_top
        cv.drawRect(skia.Rect(-hw, level, hw, 0), sand)
    if sand_bot > 0.001:
        level = hh - (hh * 0.8) * sand_bot
        mound = skia.Path()
        mound.moveTo(-hw, hh)
        mound.lineTo(-hw, level + 40)
        mound.quadTo(0, level - 40, hw, level + 40)
        mound.lineTo(hw, hh)
        mound.close()
        cv.drawPath(mound, sand)
    cv.restore()
    if stream:
        cv.drawLine(0, 0, 0, hh - 4, paint(GOLD, 0.7 * a, stroke=2.2))
    cv.drawPath(glass, paint(PALE, 0.55 * a, stroke=2))
    for s in (-1, 1):
        cv.drawRoundRect(skia.Rect(-hw - 40, s * hh - 12, hw + 40, s * hh + 12), 6, 6, paint((0.25, 0.2, 0.15), a))
        cv.drawRoundRect(skia.Rect(-hw - 40, s * hh - 12, hw + 40, s * hh + 12), 6, 6,
                         paint(GOLD, 0.6 * a, stroke=1.5))
    for s in (-1, 1):
        cv.drawLine(s * (hw + 26), -hh, s * (hw + 26), hh, paint((0.45, 0.36, 0.25), a, stroke=7))


def spider_web(cv, x, y, R, a):
    if a <= 0:
        return
    p = paint(PALE, 0.35 * a, stroke=1)
    spokes = 11
    for i in range(spokes):
        ang = i / spokes * 6.283
        cv.drawLine(x, y, x + R * math.cos(ang), y + R * math.sin(ang), p)
    for ring in range(1, 9):
        r = R * ring / 9
        path = skia.Path()
        for i in range(spokes + 1):
            ang = i / spokes * 6.283
            px, py = x + r * math.cos(ang), y + r * math.sin(ang)
            path.moveTo(px, py) if i == 0 else path.lineTo(px, py)
        cv.drawPath(path, p)
    cv.drawCircle(x + 6, y + 4, 7, paint((0.05, 0.05, 0.05), a))
    cv.drawCircle(x + 6, y + 4, 7, paint(PALE, 0.5 * a, stroke=1))


def scene_hourglass(cv, c):
    t = c.t
    background(cv, (0.02, 0.025, 0.05), (0.06, 0.05, 0.08))
    stars(cv, t, 0.35)
    # 月亮与林间月光
    moon = ramp(t, c.L(5), c.L(5) + 1.5)
    glow_dot(cv, 1600, 210, 260, PALE, 0.10 + 0.25 * moon)
    cv.drawCircle(1600, 210, 62, paint(PALE, 0.25 + 0.6 * moon))
    spider_web(cv, 230, 190, 230, ramp(t, c.L(5), c.L(5) + 1.2))
    # 翻转时刻：越来越快
    flips = [c.L(2) + 3.0, c.L(4) + 2.0, c.L(6) + 1.0]
    k = c.L(6) + 3.0
    step = 2.2
    while k < c.dur:
        flips.append(k)
        k += step
        step = max(0.9, step * 0.8)
    # 沙漏上下对称：翻转时把“沙全在下方”的沙漏转 180°，转完即等价于“沙全在上方”
    FLIP = 0.9
    prev, frac, rot = 0.0, 1.0, 0.0
    for f in flips:
        if t < f:
            frac = (t - prev) / (f - prev)
            break
        if t < f + FLIP:
            frac, rot = 1.0, ease_io((t - f) / FLIP)
            break
        prev = f + FLIP
    cv.save()
    cv.translate(CX, CY - 30)
    cv.rotate(180 * rot)
    glow_dot(cv, 0, 0, 380, GOLD, 0.10)
    hourglass_shape(cv, 1 - frac, frac, rot == 0.0 and frac < 1.0)
    cv.restore()
    # 环绕沙漏的词
    words = ["痛苦", "欢乐", "念头", "叹息", "渺小", "伟大", "孤独", "这一刻"]
    wa = window(t, c.L(3), c.Lend(4) + 0.8, 1.0)
    for i, w in enumerate(words):
        ang = i / len(words) * 6.283 + t * 0.12
        appear = ramp(t, c.L(3) + i * 0.7, c.L(3) + i * 0.7 + 0.8)
        text(cv, w, CX + 420 * math.cos(ang), CY - 20 + 300 * math.sin(ang), 40, PALE,
             wa * appear * 0.75, glow=6)
    dust(cv, t, 0.25 + 0.4 * ramp(t, c.L(6), c.L(6) + 2), GOLD)
    vignette(cv)


def particles_vertical(cv, t, x0, x1, color, a, direction, seed):
    rng = random.Random(seed)
    for _ in range(70):
        u, v, sp, ph = rng.random(), rng.random(), rng.uniform(0.4, 1.0), rng.random() * 6.28
        x = x0 + u * (x1 - x0) + math.sin(t * sp + ph) * 15
        y = (v * H + direction * t * 60 * sp) % H
        cv.drawCircle(x, y, 2.2 * sp, paint(color, a * (0.4 + 0.6 * sp)))


def scene_split(cv, c):
    t = c.t
    background(cv, INK, INK)
    focus = 0.5 + 0.17 * ramp(t, c.L(1), c.L(1) + 1.0) - 0.34 * ramp(t, c.L(2), c.L(2) + 1.2) \
        + 0.17 * ramp(t, c.L(3), c.L(3) + 1.2)
    xd = W * focus
    both = 1 - ramp(t, c.L(3), c.L(3) + 1.0)
    # 左：诅咒
    sh = skia.GradientShader.MakeRadial((xd * 0.5, CY), W * 0.5, [c4((0.24, 0.05, 0.05)), c4((0.04, 0.01, 0.015))])
    cv.drawRect(skia.Rect(0, 0, xd, H), paint(shader=sh, a=1))
    particles_vertical(cv, t, 0, xd, (0.55, 0.45, 0.42), 0.35, 1, 7)
    # 右：神圣
    sh = skia.GradientShader.MakeRadial((xd + (W - xd) * 0.5, CY), W * 0.5,
                                        [c4((0.42, 0.32, 0.16)), c4((0.07, 0.05, 0.03))])
    cv.drawRect(skia.Rect(xd, 0, W, H), paint(shader=sh))
    particles_vertical(cv, t, xd, W, GOLD, 0.6, -1, 8)
    cv.drawLine(xd, 0, xd, H, paint(PALE, 0.4, stroke=1.5))
    la = ramp(t, c.L(1), c.L(1) + 1.0) * both
    ra = ramp(t, c.L(2), c.L(2) + 1.0) * both
    text(cv, "诅咒", xd * 0.5, CY + 70, 200, (0.75, 0.3, 0.27), la * 0.8, TF_SERIF_B, glow=20)
    text(cv, "Fluch", xd * 0.5, CY + 150, 40, PALE, la * 0.5, TF_ITALIC)
    text(cv, "神圣", xd + (W - xd) * 0.5, CY + 70, 200, (1.0, 0.86, 0.6), ra * 0.85, TF_SERIF_B, glow=24)
    text(cv, "göttlich", xd + (W - xd) * 0.5, CY + 150, 40, PALE, ra * 0.5, TF_ITALIC)
    # 最重的分量
    q = ramp(t, c.L(3), c.L(3) + 1.4)
    if q > 0:
        cv.drawRect(skia.Rect(0, 0, W, H), paint(INK, 0.75 * q))
        pulse = 1 + 0.015 * math.sin(t * 2.2)
        cv.save()
        cv.translate(CX, CY)
        cv.scale(pulse, pulse)
        text(cv, "最重的分量", 0, 30, 130, PALE, q, TF_SERIF_B, glow=16, spacing=18)
        text(cv, "Das größte Schwergewicht", 0, 120, 44, GOLD, q * 0.9, TF_ITALIC)
        cv.restore()
    text(cv, "?", CX, 260, 120, PALE, window(t, 0.3, c.L(1) + 0.6, 0.6) * 0.5, TF_ITALIC)
    vignette(cv, 0.6)


def scene_orbits(cv, c):
    t = c.t
    background(cv, (0.02, 0.03, 0.05), (0.03, 0.03, 0.06))
    stars(cv, t, 0.25)
    P = 7.0
    mirror = ramp(t, c.L(3), c.L(3) + 2.5)
    ox, oy = CX, CY - 40
    shrink = 1 - 0.55 * mirror
    bodies = [(70 + 52 * k, k + 1, mix(GOLD, PALE, k / 7)) for k in range(8)]
    for r, k, col in bodies:
        cv.drawCircle(ox, oy, r * shrink, paint(PALE, 0.07, stroke=1))
    phase = (t % P) / P
    aligned = max(0.0, 1 - min(phase, 1 - phase) * P / 0.35)
    if aligned > 0:
        cv.drawLine(ox, oy, ox + 520 * shrink, oy, paint(GOLD, 0.55 * aligned, stroke=2))
        cv.drawLine(ox, oy, ox + 520 * shrink, oy, paint(GOLD, 0.35 * aligned, stroke=10, blur=10))
    for r, k, col in bodies:
        ang = 6.2832 * k * t / P
        # 拖尾
        trail = skia.Path()
        for j in range(25):
            a2 = ang - j * 0.02 * k
            px, py = ox + r * shrink * math.cos(a2), oy + r * shrink * math.sin(a2)
            trail.moveTo(px, py) if j == 0 else trail.lineTo(px, py)
        cv.drawPath(trail, paint(col, 0.35, stroke=3))
        x, y = ox + r * shrink * math.cos(ang), oy + r * shrink * math.sin(ang)
        glow_dot(cv, x, y, 26, col, 0.8)
        cv.drawCircle(x, y, 5, paint(col))
    glow_dot(cv, ox, oy, 60, PALE, 0.5)
    n = int(t // P)
    text(cv, f"第 {n + 1} 次重复" if t > P * 0.5 else "", W - 160, 140, 34, GOLD,
         0.5 + 0.5 * aligned * (1 - mirror), align="right")
    # “假如”
    ja = window(t, c.Lend(1) - 1.2, c.L(3), 0.8)
    text(cv, "假如", 300, CY + 30, 120, PALE, ja * 0.9, TF_SERIF_B, glow=14)
    text(cv, "Gesetzt…", 300, CY + 100, 38, GOLD, ja * 0.7, TF_ITALIC)
    # 镜子
    if mirror > 0:
        R = 360
        sh = skia.GradientShader.MakeLinear([(ox - R, oy - R), (ox + R, oy + R)],
                                            [c4((0.5, 0.55, 0.65), 0.0), c4((0.7, 0.75, 0.85), 0.12 * mirror),
                                             c4((0.5, 0.55, 0.65), 0.0)])
        cv.drawCircle(ox, oy, R, paint(shader=sh))
        cv.drawCircle(ox, oy, R, paint(PALE, 0.7 * mirror, stroke=2))
        cv.drawCircle(ox, oy, R + 14, paint(GOLD, 0.35 * mirror, stroke=1))
        text(cv, "你", ox, oy + 150, 110, PALE, ramp(t, c.L(3) + 3.0, c.L(3) + 4.5) * 0.9, TF_SERIF_B, glow=16)
    vignette(cv)


def scene_line_circle(cv, c):
    t = c.t
    cold = ramp(t, c.L(3), c.L(4) + 1.0)
    warm = ramp(t, c.L(6), c.L(6) + 2.5)
    top = mix(mix((0.04, 0.05, 0.10), (0.03, 0.03, 0.035), cold), (0.10, 0.07, 0.04), warm)
    background(cv, top, INK)
    stars(cv, t, 0.3 * (1 - cold))
    morph = ease_io((t - c.L(5)) / 3.5)
    R = 300
    y_bot, y_top = 950, 140
    collapse = ramp(t, c.L(3), c.L(3) + 2.2)
    col = mix(mix(PALE, (0.45, 0.45, 0.48), cold), GOLD, warm)
    pts = []
    for i in range(241):
        s = i / 240
        lx, ly = CX, y_bot - s * (y_bot - y_top)
        ang = math.pi / 2 + 6.2832 * s
        qx, qy = CX + R * math.cos(ang), CY + R * math.sin(ang)
        pts.append((lerp(lx, qx, morph), lerp(ly, qy, morph)))
    # 上帝死后，直线从顶端开始断裂
    visible = 1.0 - 0.45 * collapse * (1 - morph)
    path = skia.Path()
    for i, (x, y) in enumerate(pts):
        if i / 240 > visible + morph:
            break
        path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
    if warm > 0:
        cv.drawPath(path, paint(GOLD, 0.5 * warm, stroke=16, blur=16))
    cv.drawPath(path, paint(col, 0.85, stroke=2.4))
    # 彼岸之光
    heaven = ramp(t, c.L(1), c.L(1) + 1.5) * (1 - collapse)
    flick = 1 - 0.5 * collapse * (0.5 + 0.5 * math.sin(t * 40))
    if heaven > 0.01:
        glow_dot(cv, CX, y_top - 10, 180, PALE, 0.8 * heaven * flick)
        text(cv, "彼岸", CX + 120, y_top + 10, 44, PALE, heaven, align="left")
        for i, w in enumerate(["天堂", "来世", "最终的目的"]):
            text(cv, w, CX - 120, y_top + 60 + i * 60, 30, PALE,
                 heaven * ramp(t, c.L(1) + 2.2 + i * 0.7, c.L(1) + 3 + i * 0.7) * 0.6, align="right")
    text(cv, "此生", CX + 120, y_bot - 10, 40, PALE, ramp(t, c.L(2), c.L(2) + 1) * (1 - morph) * 0.8,
         align="left")
    if collapse > 0:
        text(cv, "？", CX, y_top + 40, 70, PALE, collapse * (1 - morph) * 0.5)
    # 沿路行走的“生命”
    speed = 0.07 + 0.08 * morph
    s = (t * speed) % 1.0 if morph > 0.5 else min((t * 0.045) % 1.0, visible)
    idx = int(s * 240)
    x, y = pts[min(idx, len(pts) - 1)]
    glow_dot(cv, x, y, 40, mix(PALE, GOLD, warm), 0.9)
    cv.drawCircle(x, y, 5, paint(PALE))
    text(cv, "虚无主义", CX, CY + 12, 54, PALE, window(t, c.L(4), c.L(6) + 0.5, 0.8) * (1 - warm) * 0.75,
         TF_SERIF_B, spacing=10)
    text(cv, "Nihilismus", CX, CY + 70, 30, PALE, window(t, c.L(4), c.L(6) + 0.5, 0.8) * (1 - warm) * 0.5,
         TF_ITALIC)
    if warm > 0:
        for i in range(36):
            ang = i / 36 * 6.2832 + t * 0.05
            r0, r1 = R + 30, R + 60 + 40 * warm + 20 * math.sin(t * 2 + i)
            cv.drawLine(CX + r0 * math.cos(ang), CY + r0 * math.sin(ang),
                        CX + r1 * math.cos(ang), CY + r1 * math.sin(ang), paint(GOLD, 0.5 * warm, stroke=2))
        text(cv, "是", CX, CY + 40, 140, PALE, ramp(t, c.L(6) + 2.5, c.L(6) + 4.0), TF_SERIF_B, glow=18)
    dust(cv, t, 0.2 + 0.3 * warm)
    vignette(cv)


def road_x(u, side):
    return CX + side * (W / 2 + 60) * (1 - math.exp(-u * 0.55))


def road_scale(u):
    return math.exp(-u * 0.55)


def gate(cv, x, y, s, a, glow=0.0):
    w, h = 170 * s, 340 * s
    p = paint(GOLD, a, stroke=max(1.0, 5 * s))
    if glow > 0:
        cv.drawRect(skia.Rect(x - w / 2, y - h, x + w / 2, y), paint(GOLD, 0.25 * glow * a, blur=30 * s))
    cv.drawLine(x - w / 2, y, x - w / 2, y - h, p)
    cv.drawLine(x + w / 2, y, x + w / 2, y - h, p)
    cv.drawLine(x - w / 2 - 25 * s, y - h, x + w / 2 + 25 * s, y - h, p)
    cv.drawLine(x - w / 2 - 10 * s, y - h + 28 * s, x + w / 2 + 10 * s, y - h + 28 * s,
                paint(GOLD, a * 0.7, stroke=max(1.0, 3 * s)))


def scene_gateway(cv, c):
    t = c.t
    background(cv, (0.03, 0.035, 0.07), (0.07, 0.06, 0.08))
    stars(cv, t, 0.45)
    gy = 720
    # 两条通向永恒的路
    for side in (-1, 1):
        for edge in (-1, 1):
            path = skia.Path()
            for i in range(80):
                u = i * 0.12
                x, y = road_x(u, side), gy + edge * 70 * road_scale(u)
                path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
            cv.drawPath(path, paint(PALE, 0.35, stroke=1.6))
        # 路面虚线：时间从过去流向未来
        for k in range(40):
            u = (k * 0.45 + side * t * 0.35) % 18
            s = road_scale(u)
            x = road_x(u, side)
            cv.drawLine(x - 18 * s, gy, x + 18 * s, gy, paint(GOLD, 0.6 * s, stroke=max(0.8, 3 * s)))
    lab = ramp(t, c.L(1), c.L(1) + 1.2)
    text(cv, "过去 · 一个永恒", 330, gy - 120, 40, PALE, lab * 0.8)
    text(cv, "未来 · 又一个永恒", W - 330, gy - 120, 40, PALE, lab * 0.8)
    # 幽灵般的门：这一瞬间早已存在过
    ghosts = ramp(t, c.L(2) + 2.0, c.L(2) + 6.0)
    threads = ramp(t, c.L(3), c.L(3) + 2.0)
    for side in (-1, 1):
        for k in range(1, 9):
            u = k * 1.05
            s = road_scale(u)
            x = road_x(u, side)
            ga = ghosts * ramp(ghosts, (k - 1) / 9, k / 9) * 0.55
            gate(cv, x, gy, s, ga)
            if threads > 0:
                path = skia.Path()
                path.moveTo(CX, gy - 340)
                path.quadTo((CX + x) / 2, gy - 520 - 30 * k, x, gy - 340 * s)
                cv.drawPath(path, paint(GOLD, 0.25 * threads * (1 - k / 10), stroke=1.2))
    heavy = ramp(t, c.L(4), c.L(4) + 2.5)
    if heavy > 0:
        sh = skia.GradientShader.MakeLinear([(0, 0), (0, gy)], [c4(GOLD, 0), c4(GOLD, 0.35 * heavy)])
        cv.drawRect(skia.Rect(CX - 110, 0, CX + 110, gy), paint(shader=sh, blur=24))
    gate(cv, CX, gy, 1.0, ramp(t, 0.2, 2.0), glow=0.5 + heavy)
    name = ramp(t, c.L(0) + 3.5, c.L(0) + 5)
    text(cv, "瞬间", CX, gy - 370, 64, PALE, name, TF_SERIF_B, glow=12, spacing=10)
    text(cv, "Augenblick", CX, gy + 130, 36, GOLD, name * 0.8, TF_ITALIC)
    vignette(cv)


def serpent_spine(t, s, tight, offset=(0.0, 0.0)):
    r = (330 - 200 * tight) * (1 - 0.72 * s) + 12 * math.sin(14 * s - 3.2 * t)
    ang = 5.3 * math.pi * s + t * (0.5 + 0.6 * tight)
    return CX + offset[0] + r * math.cos(ang), CY - 40 + offset[1] + r * 0.82 * math.sin(ang)


def scene_serpent(cv, c):
    t = c.t
    bite = c.L(3)
    lit = ramp(t, c.L(4), c.L(4) + 1.5)
    relapse = window(t, c.L(5), c.L(6) + 0.5, 1.0)
    final = ramp(t, c.L(6), c.L(6) + 2.0)
    bright = clamp(lit - 0.6 * relapse + final)
    background(cv, mix((0.02, 0.035, 0.03), (0.16, 0.11, 0.05), bright), mix(INK, (0.06, 0.04, 0.02), bright))
    # 被缠绕的人：中心的微光
    tight = ramp(t, c.L(0), c.L(2))
    core = 0.7 - 0.45 * tight * (1 - ramp(t, bite, bite + 0.5)) + 1.2 * lit
    glow_dot(cv, CX, CY - 40, 120 + 380 * bright, GOLD, clamp(core * 0.6))
    cv.drawCircle(CX, CY - 40, 9 + 6 * bright, paint(PALE, clamp(core)))
    # 光芒
    if bright > 0:
        for i in range(48):
            ang = i / 48 * 6.2832 + t * 0.04
            r0 = 90
            r1 = 260 + 420 * bright + 60 * math.sin(i * 2.7 + t * 1.5)
            cv.drawLine(CX + r0 * math.cos(ang), CY - 40 + r0 * math.sin(ang),
                        CX + r1 * math.cos(ang), CY - 40 + r1 * math.sin(ang), paint(GOLD, 0.22 * bright, stroke=2))
    # 黑蛇
    after = t - bite
    body_alpha = 1.0 if after < 0 else clamp(1 - after / 1.6)
    head_off = (0.0, 0.0) if after < 0 else (900 * ease_io(after / 1.8), -500 * ease_io(after / 1.8))
    head_alpha = 1.0 if after < 0 else clamp(1 - after / 1.8)
    shake = 14 * math.exp(-max(0, after) * 4) * math.sin(after * 70) if after > 0 else 0
    cv.save()
    cv.translate(shake, 0)
    N = 150
    for i in range(N, -1, -1):
        s = i / N
        is_head = s < 0.08
        a = head_alpha if is_head else body_alpha
        if a <= 0.01:
            continue
        x, y = serpent_spine(t, s, tight, head_off if is_head else (0, 0))
        if not is_head and after > 0:
            rng = random.Random(i)
            x += (rng.random() - 0.5) * 400 * after
            y += (rng.random() - 0.5) * 400 * after
        rad = 34 * (1 - s) ** 0.5 * (0.6 + 0.4 * math.sin(min(1, s * 12) * 1.57)) + 3
        cv.drawCircle(x, y, rad + 2, paint((0.20, 0.30, 0.22), 0.6 * a))
        cv.drawCircle(x, y, rad, paint((0.015, 0.02, 0.018), a))
        if i % 6 == 0:
            cv.drawCircle(x - rad * 0.3, y - rad * 0.3, rad * 0.25, paint((0.35, 0.45, 0.38), 0.25 * a))
    hx, hy = serpent_spine(t, 0.0, tight, head_off)
    cv.drawCircle(hx, hy, 5, paint(CRIMSON, head_alpha))
    cv.restore()
    # 轮回中重来的黑蛇（衔尾之环）
    if relapse > 0:
        ring = skia.Path()
        ring.addCircle(CX, CY - 40, 420)
        cv.drawPath(ring, paint((0.02, 0.025, 0.02), 0.85 * relapse, stroke=46))
        cv.drawPath(ring, paint((0.25, 0.35, 0.28), 0.5 * relapse, stroke=2))
        for i, w in enumerate(["渺小", "卑劣", "令人厌恶"]):
            ang = -1.9 + i * 1.9 + t * 0.1
            text(cv, w, CX + 560 * math.cos(ang), CY - 30 + 380 * math.sin(ang), 40, (0.6, 0.7, 0.62),
                 relapse * ramp(t, c.L(5) + 3 + i * 0.8, c.L(5) + 4 + i * 0.8) * 0.8)
    # “咬！”
    bite_word = window(t, c.L(2) + 0.6, c.L(3) + 0.4, 0.25)
    if bite_word > 0:
        cv.drawRect(skia.Rect(0, 0, W, H), paint(CRIMSON, 0.12 * bite_word))
        text(cv, "咬！", CX, CY + 20, 220, (1.0, 0.9, 0.85), bite_word, TF_SERIF_B, glow=26)
    flash = math.exp(-max(0, after) * 5) if after > 0 else 0
    if flash > 0.01:
        cv.drawRect(skia.Rect(0, 0, W, H), paint(PALE, 0.8 * flash))
    laugh = window(t, c.Lend(4) - 1.8, c.L(5) + 0.6, 0.8)
    text(cv, "他笑了", CX, H - 250, 60, PALE, laugh, TF_SERIF_B, glow=14, spacing=12)
    dust(cv, t, 0.4 * bright)
    vignette(cv)


def scene_amor(cv, c):
    t = c.t
    resign = window(t, c.L(3), c.L(4) + 0.3, 0.8)
    fire = ramp(t, c.L(4), c.L(4) + 1.5)
    gold = clamp(1 - resign)
    background(cv, mix((0.03, 0.03, 0.04), (0.10, 0.07, 0.035), gold * 0.8), INK)
    R = 300
    spin = t * 9 * (1 - 0.95 * resign) + 40 * fire * t
    col = mix((0.45, 0.45, 0.47), GOLD, gold)
    glow_dot(cv, CX, CY - 30, 600, col, 0.18 * gold + 0.25 * fire)
    cv.save()
    cv.translate(CX, CY - 30)
    cv.rotate(spin)
    cv.drawCircle(0, 0, R, paint(col, 0.35 * gold, stroke=14, blur=14))
    cv.drawCircle(0, 0, R, paint(col, 0.9, stroke=3))
    cv.drawCircle(0, 0, R - 26, paint(col, 0.35, stroke=1))
    for i in range(120):
        ang = i / 120 * 6.2832
        l = 18 if i % 10 == 0 else 7
        cv.drawLine((R - 26) * math.cos(ang), (R - 26) * math.sin(ang),
                    (R - 26 - l) * math.cos(ang), (R - 26 - l) * math.sin(ang), paint(col, 0.6, stroke=1.4))
    # 衔尾：环上一个首尾相接的结
    cv.drawCircle(R, 0, 16, paint(col, 1))
    cv.drawCircle(R, 0, 26, paint(col, 0.5, stroke=2))
    cv.restore()
    a = ramp(t, c.L(0) + 0.5, c.L(0) + 2.0)
    text(cv, "Amor fati", CX, CY + 10, 130, mix(PALE, (0.7, 0.7, 0.72), resign), a, TF_ITALIC, glow=14 * gold)
    text(cv, "热 爱 命 运", CX, CY + 90, 44, col, a * 0.85)
    for i, w in enumerate(["不向前", "不向后", "永远不"]):
        text(cv, w, CX + (i - 1) * 300, CY + 380, 38, PALE,
             window(t, c.L(1) + 4.8 + i * 0.8, c.L(2) + 1.0, 0.6) * 0.75)
    text(cv, "认命", CX - 560, CY - 10, 70, (0.6, 0.6, 0.62), resign * 0.75, TF_SERIF_B)
    text(cv, "再来一次", CX, CY + 400, 72, PALE, ramp(t, c.L(4) + 2.6, c.L(4) + 3.6), TF_SERIF_B, glow=18,
         spacing=14)
    if fire > 0:
        rng = random.Random(9)
        for i in range(80):
            ang = rng.random() * 6.2832 + t * 0.2
            dist = R + ((t * rng.uniform(40, 120) + rng.random() * 400) % 420)
            cv.drawCircle(CX + dist * math.cos(ang), CY - 30 + dist * math.sin(ang), 2.2,
                          paint(GOLD, fire * (1 - (dist - R) / 420)))
    dust(cv, t, 0.3 * gold)
    vignette(cv)


def build_web():
    rng = random.Random(1888)
    nodes = []
    while len(nodes) < 58:
        x, y = rng.uniform(180, W - 180), rng.uniform(130, H - 230)
        if all((x - a) ** 2 + (y - b) ** 2 > 130 ** 2 for a, b, _ in nodes):
            nodes.append((x, y, rng.random() < 0.45))
    edges = set()
    for i, (x, y, _) in enumerate(nodes):
        near = sorted(range(len(nodes)), key=lambda j: (nodes[j][0] - x) ** 2 + (nodes[j][1] - y) ** 2)[1:4]
        for j in near:
            edges.add((min(i, j), max(i, j)))
    # 从最靠近中心的节点出发的图距离
    start = min(range(len(nodes)), key=lambda i: (nodes[i][0] - CX) ** 2 + (nodes[i][1] - CY + 60) ** 2)
    adj = {i: [] for i in range(len(nodes))}
    for i, j in edges:
        adj[i].append(j)
        adj[j].append(i)
    dist = {start: 0}
    queue = [start]
    while queue:
        i = queue.pop(0)
        for j in adj[i]:
            if j not in dist:
                dist[j] = dist[i] + 1
                queue.append(j)
    return nodes, sorted(edges), dist, start


WEB = build_web()


def scene_web(cv, c):
    t = c.t
    nodes, edges, dist, start = WEB
    background(cv, (0.03, 0.03, 0.05), INK)
    appear = ramp(t, 0.2, 2.5)
    chain = ramp(t, c.L(3), c.L(3) + 1.5)
    wave_t = c.L(4) + 2.8

    def lit(i):
        return ramp(t, wave_t + dist.get(i, 9) * 0.35, wave_t + dist.get(i, 9) * 0.35 + 0.5)

    # 试图只挑出一个美好的节点，却牵动了整张网
    pick = [i for i, n in enumerate(nodes) if not n[2]][3]
    pull = math.sin(math.pi * clamp((t - c.L(2) - 2.0) / 3.0)) if c.L(2) + 2.0 < t < c.L(2) + 5.0 else 0.0

    def pos(i):
        x, y, _ = nodes[i]
        d = math.hypot(x - nodes[pick][0], y - nodes[pick][1])
        k = 1.0 if i == pick else math.exp(-d / 260) * 0.8
        return x + 260 * pull * k, y - 160 * pull * k

    for i, j in edges:
        (x1, y1), (x2, y2) = pos(i), pos(j)
        la = (lit(i) + lit(j)) / 2
        col = mix(mix((0.35, 0.35, 0.4), GOLD, chain), GOLD, la)
        cv.drawLine(x1, y1, x2, y2, paint(col, appear * (0.22 + 0.35 * chain + 0.3 * la), stroke=1.2 + 1.5 * la))
        if chain > 0:
            u = (t * 0.6 + (i * 7 + j * 3) * 0.13) % 1.0
            cv.drawCircle(lerp(x1, x2, u), lerp(y1, y2, u), 2.5, paint(GOLD, 0.7 * chain))
    for i, (x, y, dark) in enumerate(nodes):
        x, y = pos(i)
        la = lit(i)
        base = (0.30, 0.13, 0.14) if dark else (0.92, 0.85, 0.7)
        col = mix(base, GOLD, la)
        glow_dot(cv, x, y, 36 + 30 * la, col, appear * (0.35 + 0.5 * la))
        cv.drawCircle(x, y, 7, paint(col, appear))
    labels = ["失去", "背叛", "病痛", "悔恨"]
    darks = [i for i, n in enumerate(nodes) if n[2]]
    for k, w in enumerate(labels):
        i = darks[k * 3 + 1]
        x, y = pos(i)
        a = ramp(t, c.L(0) + 3.0 + k * 0.6, c.L(0) + 3.6 + k * 0.6)
        text(cv, w, x, y - 26, 34, mix((0.85, 0.55, 0.52), GOLD, lit(i)), a * appear)
    # 尼采本人
    ni = window(t, c.L(1), c.L(2) + 0.5, 0.8)
    if ni > 0:
        cv.drawRect(skia.Rect(0, 0, W, H), paint(INK, 0.55 * ni))
        text(cv, "Friedrich Nietzsche", CX, CY - 40, 64, PALE, ni * 0.9, TF_ITALIC, glow=8)
        text(cv, "1844 — 1900", CX, CY + 30, 36, GOLD, ni * 0.8)
        for k, w in enumerate(["头痛", "呕吐", "近乎失明", "孤独", "无人问津"]):
            text(cv, w, CX + (k - 2) * 230, CY + 160, 32, (0.75, 0.6, 0.58),
                 ni * ramp(t, c.L(1) + 3.2 + k * 0.7, c.L(1) + 3.8 + k * 0.7) * 0.8)
    text(cv, "是", nodes[start][0], nodes[start][1] - 40, 72, PALE, ramp(t, wave_t - 0.4, wave_t + 0.4),
         TF_SERIF_B, glow=16)
    dust(cv, t, 0.3 * ramp(t, wave_t, wave_t + 2))
    vignette(cv)


def scene_days(cv, c):
    t = c.t
    # 一天越来越短：日子在重复
    period0, period1 = 9.0, 2.2
    # 累积相位：period 随时间线性缩短
    k = (period1 - period0) / c.dur
    phase = math.log((period0 + k * t) / period0) / k if abs(k) > 1e-6 else t / period0
    day, frac = int(phase), phase % 1.0
    sun_h = math.sin(frac * math.pi)  # 0（日出/日落）→1（正午）
    sky_top = mix((0.02, 0.03, 0.08), (0.18, 0.32, 0.55), sun_h)
    sky_bot = mix((0.08, 0.06, 0.10), (0.85, 0.62, 0.42), max(0.0, 1 - abs(sun_h - 0.25) * 2.2))
    background(cv, sky_top, sky_bot)
    stars(cv, t, clamp(1 - sun_h * 3) * 0.6)
    horizon = 760
    sx = lerp(200, W - 200, frac)
    sy = horizon - 520 * sun_h
    glow_dot(cv, sx, sy, 300, (1.0, 0.8, 0.5), 0.5)
    cv.drawCircle(sx, sy, 46, paint((1.0, 0.92, 0.75)))
    for pts, col in zip(MOUNTAINS[1:], [(0.06, 0.06, 0.09), (0.03, 0.035, 0.05)]):
        path = skia.Path()
        path.moveTo(-20, H)
        for x, y in pts:
            path.lineTo(x, y + 60)
        path.lineTo(W + 40, H)
        path.close()
        cv.drawPath(path, paint(mix(col, (0.2, 0.17, 0.2), sun_h * 0.4)))
    cv.drawRect(skia.Rect(0, horizon + 20, W, H), paint(INK, 0.8))
    text(cv, f"今天 × {day + 1}", W - 140, 120, 38, PALE, 0.75, align="right")
    # 轻如鸿毛：一片羽毛飘走
    fa = window(t, c.L(4), c.L(4) + 4.5, 0.8)
    if fa > 0:
        u = clamp((t - c.L(4)) / 4.5)
        fx, fy = lerp(560, 760, u) + 40 * math.sin(u * 9), lerp(640, 220, u)
        cv.save()
        cv.translate(fx, fy)
        cv.rotate(25 * math.sin(u * 7))
        fp = skia.Path()
        fp.moveTo(0, 70)
        fp.cubicTo(-32, 20, -24, -50, 0, -80)
        fp.cubicTo(24, -50, 32, 20, 0, 70)
        cv.drawPath(fp, paint(PALE, 0.7 * fa))
        cv.drawLine(0, 90, 0, -76, paint((0.6, 0.55, 0.5), fa, stroke=1.5))
        cv.restore()
        text(cv, "轻", 420, 520, 120, PALE, fa * 0.5, TF_SERIF_B)
    # 刻进永恒
    ea = ramp(t, c.Lend(4) - 3.2, c.Lend(4) - 1.5)
    if ea > 0:
        words = "刻进永恒"
        for i, ch in enumerate(words):
            a = ramp(t, c.Lend(4) - 3.2 + i * 0.35, c.Lend(4) - 2.4 + i * 0.35)
            x = CX + 300 + (i - 1.5) * 130
            text(cv, ch, x + 3, 563, 120, INK, a * 0.8, TF_SERIF_B)
            text(cv, ch, x, 560, 120, PALE, a, TF_SERIF_B, glow=10)
    question = window(t, c.L(1) - 0.2, c.L(2) + 0.6, 0.6)
    if question > 0:
        cv.drawRect(skia.Rect(0, 0, W, H), paint(INK, 0.45 * question))
        text(cv, "如果今天将永远重复——", CX, CY - 40, 72, PALE, question, TF_SERIF_B, glow=12)
        text(cv, "我愿意这样度过它吗？", CX, CY + 60, 72, GOLD, question, TF_SERIF_B, glow=12)
    vignette(cv, 0.55)


def scene_ending(cv, c):
    t = c.t
    draw_lake(cv, t + 50, rock_glow=0.6 + 0.4 * math.sin(t * 0.8) * 0.5, zoom=1.06 - 0.04 * t / c.dur)
    q = ramp(t, c.L(2) - 0.3, c.L(2) + 1.2)
    if q > 0:
        cv.drawRect(skia.Rect(0, 0, W, H), paint(INK, 0.6 * q))
        text(cv, "这一生，", CX, CY - 50, 84, PALE, q, TF_SERIF_B, glow=14)
        text(cv, "你愿意再来一次吗？", CX, CY + 70, 84, GOLD, ramp(t, c.L(2) + 0.8, c.L(2) + 2.0),
             TF_SERIF_B, glow=16, spacing=6)
    dust(cv, t, 0.3)
    vignette(cv, 0.6)


def scene_credits(cv, c):
    t = c.t
    background(cv, INK, INK)
    dust(cv, t, 0.3)
    a = window(t, 0.4, c.dur - 0.2, 1.2)
    text(cv, "Werde, der du bist.", CX, CY - 120, 72, PALE, a, TF_ITALIC, glow=10)
    text(cv, "成为你自己。", CX, CY - 30, 50, GOLD, a * ramp(t, 1.2, 2.4), TF_SERIF_B, spacing=10)
    small = a * ramp(t, 2.6, 3.8) * 0.6
    refs = ["参考  尼采《快乐的科学》§341 ·《查拉图斯特拉如是说》·《瞧，这个人》",
            "米兰·昆德拉《不能承受的生命之轻》",
            "旁白：离线语音合成 · 画面：程序生成"]
    for i, r in enumerate(refs):
        text(cv, r, CX, CY + 150 + i * 52, 28, PALE, small)


SCENE_FUNCS = {
    "title": scene_title, "lake": scene_lake, "hourglass": scene_hourglass, "split": scene_split,
    "orbits": scene_orbits, "line_circle": scene_line_circle, "gateway": scene_gateway,
    "serpent": scene_serpent, "amor": scene_amor, "web": scene_web, "days": scene_days,
    "ending": scene_ending, "credits": scene_credits,
}


# ================================================================ 公共叠层
def wrap(s, n=26):
    if len(s) <= n:
        return [s]
    # 尽量在标点处断成两行
    best, mid = None, len(s) / 2
    for i, ch in enumerate(s):
        if ch in "，。：；？！、" and 6 < i < len(s) - 4:
            if best is None or abs(i + 1 - mid) < abs(best - mid):
                best = i + 1
    best = best or int(mid)
    return [s[:best], s[best:]]


def overlays(cv, scene, t):
    # 章节标题
    if scene["chapter"]:
        a = ramp(t, 0.6, 1.8) * (1 - ramp(t, scene["dur"] - 1.2, scene["dur"] - 0.4))
        text(cv, scene["chapter"], 90, 96, 30, GOLD, a * 0.8, align="left")
        cv.drawLine(90, 116, 90 + 60, 116, paint(GOLD, a * 0.6, stroke=1.2))
    # 字幕
    for ln in scene["lines"]:
        if scene["key"] == "ending" and ln is scene["lines"][-1]:
            continue
        s, e = ln["start"] - 0.15, ln["start"] + ln["dur"] + 0.35
        if s <= t <= e:
            a = ramp(t, s, s + 0.25) * (1 - ramp(t, e - 0.25, e))
            rows = wrap(ln["text"])
            base = H - 84 - (len(rows) - 1) * 58
            for k, row in enumerate(rows):
                y = base + k * 58
                text(cv, row, CX + 2, y + 2, 40, INK, a * 0.9, TF_SERIF, glow=6)
                text(cv, row, CX, y, 40, PALE, a, TF_SERIF)
    # 场景之间淡入淡出
    fade = min(ramp(t, 0, 0.8), 1 - ramp(t, scene["dur"] - 0.8, scene["dur"]))
    if fade < 1:
        cv.drawRect(skia.Rect(0, 0, W, H), paint(INK, 1 - fade))


# ================================================================ 输出
def render_frame(scene, t, buf=None):
    buf = buf if buf is not None else np.zeros((H, W, 4), np.uint8)
    surface = skia.Surface(buf)
    cv = surface.getCanvas()
    cv.clear(c4(INK))
    SCENE_FUNCS[scene["key"]](cv, Ctx(scene, t))
    overlays(cv, scene, t)
    return buf


def render_scene(args):
    idx, scene, f0, f1 = args
    path = os.path.join(BUILD, f"seg_{idx:02d}.mp4")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", "19",
           "-pix_fmt", "yuv420p", "-tune", "film", path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    buf = np.zeros((H, W, 4), np.uint8)
    for f in range(f0, f1):
        t = f / FPS - scene["start"]
        render_frame(scene, t, buf)
        proc.stdin.write(buf.tobytes())
    proc.stdin.close()
    proc.wait()
    print(f"  scene {idx:02d} {scene['key']:<12} {f1 - f0} frames", flush=True)
    return path


def make_music(total, sr, scenes):
    """缓慢起伏的持续音 + 每个场景开头一声轻钟。"""
    n = int(total * sr) + sr
    tt = np.arange(n) / sr
    out = np.zeros(n, np.float32)
    # A 小调的持续和弦（A2 E3 A3 C4 E4），各声部缓慢呼吸
    for i, (f, amp) in enumerate([(110.0, 0.30), (164.81, 0.20), (220.0, 0.16), (261.63, 0.10), (329.63, 0.07)]):
        lfo = 0.6 + 0.4 * np.sin(2 * np.pi * tt / (17 + 6 * i) + i)
        out += amp * lfo * np.sin(2 * np.pi * f * tt + 0.3 * np.sin(2 * np.pi * 0.11 * tt + i))
    # 轻钟
    for sc in scenes:
        i0 = int(sc["start"] * sr)
        seg = np.arange(int(6 * sr)) / sr
        bell = sum(a * np.sin(2 * np.pi * f * seg) * np.exp(-seg * d)
                   for f, a, d in [(440, 0.5, 0.9), (1108.7, 0.2, 1.6), (1760, 0.1, 2.5)])
        j = min(n, i0 + len(seg))
        out[i0:j] += 0.5 * bell[: j - i0]
    fade = np.minimum(1, np.minimum(tt / 4, (total - tt).clip(0) / 6))
    out *= fade
    out /= np.max(np.abs(out)) or 1
    return out


def main():
    with open(os.path.join(BUILD, "timeline.json")) as f:
        tl = json.load(f)
    scenes = tl["scenes"]
    if len(sys.argv) >= 4 and sys.argv[1] == "--still":
        key, sec = sys.argv[2], float(sys.argv[3])
        scene = next(s for s in scenes if s["key"] == key)
        buf = render_frame(scene, sec)
        out = os.path.join(BUILD, f"still_{key}_{sec:.1f}.png")
        skia.Image.fromarray(buf).save(out, skia.kPNG)
        print(out)
        return

    import soundfile as sf
    os.makedirs(OUT, exist_ok=True)
    jobs = []
    for i, s in enumerate(scenes):
        f0, f1 = round(s["start"] * FPS), round((s["start"] + s["dur"]) * FPS)
        jobs.append((i, s, f0, f1))
    print(f"rendering {len(jobs)} scenes, {tl['total']:.1f}s", flush=True)
    order = sorted(jobs, key=lambda j: -(j[3] - j[2]))  # 长场景先开工
    with Pool(os.cpu_count()) as pool:
        pool.map(render_scene, order, chunksize=1)
    with open(os.path.join(BUILD, "segments.txt"), "w") as f:
        for i, *_ in jobs:
            f.write(f"file 'seg_{i:02d}.mp4'\n")

    voice, sr = sf.read(os.path.join(BUILD, "voice.wav"), dtype="float32")
    music = make_music(tl["total"], sr, scenes)[: len(voice)]
    music = np.pad(music, (0, len(voice) - len(music)))
    mixed = voice + 0.075 * music
    mixed /= max(1.0, np.max(np.abs(mixed)) / 0.95)
    sf.write(os.path.join(BUILD, "mix.wav"), mixed, sr)

    out = os.path.join(OUT, "eternal_recurrence.mp4")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", os.path.join(BUILD, "segments.txt"), "-i", os.path.join(BUILD, "mix.wav"),
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", out],
                   check=True)
    print(out)


if __name__ == "__main__":
    main()
