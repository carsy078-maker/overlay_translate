"""
순수 텍스트/기하 유틸 — UIA·Qt·네트워크 의존성이 전혀 없다.

본체(discord_screen_overlay.py)에서 분리한 이유:
  - 이 부분이 로직의 핵심(무엇을 본문으로 볼지, 조각을 어떻게 줄로 묶을지)이라
    단위 테스트로 굳혀두는 게 값지다.
  - PyQt6/pywinauto 없이 import되므로 어떤 OS의 CI에서도 그대로 테스트된다.
"""

import re

# 같은 시각적 줄로 묶을 때 허용하는 세로 오차(px). 같은 줄 조각은 top이 거의
# 같고(<5px), 다른 줄은 줄높이(~18px 이상)만큼 벌어진다.
LINE_Y_THRESHOLD = 14

# 본문이 아닌 Text 조각을 걸러내는 규칙. 타임스탬프(절대/상대), 편집 표시 등.
TS_PATTERNS = [
    re.compile(r"^\d{4}-\d{2}-\d{2}"),          # 2026-04-14 오전 7:21
    re.compile(r"^\d{4}년"),                     # 2026년 4월 14일 ...
    re.compile(r"^(오전|오후)\s*\d"),            # 오전 5:33
    re.compile(r"^\d{1,2}:\d{2}"),               # 12:34
    re.compile(r"^\d+\s*(초|분|시간|일|주|개월|달|년)\s*전$"),  # 3달 전
    re.compile(r"^(어제|오늘)"),
]
EDITED = {"수정됨", "(edited)", "edited"}


def classify_run(text: str) -> str:
    """메시지 안의 Text 조각 하나를 분류한다.

    반환: empty / edited / count / timestamp / punct / content
    (content 만 번역 대상 본문으로 취급한다)
    """
    s = text.strip()
    if not s:
        return "empty"
    if s in EDITED:
        return "edited"
    if s.isdigit():                       # 반응 개수 등
        return "count"
    for p in TS_PATTERNS:
        if p.match(s):
            return "timestamp"
    if not any(ch.isalnum() for ch in s):  # 문장부호/이모지만 → 무의미
        return "punct"
    return "content"


def looks_like_korean(text: str) -> bool:
    """이미 한국어면 번역을 건너뛰기 위한 휴리스틱(글자 중 한글 비율 60% 초과)."""
    hangul = sum(1 for ch in text if "가" <= ch <= "힣")
    letters = sum(1 for ch in text if ch.isalpha())
    return letters > 0 and hangul / letters > 0.6


def is_visible_rect(rect, win_rect) -> bool:
    """rect가 넓이 0이 아니고 창 영역과 겹치는가(화면에 실제로 보이는가)."""
    l, t, r, b = rect
    if r <= l or b <= t:                     # 넓이 0 (렌더 안 됨)
        return False
    wl, wt, wr, wb = win_rect
    return not (r < wl or l > wr or b < wt or t > wb)


def group_runs_into_lines(runs_data, y_threshold: int = LINE_Y_THRESHOLD):
    """(text, rect) 조각들을 화면 세로 위치(같은 줄)로 묶어 세그먼트 리스트로.

    한 줄 안에서 멘션/코드/링크로 쪼개진 조각은 다시 이어붙여 문맥을 살린다.
    각 세그먼트는 자기 줄의 rect만 차지하므로 오버레이가 줄 단위로 정확히 얹힌다.
    반환: [(합쳐진 텍스트, (left, top, right, bottom)), ...]
    """
    runs_data = sorted(runs_data, key=lambda x: (x[1][1], x[1][0]))  # top, left 순
    groups: list[dict] = []
    for text, rect in runs_data:
        top = rect[1]
        if groups and abs(top - groups[-1]["top"]) <= y_threshold:
            groups[-1]["parts"].append((rect[0], text))
            groups[-1]["rects"].append(rect)
        else:
            groups.append({"top": top, "parts": [(rect[0], text)], "rects": [rect]})

    segments = []
    for g in groups:
        g["parts"].sort(key=lambda x: x[0])           # 왼쪽→오른쪽
        text = "".join(p[1] for p in g["parts"]).strip()
        rects = g["rects"]
        rect = (
            min(r[0] for r in rects),
            min(r[1] for r in rects),
            max(r[2] for r in rects),
            max(r[3] for r in rects),
        )
        segments.append((text, rect))
    return segments
