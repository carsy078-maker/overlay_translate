"""text_filter 순수 함수 테스트. PyQt6/pywinauto 없이 어떤 OS에서도 돈다."""

import pytest

from text_filter import (
    classify_run,
    group_runs_into_lines,
    is_visible_rect,
    looks_like_korean,
    select_body_runs,
    strip_ui_noise,
)


# ── classify_run ──────────────────────────────────────
@pytest.mark.parametrize("text, expected", [
    ("Hello world", "content"),
    ("!smallping", "content"),
    ("🤖bots", "content"),          # 이모지+영문 → 본문
    ("", "empty"),
    ("   ", "empty"),
    ("수정됨", "edited"),
    ("(edited)", "edited"),
    ("189", "count"),               # 반응 개수
    ("(", "punct"),
    (" in ", "content"),            # 'in' 이 있으므로 본문(줄 병합 시 필요)
    ("...", "punct"),
    ("😀", "punct"),                # 이모지만 → 무의미
    ("오전 5:33", "timestamp"),
    ("2026-04-14 오전 7:21", "timestamp"),
    ("2026년 4월 14일 화요일 오전 5:33", "timestamp"),
    ("3달 전", "timestamp"),
    ("12:34", "timestamp"),
    ("추가하기", "ui"),              # 액션 툴바 버튼
    ("답장", "ui"),
    ("전달", "ui"),
    ("기타", "ui"),
    ("Reply", "ui"),
    (":poop:", "punct"),            # 리액션 이모지 코드
    (":poop::thumbsup::heart:", "punct"),
])
def test_classify_run(text, expected):
    assert classify_run(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("Adjudicator (EN/SP) 서버 태그: DF 오후 6:08", "timestamp"),  # 한 덩어리 헤더
    ("Look-Ass 서버 태그: VS 오후 6:21", "timestamp"),
    ("Navael 오후 8:19", "timestamp"),
])
def test_classify_merged_header(text, expected):
    assert classify_run(text) == expected


@pytest.mark.parametrize("raw, expected", [
    ("I coach pro teams클릭해서 반응클릭해서 반응클릭해서 반응", "I coach pro teams"),
    ("you can dm also if you want클릭해서 반응", "you can dm also if you want"),
    ("hello Click to reactClick to react", "hello"),
    ("no noise here", "no noise here"),
])
def test_strip_ui_noise(raw, expected):
    assert strip_ui_noise(raw) == expected


def test_body_drops_merged_header_and_reaction_noise():
    # 이 버전 디스코드의 실제 구조: 헤더 한 덩어리 + 본문에 리액션 라벨 붙음
    runs = [
        ("Adjudicator (EN/SP) 서버 태그: DF 오후 6:10", (0, 0, 300, 22)),  # 헤더(유저명+시간)
        ("All good, its my job클릭해서 반응클릭해서 반응", (0, 24, 250, 46)),
    ]
    assert _texts(select_body_runs(runs)) == ["All good, its my job"]


def test_body_drops_ui_and_reactions():
    # 리액션 달린 메시지: 본문 뒤에 액션버튼/이모지코드가 붙어도 본문만 남아야 한다.
    runs = [
        ("오전 6:10", (0, 0, 50, 18)),
        ("All good, its my job", (0, 20, 200, 38)),
        ("추가하기", (0, 40, 40, 58)),
        ("답장", (45, 40, 70, 58)),
        ("전달", (75, 40, 100, 58)),
        ("기타", (105, 40, 130, 58)),
        (":poop::thumbsup::heart:", (135, 40, 260, 58)),
    ]
    assert _texts(select_body_runs(runs)) == ["All good, its my job"]


def test_classify_username_is_content():
    # 유저명은 content로 분류되고, 본체에서 '타임스탬프 이전' 위치로 걸러진다.
    assert classify_run("Main Dev") == "content"


# ── looks_like_korean ─────────────────────────────────
@pytest.mark.parametrize("text, expected", [
    ("안녕하세요 반갑습니다", True),
    ("Hello world", False),
    ("おはよう", False),             # 일본어는 한국어 아님
    ("", False),
    ("123 !!!", False),             # 글자 없음
    ("반가워요 hi", True),          # 한글 4 / 전체 6글자 = 66% > 60%
    ("네 ok", False),               # 한글 1 / 전체 3글자 = 33% → 한국어로 안 봄
])
def test_looks_like_korean(text, expected):
    assert looks_like_korean(text) is expected


# ── is_visible_rect ───────────────────────────────────
WIN = (0, 0, 1000, 1000)


def test_visible_normal():
    assert is_visible_rect((10, 10, 100, 30), WIN) is True


def test_visible_zero_area():
    assert is_visible_rect((0, 0, 0, 0), WIN) is False
    assert is_visible_rect((50, 50, 50, 80), WIN) is False   # 폭 0


def test_visible_offscreen():
    assert is_visible_rect((2000, 10, 2100, 30), WIN) is False   # 창 오른쪽 밖
    assert is_visible_rect((10, -50, 100, -10), WIN) is False    # 창 위쪽 밖


def test_visible_negative_coords_monitor():
    # 보조 모니터(음수 좌표)에서도 창 영역과 겹치면 보인다.
    win = (-1928, 512, 8, 1560)
    assert is_visible_rect((-1000, 600, -800, 620), win) is True


# ── group_runs_into_lines ─────────────────────────────
def test_group_same_line_merges_in_left_order():
    runs = [
        ("!smallping", (100, 600, 200, 619)),
        ("type ", (0, 600, 100, 619)),          # 더 왼쪽 → 앞에 와야
        (" in bots", (200, 602, 300, 619)),     # top 2px 차이 → 같은 줄
    ]
    segs = group_runs_into_lines(runs)
    assert len(segs) == 1
    text, rect = segs[0]
    assert text == "type !smallping in bots"
    assert rect == (0, 600, 300, 619)           # rect 합집합


def test_group_different_lines_split():
    runs = [
        ("first line", (0, 600, 100, 619)),
        ("second line", (0, 700, 120, 728)),    # top 100px 차이 → 다른 줄
    ]
    segs = group_runs_into_lines(runs)
    assert [s[0] for s in segs] == ["first line", "second line"]


def test_group_empty():
    assert group_runs_into_lines([]) == []


def test_group_does_not_mutate_input():
    runs = [("b", (10, 0, 20, 10)), ("a", (0, 0, 10, 10))]
    original = list(runs)
    group_runs_into_lines(runs)
    assert runs == original                      # 입력 리스트를 건드리지 않는다


def test_group_threshold_boundary():
    # 기본 임계값 14px: 14는 같은 줄, 15는 다른 줄
    same = group_runs_into_lines([("a", (0, 0, 10, 10)), ("b", (20, 14, 30, 24))])
    assert len(same) == 1
    diff = group_runs_into_lines([("a", (0, 0, 10, 10)), ("b", (20, 15, 30, 25))])
    assert len(diff) == 2


# ── select_body_runs (핵심 회귀 방지) ─────────────────
def _texts(runs):
    return [t for t, _ in runs]


def test_body_header_message_drops_username():
    # 헤더 메시지: [유저명][타임스탬프][본문...] → 유저명 버리고 본문만
    runs = [
        ("Adjudicator", (0, 0, 80, 18)),        # 유저명 (타임스탬프 앞)
        ("오전 6:08", (90, 0, 140, 18)),         # 타임스탬프
        ("no problem c:", (0, 20, 120, 38)),     # 본문
    ]
    assert _texts(select_body_runs(runs)) == ["no problem c:"]


def test_body_grouped_message_keeps_content():
    # 묶인 메시지: 헤더(유저명/타임스탬프)가 아예 없음 → 본문을 버리면 안 된다.
    # 이게 회귀했던 버그: seen_ts False라 전부 유저명으로 오인해 0개가 됐었다.
    runs = [
        ("u on global ", (0, 0, 90, 18)),
        ("or garena ?", (92, 0, 170, 18)),
    ]
    assert _texts(select_body_runs(runs)) == ["u on global ", "or garena ?"]


def test_body_drops_noise():
    # 타임스탬프/편집표시/반응숫자/문장부호는 본문에서 제외
    runs = [
        ("오전 6:08", (0, 0, 50, 18)),
        ("hello world", (0, 20, 100, 38)),
        ("(", (0, 40, 5, 58)),
        ("수정됨", (10, 40, 40, 58)),
        (")", (45, 40, 50, 58)),
        ("194", (0, 60, 30, 78)),
    ]
    assert _texts(select_body_runs(runs)) == ["hello world"]


def test_body_empty():
    assert select_body_runs([]) == []
    # 헤더만 있고 본문이 없는 경우(유저명+타임스탬프뿐) → 빈 결과
    assert select_body_runs([("User", (0, 0, 5, 5)), ("오전 6:08", (9, 0, 5, 5))]) == []


def test_body_preserves_rect():
    runs = [("오전 6:08", (0, 0, 50, 18)), ("hi there", (0, 20, 70, 38))]
    assert select_body_runs(runs) == [("hi there", (0, 20, 70, 38))]
