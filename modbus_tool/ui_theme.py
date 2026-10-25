"""界面配色、字体与间距栅格。

集中在一处，是因为颜色、字号、间距本身是一套设计语言（DeepSeek 客户端的
浅色系）：品牌蓝、neutral-bluish 蓝灰阶、分层靠「底色差 + 细边」而不是粗描边。
散落在 gui.py 里改一个色要翻三个文件，也说不清哪些是「主题」哪些是
「控件自己的事」。

两条硬约束（改动前请先读）：

1. **对比度**。相邻层的填充色差如果小于 1.05，屏幕上看不出来 —— 原来的
   #f5f6f7 / #ffffff / #f9fafb 就是这么塌掉的。现在每一层都留够余量，
   并且凡是「必须被认出来」的分层，都额外给一条 1px 描边。Tk 没有阴影，
   只靠填充差永远差一口气。
2. **文字达标 AA**。正文与表头对底色的对比度都 ≥ 4.5:1，状态色 ≥ 4.5:1。
   「看着淡」的根因是亮度差不够，不是字号小 —— 所以优先加深，而不是放大。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 配色（浅色主题 · DeepSeek 设计语言）
# ---------------------------------------------------------------------------

# ---- 分层底色 ----
# 四层：页面 < 下沉面 < 卡片 < 表头。相邻对比度分别 1.10 / 1.11 / 1.05，
# 表头额外靠 HEAD_EDGE 一条 1px 底线兜住，所以不需要更大的填充差。
BG = "#f1f3f7"             # 平台底座（页面底色）
CARD = "#ffffff"           # layer-1，浮起的内容层
SUNKEN = "#f2f5f9"         # 下沉面：工具条、状态栏、只读区块
HEAD_BG = "#f7f9fc"        # 表头 / 次级面
HEAD_EDGE = "#e2e7ee"      # 表头下缘 1px，把表头从数据区切出来

BORDER = "#cfd6e0"         # 常规描边（对 CARD 1.45:1，肉眼可辨）
BORDER_SOFT = "#e6eaf0"    # 分隔线，比描边更退一步

# ---- 文字 ----
TEXT = "#0f1115"           # 主文本
MUTED = "#5a6068"          # 次要文本（对 CARD 6.4:1）
FAINT = "#6e757e"          # 辅助小字（对 CARD 4.6:1，刚好达标，别再淡了）
HEAD_FG = "#5c6269"        # 表头字（对 HEAD_BG 6.1:1）

# ---- 品牌与状态 ----
ACCENT = "#3964fe"         # 品牌蓝，白字 4.6:1 —— 全站唯一的主蓝
ACCENT_PRESS = "#2b52e0"   # 悬停 / 按下
ACCENT_HOVER = "#4f75fe"   # 悬停（比常态亮一点，用于自绘控件）
ACCENT_SOFT = "#e8eefc"    # 选中底 / 焦点底

OK = "#15803d"             # 绿（对白 5.2:1）
WARN = "#b45309"           # 橙（对白 5.4:1）
ERR = "#dc2626"            # 红（对白 4.6:1）
INFO = "#1d6fd8"           # 蓝，用于中性提示

OK_SOFT = "#e7f5ec"
WARN_SOFT = "#fbefdd"
ERR_SOFT = "#fdeaea"
INFO_SOFT = "#e8f1fd"

# ---- 输入域 ----
# filled 风格：浅底色块代替描边，聚焦时转白底 + 品牌蓝细边
FIELD_BG = "#eef1f6"
FIELD_BG_FOCUS = "#ffffff"
HOVER_BG = "#e9edf3"
SELECT_BG = "#e8eefc"      # 选中行
DISABLED_BG = "#f0f2f5"
DISABLED_FG = "#949ba5"    # 对 DISABLED_BG 2.6:1，禁用态允许低对比
DISABLED_ARROW = "#b9c0ca"

# ---- 分段控件 ----
TRACK = "#e7ebf1"          # 凹槽（对 BG 1.09:1，能看出是个槽）
PILL_EDGE = "#d8dee7"      # 药丸描边
PILL_SHADOW = "#dfe4ec"    # 药丸下方 1px 投影
TAB_HOVER = "#3f4348"
TAB_IDLE = "#5a6068"

# ---- 日志着色（按角色分色）----
LOG_TS = "#7c838c"         # 时间戳
LOG_DIR_TX = "#1d6fd8"     # 发送
LOG_DIR_RX = "#15803d"     # 接收
LOG_FC = "#8a4b00"         # 功能码
LOG_PAYLOAD = "#1565c0"    # 有效载荷（正文强调）
LOG_CRC = "#8d939b"        # 校验码
LOG_ERR = "#c0281f"        # 异常帧

# ---------------------------------------------------------------------------
# 字体
#
# Tk 的字号单位是 point（9pt ≈ 12px @96dpi）。层级靠「字号一档 + 字重」共同
# 表达：正文 9、卡片标题 10 加粗、区块标题 11、品牌 14。不再单靠加粗区分。
# ---------------------------------------------------------------------------

UI_FONT = ("Microsoft YaHei UI", 9)          # 正文
FONT_SM = ("Microsoft YaHei UI", 8)          # 极小字：单位、下标
FONT_TITLE = ("Microsoft YaHei UI", 10, "bold")   # 卡片标题
FONT_SECTION = ("Microsoft YaHei UI", 11)    # 区块标题
FONT_DISPLAY = ("Microsoft YaHei UI", 12)    # 品牌

# 日志里有中文，不能直接套 Consolas —— 它没有中文字形，fallback 出来的字比
# Consolas 的行高更高，行与行会压在一起。正文用中文界面字体，只把时间戳留等宽。
LOG_FONT = ("Microsoft YaHei UI", 9)
MONO = ("Consolas", 10)                      # 数据：整数与 hex，最该易读

# ---------------------------------------------------------------------------
# 间距栅格（4px 基数）
#
# 原来内边距有 10 / 12 / 14 / 18 四套、行距有 2 / 3 / 4 三套，控件间距
# 4~14 随手取。现在只允许下面六档，任何 padding/pady 都必须从这里取。
# ---------------------------------------------------------------------------

SP_XS = 2
SP_S = 4
SP_M = 8
SP_L = 12
SP_XL = 16
SP_XXL = 20

PAD_CONTAINER = SP_L    # 容器内边距
PAD_CARD = SP_XL        # 卡片内边距
PAD_DIALOG = SP_XXL     # 弹层内边距
ROW_GAP = SP_M          # 行距

# 控件统一高度（见 ui_kit.CTRL_H，ttk 与自绘控件共用这一个数）
CTRL_H = 30
CTRL_H_SM = 26

# 圆角半径
R_SM = 4
R_MD = 6
R_LG = 8
R_PILL = 10


# ---------------------------------------------------------------------------
# 对比度自检（开发期用：python -m modbus_tool.ui_theme）
# ---------------------------------------------------------------------------

def _lum(value: str) -> float:
    h = value.lstrip("#")
    ch = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (f(c) for c in ch)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


if __name__ == "__main__":
    checks = [
        ("正文 / 卡片", TEXT, CARD, 4.5),
        ("次要字 / 卡片", MUTED, CARD, 4.5),
        ("辅助字 / 卡片", FAINT, CARD, 4.5),
        ("表头字 / 表头", HEAD_FG, HEAD_BG, 4.5),
        ("白字 / 主按钮", "#ffffff", ACCENT, 4.5),
        ("OK / 卡片", OK, CARD, 4.5),
        ("WARN / 卡片", WARN, CARD, 4.5),
        ("ERR / 卡片", ERR, CARD, 4.5),
        ("禁用字 / 禁用底", DISABLED_FG, DISABLED_BG, 2.5),
    ]
    print("文字对比度")
    for name, fg, bg, need in checks:
        ratio = contrast(fg, bg)
        print(f"  {name:18s} {ratio:5.2f}  需 {need}  {'OK' if ratio >= need else 'FAIL'}")

    print("分层对比度")
    for name, a, b in [
        ("页面 / 卡片", BG, CARD),
        ("下沉面 / 卡片", SUNKEN, CARD),
        ("表头 / 卡片", HEAD_BG, CARD),
        ("凹槽 / 页面", TRACK, BG),
        ("描边 / 卡片", BORDER, CARD),
        ("输入域 / 卡片", FIELD_BG, CARD),
    ]:
        print(f"  {name:18s} {contrast(a, b):5.2f}")
