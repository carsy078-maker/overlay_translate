"""
Discord UIA 트리 실측 진단 스크립트 (spike)

목적: 오버레이 번역기를 본격 구현하기 전에, "디스코드의 접근성 트리가
실제로 쓸 만한가"를 숫자로 확인한다. 아래 3가지를 측정/덤프한다.

  1. 스캔 성능   - descendants(control_type="Text") 한 번에 걸리는 시간
  2. 요소 개수   - 화면에 잡히는 Text 요소가 몇 개인지 (트리가 얼마나 큰지)
  3. 요소 상세   - 각 Text 요소의 텍스트/AutomationId/ClassName/좌표/부모 계층
                   → 어떤 조건으로 "메시지 본문"만 걸러낼 수 있는지 판단 근거

사전 준비:
  1. 디스코드 설정 > 접근성 > "스크린 리더 지원" 켜기 (안 켜면 트리가 비어 있음)
  2. pip install -r requirements.txt
  3. 활발한 대화가 보이는 채널을 열어둔 상태로 실행

실행:
  python inspect_discord_tree.py

결과:
  - 콘솔에 요약(스캔 시간, 개수) 출력
  - discord_tree_dump.txt 에 요소별 상세 저장 (여기 붙여서 같이 분석)
"""

import ctypes
import sys
import time
from typing import Optional

from pywinauto import Desktop

DISCORD_TITLE_RE = ".*Discord.*"
DUMP_PATH = "discord_tree_dump.txt"
MAX_ELEMENTS_TO_DUMP = 400  # 너무 많으면 앞쪽만 덤프 (개수 자체는 전부 카운트)

# 고해상도 모니터에서 좌표가 어긋나지 않도록 DPI 인식을 켠다 (본체와 동일 조건).
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass


def find_discord_window():
    try:
        return Desktop(backend="uia").window(
            title_re=DISCORD_TITLE_RE
        ).wrapper_object()
    except Exception as e:
        print(f"[!] 디스코드 창을 못 찾음: {e}")
        return None


def parent_chain(el, depth: int = 6) -> str:
    """요소에서 위로 올라가며 (ControlType/ClassName/AutomationId) 계층을 문자열로."""
    parts = []
    cur = el
    for _ in range(depth):
        try:
            info = cur.element_info
            ct = info.control_type
            cls = info.class_name
            auto = info.automation_id
            name = (info.name or "")[:20].replace("\n", " ")
            parts.append(f"{ct}|cls={cls}|auto={auto}|name='{name}'")
            cur = cur.parent()
            if cur is None:
                break
        except Exception:
            break
    return "  ->  ".join(parts)


def safe(fn, default=""):
    try:
        v = fn()
        return v if v is not None else default
    except Exception:
        return default


def measure_scan(discord_win, control_type: Optional[str]):
    """지정한 control_type으로 descendants 스캔 시간과 개수를 잰다."""
    t0 = time.perf_counter()
    try:
        if control_type:
            els = discord_win.descendants(control_type=control_type)
        else:
            els = discord_win.descendants()
    except Exception as e:
        return None, [], f"실패: {e}"
    dt = time.perf_counter() - t0
    return dt, els, "ok"


def main():
    print("=" * 70)
    print("Discord UIA 트리 진단 시작")
    print("=" * 70)

    win = find_discord_window()
    if win is None:
        print("\n창을 못 찾았습니다. 디스코드가 켜져 있고 최소화되지 않았는지,")
        print("그리고 '스크린 리더 지원'이 켜져 있는지 확인하세요.")
        sys.exit(1)

    try:
        r = win.rectangle()
        print(f"\n[창] 위치: ({r.left},{r.top})-({r.right},{r.bottom}) "
              f"크기 {r.right - r.left}x{r.bottom - r.top}")
    except Exception as e:
        print(f"[!] 창 좌표 읽기 실패: {e}")

    # ── 1) 전체 descendants vs Text-only 스캔 시간 비교 ──────────────
    print("\n[측정] 스캔 시간 (3회 반복, 평균이 실제 주기에 영향)")

    print("  · control_type='Text' 만:")
    text_times = []
    text_els = []
    for i in range(3):
        dt, els, status = measure_scan(win, "Text")
        if dt is None:
            print(f"    {i+1}회차: {status}")
            continue
        text_times.append(dt)
        text_els = els
        print(f"    {i+1}회차: {dt*1000:8.1f} ms, {len(els)} 개")

    if text_times:
        avg = sum(text_times) / len(text_times)
        print(f"  → Text 스캔 평균: {avg*1000:.1f} ms "
              f"({'0.6초 주기 OK' if avg < 0.4 else '⚠ 주기 못 따라감 — 범위 축소 필요'})")

    # 전체 트리 크기 참고용 (한 번만, 느릴 수 있음)
    print("  · 전체 descendants (참고, 느릴 수 있음):")
    dt_all, all_els, status = measure_scan(win, None)
    if dt_all is not None:
        print(f"    {dt_all*1000:.1f} ms, {len(all_els)} 개 (전체 요소)")
    else:
        print(f"    {status}")

    if not text_els:
        print("\n[!] Text 요소가 0개입니다. '스크린 리더 지원'이 꺼져 있을 확률이 높습니다.")
        sys.exit(1)

    # ── 2) 텍스트 길이 분포 (필터 임계값 잡는 근거) ──────────────────
    lengths = []
    for el in text_els:
        t = safe(lambda: el.window_text())
        lengths.append(len(t.strip()))
    lengths.sort()
    if lengths:
        n = len(lengths)
        print(f"\n[분포] Text 길이: 최소 {lengths[0]}, 중앙값 {lengths[n//2]}, "
              f"최대 {lengths[-1]}")
        buckets = {"0-1": 0, "2-10": 0, "11-50": 0, "51-200": 0, "200+": 0}
        for L in lengths:
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
        print(f"        구간별 개수: {buckets}")

    # ── 3) 요소별 상세 덤프 (AutomationId/계층 확인용) ──────────────
    print(f"\n[덤프] 요소 상세를 {DUMP_PATH} 에 저장 중 "
          f"(최대 {MAX_ELEMENTS_TO_DUMP}개)...")

    lines = []
    lines.append("Discord UIA Text 요소 덤프")
    lines.append(f"총 Text 요소: {len(text_els)}개")
    lines.append("=" * 100)
    lines.append("")

    for idx, el in enumerate(text_els[:MAX_ELEMENTS_TO_DUMP]):
        text = safe(lambda: el.window_text()).replace("\n", "\\n")
        auto = safe(lambda: el.element_info.automation_id)
        cls = safe(lambda: el.element_info.class_name)
        try:
            rr = el.rectangle()
            rect = f"({rr.left},{rr.top})-({rr.right},{rr.bottom})"
        except Exception:
            rect = "?"
        chain = parent_chain(el)

        lines.append(f"[{idx:03d}] text='{text[:80]}'")
        lines.append(f"       len={len(text):3d}  automationId='{auto}'  "
                     f"className='{cls}'  rect={rect}")
        lines.append(f"       계층: {chain}")
        lines.append("")

    try:
        with open(DUMP_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"  → 저장 완료: {DUMP_PATH}")
    except Exception as e:
        print(f"  [!] 저장 실패: {e}")

    # ── 요약 ────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("판단 가이드")
    print("=" * 70)
    print("  1. Text 스캔 평균이 400ms 넘으면 → descendants 범위 축소가 필수")
    print("     (채팅 컨테이너를 먼저 특정한 뒤 그 서브트리만 스캔)")
    print(f"  2. {DUMP_PATH} 에서 실제 '메시지 본문' 요소를 찾아,")
    print("     그것들이 공유하는 automationId / className / 부모 계층 패턴을 확인")
    print("     → 그 조건을 필터에 넣으면 유저명·타임스탬프·버튼 오탐이 준다")
    print("  3. 덤프 내용을 붙여주면 함께 필터 조건을 설계할 수 있음")


if __name__ == "__main__":
    main()
