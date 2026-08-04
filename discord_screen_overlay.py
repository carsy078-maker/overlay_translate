"""
Discord 실시간 화면 오버레이 번역기 (Windows 전용) — v2

v1 대비 바뀐 핵심(실측 기반):
  1. 스캔 범위 축소:  창 전체(≈700ms, 407요소)를 훑지 않고, 채팅 메시지 목록
     컨테이너만 스캔한다. 메시지는 전부 automationId가 "chat-messages-"로
     시작하는 ListItem 안에 들어 있다(언어/버전 무관 안정적 앵커).
  2. 메시지 단위 처리:  디스코드는 메시지 한 줄을 링크/멘션/이모지/문장부호마다
     별개의 Text 조각으로 쪼갠다. 조각을 따로 번역하면 "(", " in " 같은 쓰레기가
     번역되고 문맥이 깨진다. 그래서 한 메시지(ListItem)의 Text 조각을 모아 하나로
     합친 뒤, 통째로 한 번 번역하고, 그 위에 박스 하나를 덮는다.
  3. 유저명/타임스탬프/편집표시/반응숫자 제거:  본문만 남긴다.
  4. 번역 비동기화:  좌표 추적(스캔)과 번역(HTTP)을 분리. 스캔 스레드는 캐시만
     읽고, 캐시에 없는 문장은 번역 워커 스레드 큐에 넣는다. 번역이 도착하면
     다음 프레임에 오버레이가 채워진다 → 스크롤/좌표 추적이 안 끊긴다.
  5. 라벨 diff:  메시지ID를 key로 라벨을 재사용. 매 프레임 전부 지웠다 다시
     만들지 않아 깜빡임이 준다.

번역 백엔드: 무료 비공식 Google 엔드포인트(translate_a/single). API 키 불필요.
  대량 호출 시 일시적으로 차단될 수 있으나 개인용에는 충분.

사전 준비:
  1. (최신 디스코드는 수동 토글이 없음) 접근성 트리가 비어 있으면
     Win+Ctrl+Enter 로 내레이터를 켜서 트리를 활성화한다.
  2. pip install -r requirements.txt
  3. python discord_screen_overlay.py
"""

import ctypes
import re
import sys
import threading
import time
from dataclasses import dataclass
from queue import Queue, Empty
from typing import Optional

import requests
from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from pywinauto import Desktop


def _log(*args):
    """exe(콘솔 없는 --windowed)로 빌드하면 sys.stdout이 None이라 print가 터진다.
    안전하게 감싼다. 콘솔이 있으면 그대로 찍히고, 없으면 조용히 무시한다."""
    try:
        print(*args)
    except Exception:
        pass

# ── 설정 ──────────────────────────────────────────────
DISCORD_TITLE_RE = ".*Discord.*"
TARGET_LANG = "ko"
SCAN_INTERVAL_SEC = 0.35
MIN_TEXT_LEN = 2
MAX_TEXT_LEN = 2000
MESSAGE_AUTOMATION_PREFIX = "chat-messages-"

# 한 화면에 번역할 줄이 10개 넘게 나오므로 워커를 여러 개 두고 동시에 번역한다.
# 다만 무료(비공식) 엔드포인트는 동시 요청을 너무 때리면 레이트리밋을 거니 과하지 않게.
TRANSLATE_WORKERS = 4
TRANSLATE_MAX_RETRIES = 3
TRANSLATE_RETRY_DELAY = 0.7   # 재시도마다 0.7s, 1.4s, 2.1s 로 점증

# 원문 rect 높이가 이 값 이하면 '한 줄짜리'로 보고, 줄바꿈 없이 가로로 늘린다.
SINGLE_LINE_MAX_H = 40

# 고해상도(배율 조정된) 모니터에서 좌표가 어긋나지 않도록 DPI 인식을 켠다.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

# ── 본문이 아닌 것들을 걸러내는 규칙 ────────────────────
# 타임스탬프(절대/상대), 편집 표시 등. 한 메시지 안에서 본문만 남기려고 쓴다.
_TS_PATTERNS = [
    re.compile(r"^\d{4}-\d{2}-\d{2}"),          # 2026-04-14 오전 7:21
    re.compile(r"^\d{4}년"),                     # 2026년 4월 14일 ...
    re.compile(r"^(오전|오후)\s*\d"),            # 오전 5:33
    re.compile(r"^\d{1,2}:\d{2}"),               # 12:34
    re.compile(r"^\d+\s*(초|분|시간|일|주|개월|달|년)\s*전$"),  # 3달 전
    re.compile(r"^(어제|오늘)"),
]
_EDITED = {"수정됨", "(edited)", "edited"}


def _classify_run(text: str) -> str:
    """메시지 안의 Text 조각 하나를 분류한다."""
    s = text.strip()
    if not s:
        return "empty"
    if s in _EDITED:
        return "edited"
    if s.isdigit():                       # 반응 개수 등
        return "count"
    for p in _TS_PATTERNS:
        if p.match(s):
            return "timestamp"
    if not any(ch.isalnum() for ch in s):  # 문장부호/이모지만 → 무의미
        return "punct"
    return "content"


# ── 번역 ──────────────────────────────────────────────
def _looks_like_korean(text: str) -> bool:
    hangul = sum(1 for ch in text if "가" <= ch <= "힣")
    letters = sum(1 for ch in text if ch.isalpha())
    return letters > 0 and hangul / letters > 0.6


# 요청 실패(네트워크/레이트리밋)를 '번역할 필요 없음(None)'과 구분하기 위한 표식.
# 이 둘을 뭉뚱그리면 실패한 문장이 영구히 캐시돼서 다시는 번역되지 않는다.
_FAILED = object()


def _google_free_translate(text: str, session=None):
    """무료 비공식 Google 엔드포인트.

    반환: 번역문(str) / None(한국어라 번역 불필요) / _FAILED(요청 실패 → 재시도 대상)
    """
    http = session or requests
    try:
        res = http.get(
            "https://translate.googleapis.com/translate_a/single",
            params={
                "client": "gtx",
                "sl": "auto",
                "tl": TARGET_LANG,
                "dt": "t",
                "q": text,
            },
            timeout=5,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        res.raise_for_status()
        data = res.json()
    except Exception:
        return _FAILED          # 네트워크 오류/429 등 → 나중에 다시 시도

    try:
        detected = data[2] if len(data) > 2 else None
        if detected == TARGET_LANG:
            return None
        segments = data[0]
        translated = "".join(seg[0] for seg in segments if seg and seg[0])
        translated = translated.strip()
        if not translated or translated == text.strip():
            return None
        return translated
    except Exception:
        return _FAILED          # 응답 형식이 깨짐 → 역시 재시도


class TranslationManager:
    """번역 캐시 + 백그라운드 워커 풀.

    get(text): 캐시에 있으면 즉시 반환(번역문 or None), 없으면 큐에 넣고 None.
    None은 '아직 없음 / 한국어 / 실패' 모두를 뜻한다. 워커가 채우면 다음 프레임에 뜬다.

    줄 단위로 쪼개면 한 화면에 번역할 문장이 10개 넘게 나온다. 워커가 하나면
    HTTP 왕복을 줄줄이 기다려서 체감 딜레이가 커지므로, 워커를 여러 개 두고
    동시에 처리한다. 각 워커는 Session을 재사용해 커넥션도 아낀다.
    """

    def __init__(self, num_workers: int = TRANSLATE_WORKERS):
        self._cache: dict[str, Optional[str]] = {}
        self._pending: set[str] = set()
        self._queue: "Queue[str]" = Queue()
        self._lock = threading.Lock()
        for _ in range(num_workers):
            threading.Thread(target=self._worker, daemon=True).start()

    def get(self, text: str) -> Optional[str]:
        with self._lock:
            if text in self._cache:
                return self._cache[text]
            if text not in self._pending:
                self._pending.add(text)
                self._queue.put((text, 0))
        return None

    def _worker(self):
        session = requests.Session()
        while True:
            try:
                text, attempt = self._queue.get(timeout=1)
            except Empty:
                continue

            if _looks_like_korean(text):
                result = None
            else:
                result = _google_free_translate(text, session)

            if result is _FAILED:
                # 실패는 캐시하지 않는다. 캐시해버리면 그 문장은 영원히 원문으로 남는다.
                if attempt < TRANSLATE_MAX_RETRIES:
                    time.sleep(TRANSLATE_RETRY_DELAY * (attempt + 1))  # 점증 backoff
                    self._queue.put((text, attempt + 1))
                    continue                       # _pending 유지 → 중복 큐잉 방지
                _log(f"[translate] 포기(재시도 {attempt}회 실패): {text[:40]!r}")
                result = None

            with self._lock:
                self._cache[text] = result
                self._pending.discard(text)


# ── 스캔 (메시지 목록만) ────────────────────────────────
@dataclass
class MessageBlock:
    msg_id: str
    text: str
    rect: tuple[int, int, int, int]  # left, top, right, bottom (스크린 좌표)


class _ListCache:
    def __init__(self):
        self.msg_list = None


def _rect_tuple(el) -> Optional[tuple[int, int, int, int]]:
    try:
        r = el.rectangle()
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return None


def _is_visible_rect(rect, win_rect) -> bool:
    l, t, r, b = rect
    if r <= l or b <= t:                     # 넓이 0 (렌더 안 됨)
        return False
    wl, wt, wr, wb = win_rect
    # 창 영역과 겹치는지 (완전히 벗어난 스크롤 밖 메시지 제외)
    return not (r < wl or l > wr or b < wt or t > wb)


def find_discord_window():
    try:
        return Desktop(backend="uia").window(
            title_re=DISCORD_TITLE_RE
        ).wrapper_object()
    except Exception:
        return None


def _has_message_items(lst) -> bool:
    """이 List가 실제 채팅 메시지(chat-messages-*)를 담고 있는가."""
    try:
        kids = lst.children(control_type="ListItem")
    except Exception:
        return False
    for k in kids:
        try:
            auto = k.element_info.automation_id or ""
        except Exception:
            continue
        if auto.startswith(MESSAGE_AUTOMATION_PREFIX):
            return True
    return False


def find_message_list(discord_win):
    """자식 ListItem의 automationId가 chat-messages-* 인 List를 찾는다.

    창 전체에서 List 컨트롤(수십 개 수준)만 훑으므로 Text 전체 스캔보다 훨씬 싸다.
    한 번 찾으면 캐시해서 재사용한다.
    """
    try:
        lists = discord_win.descendants(control_type="List")
    except Exception:
        return None
    for lst in lists:
        if _has_message_items(lst):
            return lst
    return None


# 같은 시각적 줄로 묶을 때 허용하는 세로 오차(px). 같은 줄 조각은 top이 거의
# 같고(<5px), 다른 줄은 줄높이(~18px 이상)만큼 벌어진다.
_LINE_Y_THRESHOLD = 14


def _group_runs_into_lines(runs_data):
    """(text, rect) 조각들을 화면 세로 위치(같은 줄)로 묶어 세그먼트 리스트로.

    한 줄 안에서 멘션/코드/링크로 쪼개진 조각은 다시 이어붙여 문맥을 살린다.
    각 세그먼트는 자기 줄의 rect만 차지하므로 오버레이가 줄 단위로 정확히 얹힌다.
    """
    runs_data.sort(key=lambda x: (x[1][1], x[1][0]))  # top, left 순
    groups: list[dict] = []
    for text, rect in runs_data:
        top = rect[1]
        if groups and abs(top - groups[-1]["top"]) <= _LINE_Y_THRESHOLD:
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


def _extract_message_segments(item, win_rect):
    """메시지 ListItem 하나에서 (본문줄 텍스트, rect) 세그먼트 목록을 뽑는다."""
    try:
        runs = item.descendants(control_type="Text")
    except Exception:
        return []

    seen_ts = False
    collected: list[tuple[str, tuple[int, int, int, int]]] = []

    for run in runs:
        try:
            t = run.window_text()
        except Exception:
            continue
        kind = _classify_run(t)
        if kind == "timestamp":
            seen_ts = True
            continue
        if kind in ("empty", "edited", "count", "punct"):
            continue
        if not seen_ts:
            # 타임스탬프보다 앞에 오는 본문류 = 보통 유저명 → 버린다
            continue
        r = _rect_tuple(run)
        if not r or not _is_visible_rect(r, win_rect):
            continue      # 화면에 렌더 안 된 조각은 위치를 못 잡으므로 제외
        collected.append((t, r))

    if not collected:
        return []

    segments = _group_runs_into_lines(collected)
    return [
        (text, rect)
        for text, rect in segments
        if MIN_TEXT_LEN <= len(text) <= MAX_TEXT_LEN
    ]


class Scanner(QObject):
    blocksReady = pyqtSignal(list)          # list[(MessageBlock, translated)]
    windowRectReady = pyqtSignal(tuple)
    statusReady = pyqtSignal(bool, int, int)  # (디스코드 감지됨?, 보이는 메시지, 번역표시)

    def __init__(self, translator: TranslationManager):
        super().__init__()
        self._running = True
        self._translator = translator
        self._cache = _ListCache()

    def stop(self):
        self._running = False

    def _get_message_list(self, discord_win):
        """캐시된 List 핸들을 재사용하되, 채널을 전환하면 다시 찾는다.

        주의: 채널을 바꾸면 옛 List 핸들이 예외를 던지지 않고 '빈 자식 목록'을
        돌려준다. 그래서 살아있는지(예외 여부)가 아니라, chat-messages 자식이
        실제로 있는지로 검증해야 한다.
        """
        if self._cache.msg_list is not None:
            if _has_message_items(self._cache.msg_list):
                return self._cache.msg_list
            self._cache.msg_list = None      # 채널 바뀜 → 다시 찾는다

        lst = find_message_list(discord_win)
        self._cache.msg_list = lst
        return lst

    def run(self):
        _log("[scanner] 시작. 디스코드 창을 찾는 중...")
        while self._running:
            discord_win = find_discord_window()
            if discord_win is None or not discord_win.is_visible():
                self.statusReady.emit(False, 0, 0)
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            win_rect = _rect_tuple(discord_win)
            if win_rect is None:
                self.statusReady.emit(False, 0, 0)
                time.sleep(SCAN_INTERVAL_SEC)
                continue
            self.windowRectReady.emit(win_rect)

            msg_list = self._get_message_list(discord_win)
            if msg_list is None:
                # 접근성 트리가 비었을 가능성 (내레이터 켜기 안내)
                self.statusReady.emit(True, 0, 0)
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            t0 = time.perf_counter()
            try:
                items = msg_list.children(control_type="ListItem")
            except Exception:
                self._cache.msg_list = None
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            results = []
            scanned = 0
            for item in items:
                try:
                    auto = item.element_info.automation_id or ""
                except Exception:
                    continue
                if not auto.startswith(MESSAGE_AUTOMATION_PREFIX):
                    continue
                item_rect = _rect_tuple(item)
                if item_rect is None or not _is_visible_rect(item_rect, win_rect):
                    continue
                scanned += 1
                segments = _extract_message_segments(item, win_rect)
                for i, (text, rect) in enumerate(segments):
                    translated = self._translator.get(text)
                    if translated:
                        key = f"{auto}#{i}"
                        results.append(
                            (MessageBlock(msg_id=key, text=text, rect=rect),
                             translated)
                        )

            dt = (time.perf_counter() - t0) * 1000
            _log(f"[scanner] 보이는 메시지 {scanned}개, 번역표시 {len(results)}개, "
                  f"스캔 {dt:.0f}ms")
            self.statusReady.emit(True, scanned, len(results))
            self.blocksReady.emit(results)
            time.sleep(SCAN_INTERVAL_SEC)


# ── 오버레이 ──────────────────────────────────────────
class OverlayWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        # key(msg_id) -> (QLabel, last_text)
        self.labels: dict[str, tuple[QLabel, str]] = {}
        self.origin = (0, 0, 0, 0)

    def set_window_rect(self, rect):
        self.origin = rect
        left, top, right, bottom = rect
        self.setGeometry(left, top, right - left, bottom - top)

    def _fit_label(self, lbl: QLabel, rect):
        """번역문이 원문보다 길어도 깨지지 않게 라벨 크기를 잡는다.

        원문 rect에 억지로 우겨넣으면(고정 크기 + wordWrap) 한 줄 높이 박스에
        두 줄이 들어가 글자가 잘리고 겹친다. 그래서 위치(좌상단)만 원문에 맞추고,
        크기는 '원문을 덮을 만큼(최소) + 번역문이 들어갈 만큼(필요시 확장)'으로 준다.
        """
        ox, oy, _, _ = self.origin
        l, t, r, b = rect
        x, y = l - ox, t - oy
        ow, oh = max(r - l, 1), max(b - t, 1)

        if oh <= SINGLE_LINE_MAX_H:
            # 원문이 한 줄 → 줄바꿈 없이 오른쪽으로 늘린다 (줄바꿈 깨짐 방지)
            lbl.setWordWrap(False)
            hint = lbl.sizeHint()
            w = max(ow, hint.width())
            h = max(oh, hint.height())
        else:
            # 원문이 여러 줄인 문단 → 같은 폭으로 줄바꿈하고 아래로만 늘린다
            lbl.setWordWrap(True)
            w = ow
            h = max(oh, lbl.heightForWidth(w))

        lbl.setGeometry(x, y, w, h)

    def update_blocks(self, results):
        current_keys = set()

        for block, translated in results:
            key = block.msg_id
            current_keys.add(key)

            existing = self.labels.get(key)
            if existing is None:
                lbl = QLabel(translated, self)
                lbl.setStyleSheet(
                    "background-color: rgba(30,30,30,255);"
                    "color: white; font-size: 13px; padding: 1px 4px;"
                    "border-radius: 3px;"
                )
                lbl.show()
                self.labels[key] = (lbl, translated)
            else:
                lbl, last_text = existing
                if translated != last_text:
                    lbl.setText(translated)
                    self.labels[key] = (lbl, translated)

            self._fit_label(lbl, block.rect)   # 스크롤 따라 위치/크기 갱신

        # 화면에서 사라진 메시지의 라벨 제거
        for key in list(self.labels.keys()):
            if key not in current_keys:
                lbl, _ = self.labels.pop(key)
                lbl.deleteLater()


class ControlWindow(QWidget):
    """실행 상태를 보여주고, 번역 토글/종료를 할 수 있는 작은 조작 창."""

    def __init__(self, overlay: OverlayWindow):
        super().__init__()
        self.overlay = overlay
        self.setWindowTitle("Discord 번역기")
        self.setFixedWidth(300)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)

        self.status = QLabel("● 시작하는 중…")
        self.status.setStyleSheet("font-size: 14px; font-weight: bold;")
        self.detail = QLabel("디스코드 창을 찾는 중입니다.")
        self.detail.setStyleSheet("color: #666; font-size: 12px;")
        self.detail.setWordWrap(True)

        self.toggle_btn = QPushButton("번역 숨기기")
        self.toggle_btn.clicked.connect(self._toggle_overlay)
        quit_btn = QPushButton("종료")
        quit_btn.clicked.connect(self._quit)

        buttons = QHBoxLayout()
        buttons.addWidget(self.toggle_btn)
        buttons.addWidget(quit_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(self.detail)
        layout.addLayout(buttons)

    def _toggle_overlay(self):
        show = not self.overlay.isVisible()
        self.overlay.setVisible(show)
        self.toggle_btn.setText("번역 숨기기" if show else "번역 보이기")

    def _quit(self):
        QApplication.instance().quit()

    def closeEvent(self, event):
        # 창의 X 버튼 = 프로그램 종료
        QApplication.instance().quit()
        event.accept()

    def update_status(self, found: bool, scanned: int, translated: int):
        if not found:
            self.status.setText("● 대기 중")
            self.status.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: #b0902b;")
            self.detail.setText("디스코드 창을 찾는 중입니다. 디스코드를 켜 두세요.")
        elif scanned == 0:
            self.status.setText("● 실행 중")
            self.status.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: #2b8a3e;")
            self.detail.setText(
                "디스코드는 찾았지만 메시지를 못 읽고 있습니다.\n"
                "번역이 안 뜨면 Win+Ctrl+Enter 로 내레이터를 켜 보세요.")
        else:
            self.status.setText("● 번역 중")
            self.status.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: #2b8a3e;")
            self.detail.setText(
                f"보이는 메시지 {scanned}개 · 번역 표시 {translated}개")


def main():
    app = QApplication(sys.argv)
    # 오버레이는 투명한 도구 창이라 '마지막 창'으로 잡혀 앱이 꺼질 수 있다.
    # 종료는 조작 창(ControlWindow)에서만 하도록 자동 종료를 끈다.
    app.setQuitOnLastWindowClosed(False)

    overlay = OverlayWindow()
    overlay.show()

    control = ControlWindow(overlay)
    control.show()

    translator = TranslationManager()
    scanner = Scanner(translator)
    scanner.windowRectReady.connect(overlay.set_window_rect)
    scanner.blocksReady.connect(overlay.update_blocks)
    scanner.statusReady.connect(control.update_status)

    thread = threading.Thread(target=scanner.run, daemon=True)
    thread.start()

    _log("=" * 60)
    _log("오버레이 번역기 실행 중. 조작 창에서 상태 확인/종료 가능.")
    _log("=" * 60)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
