"""config 로딩/우선순위 테스트."""

from config import Config, load_config


def test_defaults_when_no_file(tmp_path):
    cfg = load_config(str(tmp_path))          # ini 없음 → 기본값
    assert cfg.target_lang == "ko"
    assert cfg.translate_workers == 4
    assert cfg.scan_interval_sec == 0.35


def _write_ini(tmp_path, body: str):
    p = tmp_path / "translator.ini"
    p.write_text("[translator]\n" + body, encoding="utf-8")
    return str(tmp_path)


def test_ini_overrides_defaults(tmp_path):
    d = _write_ini(tmp_path, "target_lang = en\ntranslate_workers = 8\n")
    cfg = load_config(d)
    assert cfg.target_lang == "en"
    assert cfg.translate_workers == 8         # int로 변환
    assert isinstance(cfg.translate_workers, int)
    assert cfg.min_text_len == 2              # 안 적은 값은 기본값 유지


def test_ini_inline_comment_stripped(tmp_path):
    d = _write_ini(tmp_path, "target_lang = ja   ; 일본어\n")
    cfg = load_config(d)
    assert cfg.target_lang == "ja"            # 주석이 값에 안 섞임


def test_env_overrides_ini(tmp_path, monkeypatch):
    d = _write_ini(tmp_path, "target_lang = en\n")
    monkeypatch.setenv("DTL_TARGET_LANG", "zh-CN")
    monkeypatch.setenv("DTL_TRANSLATE_WORKERS", "2")
    cfg = load_config(d)
    assert cfg.target_lang == "zh-CN"         # 환경변수가 ini보다 우선
    assert cfg.translate_workers == 2


def test_bad_value_falls_back_to_default(tmp_path):
    d = _write_ini(tmp_path, "translate_workers = 안숫자\n")
    cfg = load_config(d)
    assert cfg.translate_workers == Config().translate_workers   # 변환 실패 → 기본값


def test_float_coercion(tmp_path):
    d = _write_ini(tmp_path, "scan_interval_sec = 0.5\n")
    cfg = load_config(d)
    assert cfg.scan_interval_sec == 0.5
    assert isinstance(cfg.scan_interval_sec, float)
