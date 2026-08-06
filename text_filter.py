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
# 어떤 디스코드 버전은 [유저명][태그][시간]을 한 덩어리 Text로 렌더한다.
# ("Adjudicator (EN/SP) 서버 태그: DF 오후 6:08") → 시간으로 끝나면 헤더로 본다.
HEADER_END_TIME = re.compile(r"(오전|오후)\s*\d{1,2}:\d{2}\s*$")

EDITED = {"수정됨", "(edited)", "edited"}

# 메시지 본문에 섞여 들어오는 리액션 버튼 라벨(같은 Text 안에 붙어 나온다).
# "I coach pro teams클릭해서 반응클릭해서 반응..." 처럼 본문 끝에 반복해 달라붙는다.
# 라벨 바로 앞뒤 공백까지 함께 지운다(단어끼리 붙는 것 방지). 단, 조각 내부의
# 정상 공백은 건드리지 않으므로 조각 이어붙이기(공백 보존)에는 영향이 없다.
# "React" 단독은 일반 단어(프레임워크 등) 오탐 위험이라 넣지 않는다.
REACTION_NOISE = re.compile(r"\s*(클릭해서\s*반응|Click to react)+\s*", re.IGNORECASE)


def strip_ui_noise(text: str) -> str:
    """본문 Text에 섞인 리액션 버튼 라벨을 제거한다(조각 내부 공백은 보존)."""
    return REACTION_NOISE.sub("", text)

# 메시지 위에 뜨는 액션 툴바 버튼 라벨(리액션 달린 메시지에서 본문에 딸려 들어온다).
# 한국어 UI + 영어 UI 둘 다.
UI_ACTIONS = {
    "추가하기", "반응 추가하기", "답장", "전달", "전달하기", "기타", "더 보기", "더보기",
    "Add Reaction", "React", "Reply", "Forward", "More", "More Actions",
}
# 리액션 이모지 접근성 이름(":poop::thumbsup::heart:" 처럼 콜론으로 감싼 코드들).
EMOJI_SHORTCODE = re.compile(r"^(:[^:\s]+:)+$")


def classify_run(text: str) -> str:
    """메시지 안의 Text 조각 하나를 분류한다.

    반환: empty / edited / count / timestamp / ui / punct / content
    (content 만 번역 대상 본문으로 취급한다)
    """
    s = text.strip()
    if not s:
        return "empty"
    if s in EDITED:
        return "edited"
    if s in UI_ACTIONS:                    # 액션 툴바 버튼(추가하기/답장/전달/기타 …)
        return "ui"
    if s.isdigit():                       # 반응 개수 등
        return "count"
    if EMOJI_SHORTCODE.match(s):           # :poop::thumbsup: 같은 리액션 코드
        return "punct"
    for p in TS_PATTERNS:
        if p.match(s):
            return "timestamp"
    if HEADER_END_TIME.search(s):          # [유저명 … 오후 6:08] 한 덩어리 헤더
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


def select_body_runs(runs):
    """트리 순서의 Text 조각들에서 '본문'만 골라 (text, rect)로 반환한다.

    runs: [(text, rect), ...]  (rect는 None일 수 있음 — 좌표 가시성 필터는 호출측 몫)

    유저명 처리가 핵심. 디스코드는 같은 사람이 연속으로 쓴 메시지를 '묶어서'
    첫 메시지에만 [유저명][타임스탬프] 헤더를 붙이고, 묶인 나머지는 헤더가 없다.
    따라서 '타임스탬프 뒤 = 본문'으로 단정하면 헤더 없는 묶인 메시지(대화의 대부분)의
    본문을 통째로 유저명으로 오인해 버린다.

      - 타임스탬프가 있으면(헤더 메시지): 앞쪽(=유저명)은 버리고 뒤쪽만 본문.
      - 타임스탬프가 없으면(묶인 메시지): 앞쪽이 곧 본문이므로 그대로 사용.
    """
    pre, post = [], []
    seen_ts = False
    for text, rect in runs:
        kind = classify_run(text)
        if kind == "timestamp":
            seen_ts = True
            continue
        if kind in ("empty", "edited", "count", "punct", "ui"):
            continue
        cleaned = strip_ui_noise(text)     # 본문에 붙은 '클릭해서 반응' 등 제거
        if not cleaned:
            continue
        (post if seen_ts else pre).append((cleaned, rect))
    return post if seen_ts else pre


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
