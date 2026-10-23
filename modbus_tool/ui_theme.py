"""界面配色与字体。

集中在一处，是因为颜色和字体本身有一套设计语言（DeepSeek 客户端的浅色系）：
品牌蓝 #5686FE、neutral-bluish 蓝灰阶、分层靠底色不靠描边。散落在 gui.py 里
改一个色要翻三个文件，也说不清哪些是「主题」哪些是「控件自己的事」。
"""

from __future__ import annotations

# ---- 配色（浅色主题 · DeepSeek Harness 设计语言）----
# 色值取自 DeepSeek 官方客户端：极简、无边框轻拟态 —— 分层靠底色不靠描边，
# 品牌蓝 #5686FE（悬停加深为 #3964FE），中性色走 neutral-bluish 蓝灰阶。
BG = "#f5f6f7"             # neutral-bluish-60，平台底座
CARD = "#ffffff"           # layer-1，浮起的内容层
BORDER = "#e4e6e9"         # border-l2 rgba(0,0,0,.10) 的实色近似，淡到若有若无
TEXT = "#0f1115"           # neutral-bluish-1000
MUTED = "#61666b"          # neutral-bluish-700
ACCENT = "#5686fe"         # deepseek-450，品牌蓝
ACCENT_PRESS = "#3964fe"   # deepseek-500，悬停/按下加深
OK = "#16a34a"
WARN = "#dd8629"           # amber-600
ERR = "#ec1313"            # red-600
MONO = ("Consolas", 9)
# 日志里有中文，不能直接套 Consolas —— 它没有中文字形，fallback 出来的字比
# Consolas 的行高更高，行与行会压在一起。正文用中文界面字体，只把时间戳留给等宽。
LOG_FONT = ("Microsoft YaHei UI", 9)
UI_FONT = ("Microsoft YaHei UI", 9)

# filled 风格输入框：浅底色块代替描边，聚焦时转白底 + 品牌蓝细边
FIELD_BG = "#f0f1f3"
HEAD_BG = "#f9fafb"        # 表头/次级面（neutral-bluish-50）
HOVER_BG = "#f1f3f5"       # 悬停底色（neutral-bluish-75）
SELECT_BG = "#e4edfd"      # 选中行（deepseek-100）
DISABLED_BG = "#eef0f3"
DISABLED_FG = "#adb2b8"    # neutral-bluish-400
DISABLED_ARROW = "#c3cad6"

# ---- 分段控件用色 ----
TRACK = "#eef0f3"          # 分段控件的凹槽（官方 fill：白底上 rgba(38,49,72,.06)）
PILL_EDGE = "#e7e9ed"      # 药丸描边 —— 轻拟态，描边淡到只够把药丸从凹槽里托出来
PILL_SHADOW = "#eceef1"    # 药丸下方的 1px 投影，让它浮起来一点
TAB_HOVER = "#43454a"      # 未选中项悬停时的文字色（neutral-bluish-750）
TAB_IDLE = "#61666b"       # 未选中项常态文字色（neutral-bluish-700）
