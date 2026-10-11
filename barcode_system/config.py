"""
실행 프로그램 공용 설정 처리: 명령줄 옵션 > --config INI 파일 > 기본값.

OPTIONS 항목: (INI 섹션, 키, 명령줄 옵션, 형, 기본값, 선택지, 도움말)
결과 Namespace의 속성 이름은 '<섹션>_<키>' (예: mqtt_host).
"""
import argparse
import configparser
import os


def bool_value(v):
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    raise ValueError(f"참/거짓 값이 아닙니다: {v!r}")


def build_parser(description, options):
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--config", help="INI 설정 파일 (config/*.example.ini 참조)")
    ap.add_argument("--debug", action="store_true", help="상세 로그 출력")
    for section, key, flag, typ, default, choices, help_ in options:
        ap.add_argument(flag, dest=f"{section}_{key}", type=typ, choices=choices, default=None,
                        metavar=None if choices else key.upper(),
                        help=(help_ + " " if help_ else "") + f"(기본 {default})")
    return ap


def resolve(ap, args, options):
    """명령줄 값이 없으면 INI, 그것도 없으면 기본값. 잘못된 INI 값은 ap.error로 종료."""
    ini = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    if args.config and not ini.read(args.config, encoding="utf-8"):
        ap.error(f"설정 파일을 읽을 수 없습니다: {args.config}")

    cfg = argparse.Namespace(debug=args.debug)
    for section, key, flag, typ, default, choices, _ in options:
        value = getattr(args, f"{section}_{key}")
        if value is None and ini.has_option(section, key) and ini.get(section, key).strip():
            raw = ini.get(section, key).strip()
            try:
                value = typ(raw)
            except ValueError:
                ap.error(f"설정 파일 [{section}] {key} 값이 잘못됐습니다: {raw!r}")
            if choices and value not in choices:
                ap.error(f"설정 파일 [{section}] {key}는 {choices} 중 하나여야 합니다: {raw!r}")
        setattr(cfg, f"{section}_{key}", default if value is None else value)
    return cfg


def read_password(ap, path, env_name):
    """비밀번호 파일의 첫 줄, 파일을 지정하지 않았으면 환경 변수. 명령줄에는 받지 않는다."""
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                return f.readline().rstrip("\r\n")
        except OSError as e:
            ap.error(f"비밀번호 파일을 읽을 수 없습니다: {e}")
    return os.environ.get(env_name)


def setup_logging(debug):
    import logging
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
