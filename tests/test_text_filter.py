"""text_filter 순수 함수 테스트. PyQt6/pywinauto 없이 어떤 OS에서도 돈다."""

import pytest

from text_filter import (
    classify_run,
    group_runs_into_lines,
    is_visible_rect,
    looks_like_korean,
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
])
def test_classify_run(text, expected):
    assert classify_run(text) == expected


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
