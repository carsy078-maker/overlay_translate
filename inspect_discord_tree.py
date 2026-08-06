"""
Discord UIA 트리 실측 진단 & 벤치마크 스크립트

오버레이 번역기의 설계 결정("창 전체 스캔은 느리다 → 메시지 목록만 스캔한다")을
숫자로 증명하고 재현 가능하게 만드는 도구. 두 가지를 만든다.

  1. results/scan_benchmark.md  (커밋됨)
     - 전체 창 스캔 vs 메시지 목록만 스캔 시간 비교, 요소 개수, 길이 분포,
       메시지 구조(정제됨). 개인정보(서버명·유저명·메시지 본문)는 담지 않는다.

  2. discord_tree_dump.txt      (커밋 안 함, .gitignore)
     - 요소별 원본 상세(실제 텍스트 포함). 필터를 직접 다듬을 때 쓰는 개인 분석용.

사전 준비:
  1. 디스코드를 화면에 띄운다(최소화/트레이 상태면 좌표가 0이라 측정 불가).
  2. 접근성 트리가 비어 있으면(메시지 0개) Win+Ctrl+Enter 로 내레이터를 켠다.
  3. 활발한 대화가 보이는 채널을 열어둔다.

실행:
  python inspect_discord_tree.py
"""

import ctypes
import os
import platform
import re
import sys
import time
from datetime import datetime
from typing import Optional

from pywinauto import Desktop

DISCORD_TITLE_RE = ".*Discord.*"
DISCORD_WIN_CLASS = "Chrome_WidgetWin_1"
MESSAGE_AUTOMATION_PREFIX = "chat-messages-"

DUMP_PATH = "discord_tree_dump.txt"          # 원본(개인정보 포함) — 커밋 안 함
RESULTS_DIR = "results"
RESULTS_PATH = os.path.join(RESULTS_DIR, "scan_benchmark.md")  # 정제본 — 커밋
MAX_ELEMENTS_TO_DUMP = 400
SCAN_REPEATS = 3

# 본체와 동일 조건(DPI 인식).
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

# 본문이 아닌 Text 조각 분류용(본체와 같은 규칙의 축약판).
_TS_PATTERNS = [
    re.compile(r"^\d{4}-\d{2}-\d{2}"),
    re.compile(r"^\d{4}년"),
    re.compile(r"^(오전|오후)\s*\d"),
    re.compile(r"^\d{1,2}:\d{2}"),
    re.compile(r"^\d+\s*(초|분|시간|일|주|개월|달|년)\s*전$"),
]


def classify_run(text: str) -> str:
    s = text.strip()
    if not s:
        return "빈칸"
    if s in ("수정됨", "(edited)", "edited"):
        return "편집표시"
    if s.isdigit():
        return "숫자(반응 등)"
    for p in _TS_PATTERNS:
        if p.match(s):
            return "타임스탬프"
    if not any(ch.isalnum() for ch in s):
        return "문장부호/이모지"
    return "본문"


def safe(fn, default=""):
    try:
        v = fn()
        return v if v is not None else default
    except Exception:
        return default


def find_discord_window():
    """제목에 Discord가 든 창이 여럿이어도 본체(Chrome_WidgetWin_1, 최대 크기)를 고른다."""
    try:
        wins = Desktop(backend="uia").windows(
            title_re=DISCORD_TITLE_RE, top_level_only=True
        )
    except Exception as e:
        print(f"[!] 창 탐색 실패: {e}")
        return None

    candidates = []
    for w in wins:
        try:
            if not w.is_visible():
                continue
            cls = w.element_info.class_name or ""
            r = w.rectangle()
            area = (r.right - r.left) * (r.bottom - r.top)
            if area <= 0:                       # 최소화/트레이 상태
                continue
            candidates.append((cls, area, w))
        except Exception:
            continue
    chrome = [c for c in candidates if c[0] == DISCORD_WIN_CLASS]
    pool = chrome or candidates
    if not pool:
        return None
    pool.sort(key=lambda c: c[1], reverse=True)
    return pool[0][2]


def find_message_list(discord_win):
    """자식 ListItem의 automationId가 chat-messages-* 인 List(메시지 목록)를 찾는다."""
    try:
        lists = discord_win.descendants(control_type="List")
    except Exception:
        return None
    for lst in lists:
        try:
            kids = lst.children(control_type="ListItem")
        except Exception:
            continue
        for k in kids[:4]:
            auto = safe(lambda: k.element_info.automation_id)
            if auto.startswith(MESSAGE_AUTOMATION_PREFIX):
                return lst
    return None


def parent_chain(el, depth: int = 6) -> str:
    parts = []
    cur = el
    for _ in range(depth):
        try:
            info = cur.element_info
            name = (info.name or "")[:20].replace("\n", " ")
            parts.append(f"{info.control_type}|cls={info.class_name}"
                         f"|auto={info.automation_id}|name='{name}'")
            cur = cur.parent()
            if cur is None:
                break
        except Exception:
            break
    return "  ->  ".join(parts)


def measure_text_scan(target, repeats: int = SCAN_REPEATS):
    """target의 Text descendants 스캔 시간(여러 번)과 마지막 요소 목록을 반환."""
    times, els = [], []
    for _ in range(repeats):
        t0 = time.perf_counter()
        try:
            els = target.descendants(control_type="Text")
        except Exception as e:
            return [], [], f"실패: {e}"
        times.append(time.perf_counter() - t0)
    return times, els, "ok"


def _rect(el):
    try:
        r = el.rectangle()
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return None


def _visible(rect, win_rect) -> bool:
    if rect is None:
        return False
    l, t, r, b = rect
    if r <= l or b <= t:
        return False
    wl, wt, wr, wb = win_rect
    return not (r < wl or l > wr or b < wt or t > wb)


def measure_production_scan(msg_list, win_rect, repeats: int = SCAN_REPEATS):
    """본체가 실제로 하는 방식으로 측정한다:
    메시지 목록의 children(ListItem)만 받고(값싸다), 그중 '화면에 보이는' 메시지만
    Text로 파고든다. off-screen 메시지는 건드리지 않는다. 이게 매 프레임 실제 비용."""
    times = []
    vis_count = seg_count = 0
    for _ in range(repeats):
        t0 = time.perf_counter()
        vc = sc = 0
        try:
            items = msg_list.children(control_type="ListItem")
        except Exception as e:
            return [], 0, 0, f"실패: {e}"
        for it in items:
            auto = safe(lambda: it.element_info.automation_id)
            if not auto.startswith(MESSAGE_AUTOMATION_PREFIX):
                continue
            if not _visible(_rect(it), win_rect):
                continue
            vc += 1
            try:
                sc += len(it.descendants(control_type="Text"))
            except Exception:
                pass
        times.append(time.perf_counter() - t0)
        vis_count, seg_count = vc, sc
    return times, vis_count, seg_count, "ok"


def length_buckets(text_els):
    buckets = {"0-1": 0, "2-10": 0, "11-50": 0, "51-200": 0, "200+": 0}
    lengths = []
    for el in text_els:
        L = len(safe(lambda: el.window_text()).strip())
        lengths.append(L)
        if L <= 1:
            buckets["0-1"] += 1
        elif L <= 10:
            buckets["2-10"] += 1
        elif L <= 50:
            buckets["11-50"] += 1
        elif L <= 200:
            buckets["51-200"] += 1
        else:
            buckets["200+"] += 1
    lengths.sort()
    return lengths, buckets


def build_structural_sample(msg_list, win_rect, max_msgs: int = 2):
    """메시지 몇 개의 구조를 '정제'해서 반환. 실제 텍스트는 담지 않고,
    조각의 종류(본문/유저명/타임스탬프 등)와 글자 수만 보여 메시지가 어떻게
    쪼개지는지 증명한다."""
    lines = []
    try:
        items = msg_list.children(control_type="ListItem")
    except Exception:
        return lines
    shown = 0
    for it in items:
        auto = safe(lambda: it.element_info.automation_id)
        if not auto.startswith(MESSAGE_AUTOMATION_PREFIX):
            continue
        try:
            runs = it.descendants(control_type="Text")
        except Exception:
            continue
        if not runs:
            continue
        lines.append("ListItem  automationId = chat-messages-<서버ID>-<메시지ID>")
        for run in runs:
            t = safe(lambda: run.window_text())
            kind = classify_run(t)
            lines.append(f"    └─ Text  [{kind:12}]  {len(t.strip()):>4} 글자")
        lines.append("")
        shown += 1
        if shown >= max_msgs:
            break
    return lines


def write_raw_dump(text_els):
    lines = [f"Discord UIA Text 요소 덤프 (총 {len(text_els)}개)", "=" * 100, ""]
    for idx, el in enumerate(text_els[:MAX_ELEMENTS_TO_DUMP]):
        text = safe(lambda: el.window_text()).replace("\n", "\\n")
        auto = safe(lambda: el.element_info.automation_id)
        cls = safe(lambda: el.element_info.class_name)
        try:
            rr = el.rectangle()
            rect = f"({rr.left},{rr.top})-({rr.right},{rr.bottom})"
        except Exception:
            rect = "?"
        lines.append(f"[{idx:03d}] text='{text[:80]}'")
        lines.append(f"       len={len(text):3d}  automationId='{auto}'  "
                     f"className='{cls}'  rect={rect}")
        lines.append(f"       계층: {parent_chain(el)}")
        lines.append("")
    with open(DUMP_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def write_results_md(ctx):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    full_avg = ctx["full_avg"]
    scoped_avg = ctx["scoped_avg"]
    speedup = (full_avg / scoped_avg) if scoped_avg else 0

    def ms_list(times):
        return ", ".join(f"{t*1000:.0f}" for t in times) + " ms"

    md = []
    md.append("# 스캔 성능 실측 결과")
    md.append("")
    md.append("> `inspect_discord_tree.py`가 자동 생성한 벤치마크 결과입니다.")
    md.append("> 개인정보(서버명·유저명·메시지 본문)는 담지 않고, 수치와 구조만 기록합니다.")
    md.append("> 원본 덤프(`discord_tree_dump.txt`)는 개인정보 때문에 커밋하지 않습니다.")
    md.append("")
    md.append("## 측정 환경")
    md.append("")
    md.append(f"- OS: {ctx['os']}")
    md.append(f"- 측정 시각: {ctx['when']}")
    md.append(f"- Discord 창 크기: {ctx['win_w']} x {ctx['win_h']} px")
    md.append(f"- Python: {ctx['py']}")
    md.append("")
    md.append("## 핵심 결과")
    md.append("")
    md.append("| 스캔 방식 | 대상 | 평균 시간 | 개별 측정 |")
    md.append("|---|---:|---:|---|")
    md.append(f"| ① 창 전체 모든 요소 `descendants()` | {ctx['all_count']}개 | "
              f"{ctx['all_time']*1000:.0f} ms | 1회 |")
    md.append(f"| ② 창 전체 Text `descendants()` (순진) | {ctx['full_count']}개 | "
              f"**{full_avg*1000:.0f} ms** | {ms_list(ctx['full_times'])} |")
    md.append(f"| ③ 프로덕션: 보이는 메시지만 | 메시지 {ctx['vis_count']}개 | "
              f"**{scoped_avg*1000:.0f} ms** | {ms_list(ctx['scoped_times'])} |")
    md.append("")
    md.append(f"**→ ② 대비 ③이 약 {speedup:.1f}배 빠름 "
              f"({full_avg*1000:.0f} ms → {scoped_avg*1000:.0f} ms)**")
    md.append("")
    md.append("핵심은 두 가지다.")
    md.append("")
    md.append("1. **메시지만 콕 집는다.** 메시지는 전부 `automationId`가 "
              "`chat-messages-`로 시작하는 ListItem 안에 있다. 이 앵커로 목록 컨테이너를")
    md.append("   특정하면 사이드바·채널목록·헤더·유저설정을 통째로 건너뛴다(오탐 제거).")
    md.append("2. **보이는 메시지만 파고든다.** 목록의 `children(ListItem)`은 값싸게 받고,")
    md.append("   화면 밖(off-screen) 메시지는 `descendants(Text)`로 파고들지 않는다.")
    md.append("   비용이 '전체 메시지 수'가 아니라 '화면에 보이는 ~십여 개'에만 비례한다.")
    md.append("")
    md.append("> 참고: ②의 절대값은 채널마다 다르다. 긴 임베드가 많은 채널(예: 공지)은")
    md.append("> Text 요소가 400개를 넘겨 ② 스캔이 700 ms에 달하기도 한다. ③은 그런")
    md.append("> 채널에서도 보이는 메시지 수만큼만 일하므로 영향이 훨씬 작다.")
    md.append("")
    md.append("## Text 요소 길이 분포 (창 전체 기준)")
    md.append("")
    md.append(f"- 최소 {ctx['len_min']} / 중앙값 {ctx['len_median']} / 최대 {ctx['len_max']} 글자")
    md.append("")
    md.append("| 길이 구간 | 개수 |")
    md.append("|---|---:|")
    for k, v in ctx["buckets"].items():
        md.append(f"| {k} 글자 | {v} |")
    md.append("")
    md.append("짧은 조각(0~10글자)이 압도적으로 많다. 링크·멘션·이모지·문장부호가")
    md.append("전부 개별 Text로 쪼개지기 때문이다.")
    md.append("")
    md.append("## 메시지 구조 (정제됨)")
    md.append("")
    md.append("메시지 한 개(ListItem)가 실제로 어떻게 쪼개지는지 — 실제 텍스트 대신")
    md.append("조각의 종류와 글자 수만 표시한다. 한 문장이 여러 Text 조각으로 나뉘므로,")
    md.append("조각별이 아니라 **메시지 단위로 합쳐서 번역**해야 한다는 근거다.")
    md.append("")
    md.append("```")
    md.extend(ctx["structural_sample"])
    md.append("```")
    md.append("")
    md.append("## 재현 방법")
    md.append("")
    md.append("```")
    md.append("python inspect_discord_tree.py")
    md.append("```")
    md.append("")
    md.append("디스코드를 화면에 띄운 상태로 실행하면 이 파일이 다시 생성된다.")
    md.append("메시지가 0개로 잡히면 `Win+Ctrl+Enter`로 내레이터를 켜고 재실행한다.")
    md.append("")

    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(md))


def main():
    print("=" * 70)
    print("Discord UIA 트리 진단 & 벤치마크")
    print("=" * 70)

    win = find_discord_window()
    if win is None:
        print("\n창을 못 찾았습니다. 디스코드를 화면에 띄웠는지(최소화/트레이 X),")
        print("'스크린 리더'(Win+Ctrl+Enter)가 필요한지 확인하세요.")
        sys.exit(1)

    r = win.rectangle()
    win_w, win_h = r.right - r.left, r.bottom - r.top
    print(f"\n[창] 크기 {win_w}x{win_h}")

    # 1) 창 전체 Text 스캔
    print(f"\n[측정] 창 전체 Text 스캔 ({SCAN_REPEATS}회)...")
    full_times, full_els, status = measure_text_scan(win)
    if not full_els:
        print(f"[!] Text 요소 0개 ({status}). 내레이터를 켜고 다시 실행하세요.")
        sys.exit(1)
    for i, t in enumerate(full_times):
        print(f"    {i+1}회차: {t*1000:.0f} ms, {len(full_els)}개")
    full_avg = sum(full_times) / len(full_times)

    # 2) 창 전체 모든 요소 (참고)
    print("[측정] 창 전체 모든 요소 스캔 (참고, 1회)...")
    t0 = time.perf_counter()
    all_els = win.descendants()
    all_time = time.perf_counter() - t0
    print(f"    {all_time*1000:.0f} ms, {len(all_els)}개")

    # 3) 프로덕션 방식: 메시지 목록 children + 보이는 메시지만 파고들기
    print(f"\n[측정] 프로덕션 방식(보이는 메시지만) ({SCAN_REPEATS}회)...")
    win_rect = (r.left, r.top, r.right, r.bottom)
    msg_list = find_message_list(win)
    scoped_times, vis_count, seg_count = [], 0, 0
    if msg_list is None:
        print("[!] 메시지 목록을 못 찾음(접근성 트리 비활성?). 최적화 측정 생략.")
    else:
        scoped_times, vis_count, seg_count, status = measure_production_scan(
            msg_list, win_rect)
        for i, t in enumerate(scoped_times):
            print(f"    {i+1}회차: {t*1000:.0f} ms "
                  f"(보이는 메시지 {vis_count}개, Text 조각 {seg_count}개)")

    # 4) 길이 분포
    lengths, buckets = length_buckets(full_els)
    n = len(lengths)

    # 5) 구조 샘플 (정제)
    structural = build_structural_sample(msg_list, win_rect) \
        if msg_list else ["(메시지 목록 비활성 — 내레이터 켜고 재실행)"]

    # 6) 원본 덤프 (개인정보 포함, 커밋 안 함)
    write_raw_dump(full_els)
    print(f"\n[저장] 원본 덤프 → {DUMP_PATH} (개인정보 포함, git 제외)")

    # 7) 정제 결과 (커밋)
    if scoped_times:
        scoped_avg = sum(scoped_times) / len(scoped_times)
        ctx = {
            "os": platform.platform(),
            "py": platform.python_version(),
            "when": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "win_w": win_w, "win_h": win_h,
            "full_times": full_times, "full_avg": full_avg,
            "full_count": len(full_els),
            "all_time": all_time, "all_count": len(all_els),
            "scoped_times": scoped_times, "scoped_avg": scoped_avg,
            "vis_count": vis_count, "seg_count": seg_count,
            "len_min": lengths[0], "len_median": lengths[n // 2],
            "len_max": lengths[-1], "buckets": buckets,
            "structural_sample": structural,
        }
        write_results_md(ctx)
        print(f"[저장] 정제 벤치마크 → {RESULTS_PATH} (커밋용)")
        speedup = full_avg / scoped_avg if scoped_avg else 0
        print(f"\n★ 결과: 창 전체 {full_avg*1000:.0f}ms → 메시지목록만 "
              f"{scoped_avg*1000:.0f}ms  (약 {speedup:.1f}배)")
    else:
        print("[!] 범위축소 측정을 못 해서 정제 결과는 생성하지 않았습니다.")


if __name__ == "__main__":
    main()
