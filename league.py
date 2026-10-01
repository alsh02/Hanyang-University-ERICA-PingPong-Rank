"""리그전(토너먼트) 운영.

운영진이 방을 개설하면 참가 코드가 생기고, 부원들은 코드로 들어와 명단에서 자기 이름을 고른다.
운영진이 '대진표 만들기'를 누르면 참가자를 부수 기준으로 상위부·중위부·하위부로 나눠 그룹마다 단판 토너먼트 대진표를 만든다(편성 중).
편성 중에는 운영진만 대진표를 보고 고치며, 참가 접수는 '토너먼트 시작'을 누를 때까지 열려 있다.
경기가 끝나면 선수(또는 점수판)가 결과를 보고하고, 운영진이 확정하면 승자가 다음 대진으로 올라간다.
모든 그룹의 우승자가 정해지면 리그전이 끝난다.
개설할 때 예선을 고르면 그룹마다 3명씩 조를 짜 리그전(3판 2선)을 먼저 하고, 조 1·2위만 본선 토너먼트(5판 3선)에 오른다.

상태는 '탁우회 토너먼트' 파일의 두 시트에 둔다.
- '토너먼트로그': 참가·결과 보고·운영 기록을 한 줄씩 덧붙이기만 한다(append-only). 이것이 원본이다.
  덧붙이기는 동시에 여러 건이 와도 서로 덮어쓰지 않는다.
- '토너먼트': 방마다 한 줄. 운영 기록을 적용한 상태(JSON)를 저장해 두는 스냅숏이다 — 대진표, 확정 결과, 그룹 조정.
운영진 동작은 먼저 로그에 한 줄 덧붙이고(이 순간 확정) 그다음 스냅숏을 고친다. 운영진 두 명이 거의 동시에 저장하면
나중 스냅숏이 앞 스냅숏을 덮을 수 있지만, 스냅숏에 없는 운영 기록은 읽을 때 다시 적용하므로 변경이 사라지지 않는다.
"""
import copy
import hashlib
import json
import random
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

league_bp = Blueprint("league", __name__)

TOURNAMENT_SHEET = "토너먼트"
LOG_SHEET = "토너먼트로그"
TOURNAMENT_HEADERS = ["코드", "상태", "생성일시", "갱신일시", "상태JSON", "관리자코드"]
LOG_HEADERS = ["코드", "종류", "시각", "내용JSON"]

CACHE_TTL = 3            # 참가자 화면이 몇 초마다 새로 고쳐도 시트 호출은 이 간격으로만 나간다 (인스턴스마다)
WRITE_LIMIT = 240        # 10분당 쓰기 허용 횟수 (공개 주소이므로 최소한의 남용 방지)
CODE_ALPHABET = "0123456789"   # 참가 코드·관리자 코드 모두 숫자 6자리
ADMIN_LOGIN_LIMIT = 30         # 10분당 관리자 코드 입력 시도 허용 횟수
BEST_OF_FROM_CHOICES = (0, 2, 4, 8, 16, 32)   # 5판 3선으로 바꾸는 시점(남은 선수 수). 0은 전환 없음
STATUS_LABELS = {"lobby": "참가 접수 중", "draft": "대진 편성 중", "running": "진행 중", "finished": "종료"}
OPEN_STATUSES = ("lobby", "draft")   # 참가 접수가 열려 있는 상태 (편성 중에도 시작 전까지는 받는다)

# 기본 그룹 기준: 부수표의 상위부(1~4부) · 중위부(5~7부) · 하위부(8부~). 0부 이하도 상위부.
DEFAULT_GROUPS = [
    {"key": "upper", "name": "상위부", "min": -99, "max": 4},
    {"key": "middle", "name": "중위부", "min": 5, "max": 7},
    {"key": "lower", "name": "하위부", "min": 8, "max": 99},
]

# app.py가 시작할 때 채워 주는 의존성 (순환 import를 피하기 위해 함수로 받는다)
_deps = {}


def configure(**deps):
    _deps.update(deps)


def kst_now():
    return datetime.now(_deps["kst"]).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 저장소: 구글 시트 두 장
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_cache = {"rows": None, "logs": None, "expires_at": 0.0}
_writes = deque()
_admin_logins = deque()
_sheets_ready = False
RANGES = [f"'{TOURNAMENT_SHEET}'!A:F", f"'{LOG_SHEET}'!A:D"]
ROOM_LIST_LIMIT = 30      # 참가 화면에 보여 주는 방 수
FINISHED_ROOM_DAYS = 2    # 끝난 방은 이틀까지만 목록에 남긴다


class LeagueError(Exception):
    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra   # 응답에 함께 실을 값 (예: 다시 연결할 수 있음)


def _spreadsheet():
    return _deps["open_league_spreadsheet"]()


def _ensure_sheets():
    # 두 시트가 있는지는 인스턴스마다 한 번만 확인하고(메타데이터 호출 1회), 없으면 만든다
    global _sheets_ready
    if _sheets_ready:
        return
    spreadsheet = _spreadsheet()
    existing = {ws.title for ws in spreadsheet.worksheets()}
    for title, headers in ((TOURNAMENT_SHEET, TOURNAMENT_HEADERS), (LOG_SHEET, LOG_HEADERS)):
        if title not in existing:
            spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers)).append_row(headers)
    _sheets_ready = True


RETRY_STATUSES = (429, 500, 502, 503, 504)


def _retrying(call):
    # 구글 시트가 잠깐 거절하면(동시 쓰기가 몰렸거나 순간 한도) 한 번만 조금 기다렸다 다시 한다.
    # 로그 줄은 두 번 들어가도 괜찮다 — 운영 기록은 id로, 참가는 이름으로 한 번만 세고, 보고는 경기별 마지막 것만 쓴다.
    import gspread

    try:
        return call()
    except gspread.exceptions.APIError as e:
        if getattr(getattr(e, "response", None), "status_code", None) not in RETRY_STATUSES:
            raise
        time.sleep(1 + random.random())   # 동시에 실패한 요청들이 같은 순간에 다시 몰리지 않게
        return call()


def _read_all(force=False):
    # 두 시트를 API 호출 한 번(values_batch_get)으로 읽어 잠깐 캐시한다.
    # 구글 시트 읽기 한도가 사용자당 분당 60회라, 참가자 폰이 몇 초마다 새로 고쳐도 인스턴스당 호출은 CACHE_TTL마다 한 번이다.
    with _lock:
        now = time.monotonic()
        if force or now >= _cache["expires_at"] or _cache["rows"] is None:
            _ensure_sheets()
            ranges = _retrying(lambda: _spreadsheet().values_batch_get(RANGES)).get("valueRanges", [])
            _cache["rows"] = ranges[0].get("values", []) if len(ranges) > 0 else []
            _cache["logs"] = ranges[1].get("values", []) if len(ranges) > 1 else []
            _cache["expires_at"] = now + CACHE_TTL
        return _cache["rows"], _cache["logs"]


def _invalidate():
    with _lock:
        _cache["expires_at"] = 0.0


def _check_write_limit():
    now = time.monotonic()
    while _writes and now - _writes[0] > 600:
        _writes.popleft()
    if len(_writes) >= WRITE_LIMIT:
        raise LeagueError("요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.", 429)
    _writes.append(now)


def _find_row(rows, code):
    for i, row in enumerate(rows):
        if i == 0 or not row:
            continue
        if row[0].strip().upper() == code:
            return i + 1, row  # 시트 행 번호는 1부터
    return None, None


def load_state(code, force=False):
    # 방 스냅숏(JSON) + 로그(참가·보고·운영 기록)를 합쳐 하나의 상태로 돌려준다
    code = normalize_code(code)
    rows, logs = _read_all(force)
    row_no, row = _find_row(rows, code)
    if row is None or len(row) < 5 or not row[4]:
        raise LeagueError("그 코드의 토너먼트가 없습니다.", 404)
    state = json.loads(row[4])
    state["_row_no"] = row_no   # 저장할 때 행을 다시 찾지 않도록 기억해 둔다
    return assemble(state, [l for l in logs[1:] if l and l[0].strip().upper() == code])


_WRITE_PARAMS = {"valueInputOption": "RAW"}


def _row_values(state):
    return [state["code"], state["status"], state["created_at"], state["updated_at"],
            json.dumps(persistable(state), ensure_ascii=False), state.get("admin_code", "")]


def save_state(state):
    # 새 방의 첫 스냅숏을 덧붙인다 (그 뒤의 저장은 운영 기록과 함께 commit_op가 한다)
    state["updated_at"] = kst_now()
    _ensure_sheets()
    _spreadsheet().values_append(RANGES[0], {**_WRITE_PARAMS, "insertDataOption": "INSERT_ROWS"}, {"values": [_row_values(state)]})
    _invalidate()


def commit_op(state, op, joins=()):
    # 운영 기록(과 함께 들어갈 참가 줄)을 로그에 덧붙이는 순간 확정된다. 덧붙이기는 values.append + INSERT_ROWS라
    # 동시에 여러 건이 와도 건마다 새 행이 끼워져 서로 덮어쓰지 않는다. (batchUpdate의 appendCells는 동시에 오면
    # 같은 행에 써서 한쪽이 사라진다 — 프리뷰에서 8건 동시에 보내 확인했다.)
    # 스냅숏은 그다음에 고치는 사본이다. 저장이 실패하거나 다른 운영진의 저장에 덮여도 기록은 다음에 읽을 때 다시 적용된다.
    # 스냅숏은 행 번호로 고친다 (읽을 때 기억해 둔 것 — 방 행은 지우지 않으므로 밀리지 않는다).
    append_logs(state, [("참가", j) for j in joins] + [("운영", op)])
    state["applied"].append(op["id"])
    state["updated_at"] = kst_now()
    row_no = state["_row_no"]
    try:
        _spreadsheet().values_update(f"'{TOURNAMENT_SHEET}'!A{row_no}:F{row_no}", _WRITE_PARAMS, {"values": [_row_values(state)]})
    except Exception as e:
        _deps["logger"].warning("리그전 스냅숏 저장 실패(운영 기록은 로그에 남음): %s", e)
    _invalidate()


def append_logs(state, entries):
    # entries: [(종류, 내용)]. 여러 줄도 호출 한 번으로 덧붙인다 — 한 번의 호출은 통째로 들어가거나 통째로 실패한다.
    _ensure_sheets()
    now = kst_now()
    values = [[state["code"], kind, now, json.dumps(p, ensure_ascii=False)] for kind, p in entries]
    _retrying(lambda: _spreadsheet().values_append(RANGES[1], {**_WRITE_PARAMS, "insertDataOption": "INSERT_ROWS"}, {"values": values}))
    _invalidate()
    state["rev"] = state.get("rev", 0) + len(entries)
    return now


def delete_tournament(state):
    # 행을 지우면 아래 방들의 행 번호가 밀려 다른 방의 저장이 엉뚱한 행을 덮을 수 있다. 행은 두고 내용만 비운다.
    # 코드는 남겨 두어 새 방이 같은 코드를 받지 않게 한다.
    row_no = state["_row_no"]
    _spreadsheet().values_update(f"'{TOURNAMENT_SHEET}'!A{row_no}:F{row_no}", _WRITE_PARAMS,
                                 {"values": [[state["code"], "삭제", state["created_at"], kst_now(), "", ""]]})
    _invalidate()


# ---------------------------------------------------------------------------
# 상태 조립
# ---------------------------------------------------------------------------
PERSIST_KEYS = ("code", "name", "status", "created_at", "updated_at", "started_at", "finished_at", "admin_key", "admin_code",
                "format", "groups", "seed", "group_overrides", "removed", "brackets", "applied")


def persistable(state):
    return {k: state[k] for k in PERSIST_KEYS if k in state}


def normalize_code(code):
    return re.sub(r"[^0-9]", "", str(code or ""))[:6]


def new_code(existing):
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        if code not in existing:
            return code


def match_best_of(fmt, players_in_round):
    # 예선이 있으면 본선은 모두 5판 3선이다(예선 경기는 3판 2선으로 따로 정한다).
    # 없으면 초반은 3판 2선, 정해 둔 라운드(남은 선수 수 기준)부터 5판 3선
    if fmt.get("prelims"):
        return 5
    if fmt["best_of"] == 5 or not fmt.get("best_of_from"):
        return fmt["best_of"]
    return 5 if players_in_round <= fmt["best_of_from"] else 3


def group_for(division, groups):
    # 부수 숫자를 그룹 기준에 맞춰 배정한다. 부수가 없으면 배정 보류(운영진이 정한다) — 임시로 마지막 그룹.
    number = _deps["division_number"](division or "")
    if number is None:
        return groups[-1]["key"], False
    for g in groups:
        if g["min"] <= number <= g["max"]:
            return g["key"], True
    return groups[-1]["key"], False


def assemble(state, logs):
    # 로그를 차례로 읽는다: 참가 → 참가자 목록, 보고 → 경기별 최신 보고, 운영 → 스냅숏에 아직 없는 운영 기록을 다시 적용
    joins, reports, ops = [], {}, []
    for _, kind, at, payload in (l[:4] for l in logs if len(l) >= 4):
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        if kind == "참가":
            joins.append({**data, "at": at})
        elif kind == "보고":
            reports[data.get("match")] = {**data, "at": at}
        elif kind == "운영" and data.get("id"):
            ops.append(data)
    for key, empty in (("removed", []), ("group_overrides", {}), ("brackets", {}), ("applied", [])):
        state.setdefault(key, empty)
    state["_joins"], state["_reports"] = joins, reports
    state["rev"] = len(logs)   # 로그 줄 수. 화면은 이보다 작은 값의 응답(다른 인스턴스의 오래된 캐시)을 무시한다
    compute_participants(state)
    place_all(state)

    # 운영진 두 명이 거의 동시에 저장하면 나중 스냅숏이 앞 스냅숏을 덮는다. 덮인 쪽의 기록은 로그에 남아 있으므로
    # 스냅숏의 applied에 없는 기록을 로그 순서대로 다시 적용해 되살린다. 지금 상태와 맞지 않는 기록(같은 경기를 둘이 확정 등)은 건너뛴다.
    applied = set(state["applied"])
    for op in ops:
        if op["id"] in applied:
            continue
        backup = copy.deepcopy({k: v for k, v in state.items() if k != "_joins"})
        try:
            apply_op(state, op)
        except Exception as e:  # 적용하다 멈춘 기록이 상태를 반쯤 바꿔 두지 않게 되돌린다
            state.clear()
            state.update(backup, _joins=joins)
            if not isinstance(e, LeagueError):
                _deps["logger"].exception("리그전 운영 기록 적용 실패: %s", op)
        state["applied"].append(op["id"])
        applied.add(op["id"])
    refresh_derived(state)
    return state


def token_tag(token):
    # 토큰의 지문(해시 앞 8자). 화면이 자기 토큰과 비교해 '이 기기의 연결이 끊겼는지' 알 수 있게 공개한다 (토큰은 알아낼 수 없다).
    return hashlib.sha256(token.encode()).hexdigest()[:8] if token else ""


def compute_participants(state):
    # 참가 로그 → 참가자 목록 (같은 이름은 처음 한 번만). 제외한 이름은 뺀다.
    # 같은 이름의 뒤 줄: 운영진이 넣어 둔 이름을 본인이 참가하면 토큰을 잇고, '다시 연결'이면 토큰을 바꾼다(예전 기기는 끊긴다).
    participants, tokens, by_name = [], {}, {}
    removed = set(state["removed"])
    for data in state["_joins"]:
        name = data.get("name", "")
        if not name or name in removed:
            continue
        if name not in tokens:
            tokens[name] = data.get("token", "")
            by_name[name] = {"name": name, "division": data.get("division", ""), "joined_at": data["at"],
                             "added_by_admin": data.get("by") == "운영진", "tag": token_tag(tokens[name])}
            participants.append(by_name[name])
        elif data.get("token") and (not tokens[name] or data.get("reconnect")):
            tokens[name] = data["token"]
            p = by_name[name]
            p["added_by_admin"] = False
            p["tag"] = token_tag(data["token"])
            if data.get("reconnect"):
                p["reconnected_at"] = data["at"]
    state["participants"] = participants
    state["_tokens"] = tokens
    apply_groups(state)


def refresh_derived(state):
    # 저장하지 않고 매번 계산하는 값: 경기별 선수 보고, 대진표에 자리가 없는 참가자
    for bracket in state["brackets"].values():
        prelim = bracket.get("prelim")
        for match in _all_matches(bracket) + (prelim["matches"] if prelim else []):
            report = state["_reports"].get(match["id"])
            # 보고는 그 경기가 결과를 기다리고, 보고한 대진(두 선수)이 지금 대진과 같을 때만 보인다.
            # 대진을 다시 짜거나 앞 경기를 되돌려 선수가 바뀌면 예전 보고는 숨는다.
            usable = (report and match["status"] == "pending" and report.get("winner") in match["players"]
                      and report.get("players", match["players"]) == match["players"])
            match["report"] = {k: report.get(k) for k in ("winner", "games", "by", "at", "record")} if usable else None
        if prelim:
            for pool in prelim["pools"]:
                describe_pool(bracket, pool)
    state["unplaced"] = unplaced_players(state)


def unplaced_players(state):
    # 시작한 뒤 참가자인데 그룹 대진표에 자리가 없는 사람. 시작하는 순간 들어온 참가나 예전 버전에서 동시에 추가된 사람이 여기에 남는다.
    if state["status"] == "lobby":
        return {}
    placed = {}
    for key, b in state["brackets"].items():
        names = {p for m in b["matches"] if m["round"] == 1 for p in m["players"] if p}
        if b.get("prelim"):
            names |= {n for pool in b["prelim"]["pools"] for n in pool["players"]}
        if not b["matches"] and b.get("champion"):
            names.add(b["champion"])   # 혼자인 그룹
        placed[key] = names
    result = {}
    for p in state["participants"]:
        if p["name"] not in placed.get(p["group"], ()):
            result.setdefault(p["group"], []).append(p["name"])
    return result


def _all_matches(bracket):
    return bracket["matches"] + ([bracket["third"]] if bracket.get("third") else [])


def apply_groups(state):
    # 참가자마다 그룹을 정한다 (운영진이 바꾼 것이 있으면 그것을 우선)
    for p in state["participants"]:
        auto_group, decided = group_for(p["division"], state["groups"])
        override = state.get("group_overrides", {}).get(p["name"])
        p["group"] = override or auto_group
        p["group_undecided"] = not decided and not override


def list_rooms():
    # 참가 화면의 방 목록: 이름·상태·참가 인원만 내보낸다. 코드는 내보내지 않는다 — 접수 중인 방의 코드는 입장 암호다.
    rows, logs = _read_all()
    by_code = {}
    for l in logs[1:]:
        if l:
            by_code.setdefault(l[0].strip(), []).append(l)
    rooms = []
    for row in rows[1:]:
        if len(row) < 5 or not row[4]:
            continue
        try:
            # 상태·인원은 로그까지 합친 상태로 센다 (스냅숏이 덮였어도 종료 여부가 맞게)
            state = assemble(json.loads(row[4]), by_code.get(row[0].strip(), []))
        except (ValueError, KeyError, TypeError):
            continue
        if state.get("status") == "finished":
            try:
                finished = datetime.strptime(state.get("finished_at", state["updated_at"]), "%Y-%m-%d %H:%M:%S")
                if (datetime.now(_deps["kst"]).replace(tzinfo=None) - finished).days >= FINISHED_ROOM_DAYS:
                    continue
            except (ValueError, KeyError):
                continue
        rooms.append({
            "name": state["name"], "status": state["status"], "status_label": STATUS_LABELS.get(state["status"], state["status"]),
            "participants": len(state["participants"]), "created_at": state["created_at"][:16],
            "groups": [g["name"] for g in state.get("groups", [])],
            # 코드는 어떤 상태에서도 내보내지 않는다. 대진표(접수 중이면 참가자 목록)는 보기 전용 키로 연다.
            "view": view_key(state) if state.get("admin_key") else None,
        })
    rooms.sort(key=lambda r: r["created_at"], reverse=True)
    return rooms[:ROOM_LIST_LIMIT]


def view_key(state):
    # 대진표 '보기 전용' 주소용 키. 운영 키에서 한 방향으로 만들어 저장이 필요 없고, 이 키로는 참가 코드를 알 수 없다.
    return hashlib.sha256(f"view:{state['admin_key']}".encode()).hexdigest()[:12]


def load_state_by_view(view, force=False):
    # 보기 전용 키로 방을 찾는다 (방 수만큼 훑는다; 방은 많아야 수십 개)
    view = str(view or "").strip().lower()
    rows, _ = _read_all(force)
    for row in rows[1:]:
        if len(row) < 5 or not row[4]:
            continue
        try:
            raw = json.loads(row[4])
        except ValueError:
            continue
        if raw.get("admin_key") and view_key(raw) == view:
            return load_state(raw["code"], force)
    raise LeagueError("그 대진표를 찾을 수 없습니다.", 404)


def viewer_view(state):
    # 보기 전용 화면에는 참가 코드도 보내지 않는다 (코드는 입장 암호다)
    view = public_view(state)
    view.pop("code", None)
    return view


def public_view(state, admin=False):
    # 참가자 화면에 보내는 상태: 운영 키·참가 토큰·내부 값(밑줄로 시작, 적용한 운영 기록 목록)은 뺀다.
    # 편성 중인 대진표는 운영진에게만 보낸다 — 참가자는 시작한 뒤에 본다.
    view = {k: v for k, v in state.items() if not k.startswith("_") and k not in ("admin_key", "admin_code", "applied")}
    view["status_label"] = STATUS_LABELS.get(state["status"], state["status"])
    if state["status"] == "draft" and not admin:
        view["brackets"], view["unplaced"] = {}, {}
    return view


# ---------------------------------------------------------------------------
# 대진표
# ---------------------------------------------------------------------------
def seed_order(size):
    # 표준 토너먼트 배치: 1번 시드와 마지막 시드가 만나고, 상위 시드끼리는 결승 전까지 만나지 않는다
    order = [1]
    while len(order) < size:
        n = len(order) * 2
        order = [x for s in order for x in (s, n + 1 - s)]
    return order


def round_label(entrants, round_no, rounds):
    # entrants: 그 라운드에 들어오는 인원(노드) 수. 10명이면 10강 → 5강 → 준결승 → 결승
    if round_no == rounds:
        return "결승"
    if round_no == rounds - 1:
        return "준결승"
    return f"{entrants}강"


def level_entrants(bracket, round_no):
    # 예전 형식(2의 제곱 대진표)으로 저장된 방도 읽을 수 있게 한다
    entrants = bracket.get("entrants") or {}
    return entrants.get(str(round_no), bracket["size"] >> (round_no - 1))


def build_bracket(group_key, names, seed_mode, divisions, fmt, rng=None):
    # names: 그룹 참가자 이름 (seed_mode가 'division'이면 부수 순, 아니면 rng로 섞는다 — 같은 씨앗이면 어디서 만들어도 같은 대진)
    # 짝짓기는 아래에서 위로: 1라운드는 모두 짝을 이루고(홀수면 한 명만 부전승), 이후 라운드도 이웃끼리 짝을 짓되
    # 홀수가 남으면 끝의 한 노드가 부전승으로 올라간다. 부전승 자리는 홀수 라운드마다 오른쪽·왼쪽을 번갈아
    # 같은 사람이 연달아 부전승을 받지 않게 한다. (10명: 5경기 → 2경기+부전승 → 1경기+부전승 → 결승)
    names = list(names)
    if seed_mode == "division":
        names = sort_by_division(names, divisions)
    else:
        (rng or random).shuffle(names)

    n = len(names)
    if n == 1:
        return build_from_first(group_key, [[names[0], None]], fmt)

    if seed_mode == "division":
        # 상위-하위 순으로 짝을 짓고(1-끝, 2-끝-1 …), 상위 짝끼리는 멀리 떨어뜨린다. 홀수면 최상위가 1라운드 부전승.
        bye_player = names[0] if n % 2 else None
        rest = names[1:] if n % 2 else names
        pair_count = len(rest) // 2
        pairs = [[rest[i], rest[len(rest) - 1 - i]] for i in range(pair_count)]
        size = 1
        while size < pair_count:
            size *= 2
        first = [pairs[seed - 1] for seed in seed_order(size) if seed <= pair_count]
        if bye_player:
            first.append([bye_player, None])
    else:
        first = [[names[i], names[i + 1] if i + 1 < n else None] for i in range(0, n, 2)]
    return build_from_first(group_key, first, fmt)


def sort_by_division(names, divisions):
    # 부수가 높은(숫자가 작은) 사람부터, 부수가 없는 사람은 뒤로
    number = _deps["division_number"]
    return sorted(names, key=lambda n: (number(divisions.get(n, "")) is None, number(divisions.get(n, "")) or 0, n))


def build_from_first(group_key, first, fmt):
    # 1라운드 배치([[선수, 선수 또는 None], ...])에서 위 라운드를 아래에서 위로 짝지어 대진표를 만든다
    n = sum(1 for pair in first for p in pair if p)
    if n == 1:
        return {"size": 1, "rounds": 0, "matches": [], "champion": next(p for pair in first for p in pair if p), "labels": {},
                "entrants": {}, "third": None, "third_winner": None, "third_possible": False}

    def node(rnd, idx, players, bye=False):
        m = {"id": f"{group_key}-{rnd}-{idx}", "round": rnd, "index": idx, "players": players,
             "winner": None, "games": None, "status": "waiting", "best_of": None, "next": None, "slot": 0}
        if bye:
            m["bye"] = True
        return m

    level = [node(1, i, players) for i, players in enumerate(first)]
    matches = list(level)
    entrants = {"1": n}
    bye_right = n % 2 == 0   # 1라운드에서 이미 오른쪽 끝이 부전승이었으면 다음 홀수 라운드는 왼쪽부터
    rnd = 1
    while len(level) > 1:
        rnd += 1
        count = len(level)
        entrants[str(rnd)] = count
        bye_index = None
        if count % 2 == 1:
            bye_index = count - 1 if bye_right else 0
            bye_right = not bye_right
        following, idx, j = [], 0, 0
        while j < count:
            if j == bye_index:
                m = node(rnd, idx, [None, None], bye=True)
                level[j]["next"], level[j]["slot"] = m["id"], 0
                j += 1
            else:
                m = node(rnd, idx, [None, None])
                level[j]["next"], level[j]["slot"] = m["id"], 0
                level[j + 1]["next"], level[j + 1]["slot"] = m["id"], 1
                j += 2
            following.append(m)
            idx += 1
        matches.extend(following)
        level = following
    rounds = rnd
    for m in matches:
        m["best_of"] = match_best_of(fmt, entrants[str(m["round"])])
    bracket = {"size": n, "rounds": rounds, "matches": matches, "champion": None, "third": None, "third_winner": None,
               "entrants": entrants, "labels": {str(r): round_label(entrants[str(r)], r, rounds) for r in range(1, rounds + 1)}}
    reflow_bracket(bracket)
    return bracket


def reflow_bracket(bracket):
    # 1라운드 배치를 바탕으로 부전승과 다음 라운드를 다시 계산한다 (결과가 하나도 없을 때만 쓴다)
    for m in bracket["matches"]:
        if m["round"] > 1:
            m["players"] = [None, None]
        m.update({"winner": None, "games": None, "status": "waiting"})
    bracket["champion"] = None
    bracket["third_winner"] = None
    if bracket.get("third"):
        bracket["third"].update({"players": [None, None], "winner": None, "games": None, "status": "waiting"})
    by_id = {m["id"]: m for m in bracket["matches"]}
    for m in [m for m in bracket["matches"] if m["round"] == 1]:
        present = [p for p in m["players"] if p]
        if len(present) == 2:
            m["status"] = "pending"
        elif len(present) == 1:
            _advance(bracket, by_id, m, present[0], status="bye")
    bracket["third_possible"] = bracket["rounds"] >= 2 and all(f is not None for f in third_feeders(bracket))
    if bracket.get("third") and not bracket["third_possible"]:
        bracket["third"] = None
        bracket["third_winner"] = None


def group_has_results(bracket):
    prelim = bracket.get("prelim")
    return any(m["status"] == "confirmed" for m in _all_matches(bracket) + (prelim["matches"] if prelim else []))


def rebuild_group(state, key, seed=None, rng=None):
    # 참가자가 바뀌었거나 다시 섞을 때 그 그룹의 대진표를 새로 만든다 (결과가 있으면 부르지 않는다)
    names = [p["name"] for p in state["participants"] if p["group"] == key]
    divisions = {p["name"]: p["division"] for p in state["participants"]}
    third_on = bool(state["brackets"].get(key, {}).get("third"))
    if names:
        state["brackets"][key] = build_group(key, names, seed or state["seed"], divisions, state["format"], rng)
        _keep_third(state, key, third_on)
    else:
        state["brackets"].pop(key, None)


def _keep_third(state, key, third_on):
    # 다시 만든 대진표에도 3·4위전을 이어 둔다 (새 대진에서 둘 수 없으면 뺀다)
    bracket = state["brackets"][key]
    if third_on and bracket["rounds"] >= 2 and bracket.get("third_possible"):
        set_third_place(bracket, key, state["format"], True)


def round1_layout(bracket):
    # 1라운드 자리 배치: [[선수, 선수 또는 None], ...]. 혼자인 그룹은 [[그 사람, None]].
    if not bracket["matches"]:
        return [[bracket["champion"], None]] if bracket.get("champion") else []
    return [list(m["players"]) for m in sorted((m for m in bracket["matches"] if m["round"] == 1), key=lambda m: m["index"])]


def place_players(state, key):
    # 편성 중: 그룹 참가자와 대진표를 맞춘다. 대진표에 없는 사람은 첫 빈 자리(부전승 자리)에, 빈 자리가 없으면 맨 끝에 넣고,
    # 그룹을 떠난 사람은 뺀다. 운영진이 옮겨 둔 나머지 자리는 그대로 둔다. 상태가 같으면 어느 인스턴스에서 해도 결과가 같다.
    names = [p["name"] for p in state["participants"] if p["group"] == key]
    bracket = state["brackets"].get(key)
    if not names:
        state["brackets"].pop(key, None)
        return
    if wants_prelim(state["format"], len(names)) or (bracket and bracket.get("prelim")):
        place_in_pools(state, key, names, bracket)
        return
    before = round1_layout(bracket) if bracket else []
    wanted = set(names)
    layout = [pair for pair in ([p if p in wanted else None for p in pair] for pair in before) if any(pair)]
    placed = {p for pair in layout for p in pair if p}
    for name in names:
        if name in placed:
            continue
        empty = next(((i, j) for i, pair in enumerate(layout) for j in (0, 1) if pair[j] is None), None)
        if empty:
            layout[empty[0]][empty[1]] = name
        else:
            layout.append([name, None])
    if layout == before:
        return
    state["brackets"][key] = build_from_first(key, layout, state["format"])
    _keep_third(state, key, bool(bracket and bracket.get("third")))


def place_all(state):
    # 편성 중이면 모든 그룹에서 대진표 밖 참가자를 자리에 넣는다 (참가는 로그에만 들어오므로 읽을 때마다 맞춘다)
    if state["status"] == "draft":
        for g in state["groups"]:
            place_players(state, g["key"])


def swap_slots(bracket, a, b):
    # 1라운드에서 두 선수의 자리를 바꾼다
    slots = {}
    for m in bracket["matches"]:
        if m["round"] == 1:
            for i, p in enumerate(m["players"]):
                if p:
                    slots[p] = (m, i)
    if a not in slots or b not in slots:
        raise LeagueError("두 선수 모두 이 그룹 1라운드에 있어야 합니다.")
    (ma, ia), (mb, ib) = slots[a], slots[b]
    ma["players"][ia], mb["players"][ib] = b, a
    reflow_bracket(bracket)


def move_player(bracket, name, match_id, slot):
    # 1라운드의 특정 자리로 선수를 옮긴다. 그 자리에 다른 선수가 있으면 두 사람의 자리가 바뀐다.
    targets = {m["id"]: m for m in bracket["matches"] if m["round"] == 1}
    target = targets.get(match_id)
    if target is None or slot not in (0, 1):
        raise LeagueError("1라운드 자리로만 옮길 수 있습니다.")
    source = next(((m, i) for m in targets.values() for i, p in enumerate(m["players"]) if p == name), None)
    if source is None:
        raise LeagueError("이 그룹 1라운드에 없는 선수입니다.")
    src_match, src_slot = source
    if src_match is target and src_slot == slot:
        return
    other = target["players"][slot]
    target["players"][slot], src_match["players"][src_slot] = name, other
    if any(not any(m["players"]) for m in targets.values()):
        # 아무도 없는 대진이 생기면 그 자리의 상대가 영영 기다리게 되므로 되돌리고 거절한다
        target["players"][slot], src_match["players"][src_slot] = other, name
        raise LeagueError("그 자리로 옮기면 빈 대진이 생깁니다. 다른 선수와 자리를 바꿔 주세요.")
    reflow_bracket(bracket)


def _advance(bracket, by_id, match, winner, status="confirmed"):
    match["winner"] = winner
    match["status"] = status
    if match.get("third"):
        bracket["third_winner"] = winner
    elif match["next"]:
        nxt = by_id[match["next"]]
        nxt["players"][match["slot"]] = winner
        if nxt.get("bye"):
            _advance(bracket, by_id, nxt, winner, status="bye")   # 부전승 노드: 바로 그다음 경기로
            return
        if all(nxt["players"]):
            nxt["status"] = "pending"
    else:
        bracket["champion"] = winner
    sync_third(bracket)


def _entries(m):
    # 1라운드 경기에 들어올 자리: 예선이 있는 본선은 'A:1'(A조 1위) 같은 자리표, 없으면 선수 이름
    return [s for s in (m.get("seats") or m["players"]) if s]


def _is_bye_node(m):
    return bool(m.get("bye")) or (m["round"] == 1 and len(_entries(m)) < 2)


def third_feeders(bracket):
    # 결승 두 자리로 이어지는 길에서 마지막 '실제 경기'(부전승 노드 제외). 그 길에 경기가 없으면 None.
    matches = bracket["matches"]
    final = next((m for m in matches if m["round"] == bracket["rounds"]), None)
    if final is None:
        return [None, None]
    feeders = []
    for slot in (0, 1):
        m = next((c for c in matches if c["next"] == final["id"] and c["slot"] == slot), None)
        while m is not None and _is_bye_node(m):
            m = next((c for c in matches if c["next"] == m["id"]), None)
        feeders.append(m)
    return feeders


def sync_third(bracket):
    # 3·4위전이 켜져 있으면 결승 진출자에게 마지막으로 진 두 사람을 채운다.
    third = bracket.get("third")
    if not third or third["status"] in ("confirmed", "bye") or bracket["rounds"] < 2:
        return
    feeders = third_feeders(bracket)
    for slot, f in enumerate(feeders):
        third["players"][slot] = ([p for p in f["players"] if p and p != f["winner"]] or [None])[0] if f and f["status"] == "confirmed" else None
    present = [p for p in third["players"] if p]
    if len(present) == 2:
        third["status"] = "pending"
    elif len(present) == 1 and all(f is None or f["status"] == "confirmed" for f in feeders):
        third["winner"], third["status"], bracket["third_winner"] = present[0], "bye", present[0]
    else:
        third["status"] = "waiting"


def set_third_place(bracket, group_key, fmt, enabled):
    if enabled:
        if bracket["rounds"] < 2:
            raise LeagueError("준결승이 없는 그룹에는 3·4위전을 둘 수 없습니다.")
        if not bracket.get("third_possible", True):
            raise LeagueError("결승 진출자 한쪽이 경기 없이 올라오는 대진이라 3·4위전을 둘 수 없습니다.")
        if not bracket.get("third"):
            bracket["third"] = {"id": f"{group_key}-3rd", "round": bracket["rounds"], "index": 0, "players": [None, None],
                                "winner": None, "games": None, "status": "waiting", "best_of": match_best_of(fmt, 2),
                                "next": None, "slot": 0, "third": True}
            sync_third(bracket)
    elif bracket.get("third"):
        if bracket["third"]["status"] == "confirmed":
            raise LeagueError("3·4위전 결과가 이미 확정되어 취소할 수 없습니다. 먼저 되돌리세요.")
        bracket["third"] = None
        bracket["third_winner"] = None


def group_done(bracket):
    return bool(bracket["champion"]) and (not bracket.get("third") or bracket["third"]["status"] in ("confirmed", "bye"))


def refresh_status(state):
    # 모든 그룹의 우승(과 켜 둔 3·4위전)이 끝나면 종료, 아니면 진행 중
    if state["status"] not in ("running", "finished"):
        return
    if all(group_done(b) for b in state["brackets"].values()):
        if state["status"] != "finished":
            state["finished_at"] = kst_now()
        state["status"] = "finished"
    else:
        state["status"] = "running"
        state.pop("finished_at", None)


def find_match(state, match_id):
    for bracket in state.get("brackets", {}).values():
        prelim = bracket.get("prelim")
        for match in _all_matches(bracket) + (prelim["matches"] if prelim else []):
            if match["id"] == match_id:
                return bracket, match
    raise LeagueError("그 경기를 찾을 수 없습니다.", 404)


def validate_games(games, target, best_of):
    # 점수판이 보낸 게임 점수 검증: (오류, [(a,b)...], 승자 인덱스)
    if games in (None, "", []):
        return None, None, None
    try:
        parsed = [(int(g[0]), int(g[1])) for g in games]
    except (TypeError, ValueError, IndexError):
        return "게임 점수 형식이 올바르지 않습니다.", None, None
    needed = best_of // 2 + 1
    if not 1 <= len(parsed) <= best_of:
        return "게임 수가 올바르지 않습니다.", None, None
    wins = [0, 0]
    for a, b in parsed:
        high, low = max(a, b), min(a, b)
        if not (0 <= low < high <= 99 and high >= target and high - low >= 2):
            return f"완료되지 않은 게임 점수가 있습니다. ({a}:{b})", None, None
        wins[0 if a > b else 1] += 1
    if max(wins) != needed or min(wins) >= needed:
        return "승부가 확정되지 않은 점수입니다.", None, None
    return None, parsed, 0 if wins[0] > wins[1] else 1


def confirm_match(state, match_id, winner, games=None):
    bracket, match = find_match(state, match_id)
    if match["status"] == "confirmed":
        raise LeagueError("이미 확정된 경기입니다. 되돌린 뒤 다시 확정하세요.")
    if not all(match["players"]):
        raise LeagueError("두 선수가 모두 정해진 뒤에 확정할 수 있습니다.")
    if winner not in match["players"]:
        raise LeagueError("승자는 이 경기의 두 선수 중 하나여야 합니다.")
    error, parsed, winner_index = validate_games(games, state["format"]["target"], match["best_of"])
    if error:
        raise LeagueError(error)
    if parsed is not None and match["players"][winner_index] != winner:
        raise LeagueError("게임 점수와 승자가 맞지 않습니다.")

    match["games"] = parsed
    match["report"] = None
    if match.get("prelim"):
        # 예선 경기: 조의 경기가 모두 끝나 순위가 정해지면 1·2위가 본선 자리에 들어간다
        match["winner"], match["status"] = winner, "confirmed"
        sync_pools(bracket)
    else:
        _advance(bracket, {m["id"]: m for m in bracket["matches"]}, match, winner)
    refresh_status(state)
    return match, parsed


def reset_match(state, match_id):
    bracket, match = find_match(state, match_id)
    if match["status"] != "confirmed":
        raise LeagueError("확정된 경기만 되돌릴 수 있습니다.")
    if match.get("prelim"):
        # 예선 경기를 되돌리면 그 조는 다시 진행 중이 되어 본선 자리에서 빠진다 — 그 자리의 본선 경기가 끝났으면 안 된다
        pool = next(p for p in bracket["prelim"]["pools"] if p["label"] == match["pool"])
        if any(_unseat_blocked(bracket, seat) for seat in pool_seats(pool)):
            raise LeagueError("이 조에서 오른 선수의 본선 경기가 이미 끝나 되돌릴 수 없습니다. 본선 경기부터 되돌리세요.")
        match.update({"winner": None, "games": None, "status": "pending"})
        pool["order"] = None   # 결과가 바뀌면 운영진이 정한 동률 순서도 다시 정한다
        sync_pools(bracket)
        refresh_status(state)
        return match
    by_id = {m["id"]: m for m in bracket["matches"]}
    third = bracket.get("third")
    if match.get("third"):
        bracket["third_winner"] = None
    elif match["next"]:
        # 부전승 노드를 건너 실제 다음 경기를 찾는다
        real = by_id[match["next"]]
        while real is not None and real.get("bye"):
            real = by_id[real["next"]] if real["next"] else None
        if real is not None and real["status"] == "confirmed":
            raise LeagueError("다음 경기가 이미 끝나 되돌릴 수 없습니다. 다음 경기부터 되돌리세요.")
        if third and third["status"] in ("confirmed", "bye") and any(f is not None and f["id"] == match["id"] for f in third_feeders(bracket)):
            raise LeagueError("3·4위전이 이미 끝나 되돌릴 수 없습니다. 3·4위전부터 되돌리세요.")
        _retract(bracket, by_id, match)
    else:
        bracket["champion"] = None
    match.update({"winner": None, "games": None, "status": "pending"})
    sync_third(bracket)
    refresh_status(state)
    return match


def _retract(bracket, by_id, match):
    # match의 승자가 올라간 자리를 비운다. 부전승 노드를 거쳤으면 그 노드들도 함께 비운다.
    nxt = by_id[match["next"]]
    nxt["players"][match["slot"]] = None
    if nxt.get("bye"):
        if nxt["next"]:
            _retract(bracket, by_id, nxt)
        else:
            bracket["champion"] = None
        nxt.update({"winner": None, "status": "waiting"})
    else:
        nxt["status"] = "waiting"


# ---------------------------------------------------------------------------
# 예선: 그룹마다 3명씩 조를 짜 리그전(조 안의 모든 대진, 3판 2선)을 하고, 조 1·2위가 본선 토너먼트(5판 3선)에 오른다.
# 3으로 나누고 남는 2명은 2명 조(한 경기로 1·2위만 정하고 둘 다 본선), 남는 1명은 경기 없이 본선 직행(그 조 1위로 친다).
# 본선 1라운드는 'A:1'(A조 1위) 같은 자리표로 미리 짜 두고, 조 순위가 정해지면 그 자리에 이름을 넣는다.
# 그래서 먼저 끝난 조끼리는 다른 조를 기다리지 않고 본선 경기를 시작할 수 있다.
# ---------------------------------------------------------------------------
POOL_SIZE = 3


def wants_prelim(fmt, count):
    # 예선을 고른 토너먼트라도 그룹 인원이 2명 이하면 예선 없이 바로 본선(결승)이다
    return bool(fmt.get("prelims")) and count >= POOL_SIZE


def pool_label(i):
    return chr(65 + i) if i < 26 else str(i + 1)


def seat_label(label, rank):
    return f"{label}:{rank}"


def pool_seats(pool):
    # 본선에 오르는 자리: 2명 이상인 조는 1·2위, 혼자인 조(본선 직행)는 1위만
    return [seat_label(pool["label"], 1)] + ([seat_label(pool["label"], 2)] if len(pool["players"]) >= 2 else [])


def make_pools(names, seed_mode, divisions, rng=None):
    # 부수 순이면 강한 사람부터 조마다 한 명씩 뱀 모양(A→C, C→A, A→C)으로 나눠 조끼리 고르게, 아니면 섞어서 3명씩 자른다.
    # 3으로 나누고 남는 사람(가장 약한 시드 또는 섞인 순서의 끝)이 마지막 조(2명 조 또는 본선 직행 1명)가 된다.
    names = sort_by_division(names, divisions) if seed_mode == "division" else list(names)
    if seed_mode != "division":
        (rng or random).shuffle(names)
    count, rest = divmod(len(names), POOL_SIZE)
    pools = [[] for _ in range(count)]
    for i, name in enumerate(names[:count * POOL_SIZE]):
        turn, pos = divmod(i, count)
        if seed_mode == "division":
            pools[pos if turn % 2 == 0 else count - 1 - pos].append(name)
        else:
            pools[i // POOL_SIZE].append(name)
    if rest:
        pools.append(names[count * POOL_SIZE:])
    return [{"label": pool_label(i), "players": p, "order": None} for i, p in enumerate(pools)]


def pool_matches(group_key, pool):
    # 조 안의 모든 대진: 3명이면 3경기(1-3, 2-3, 1-2 — 상위 시드끼리는 마지막에), 2명이면 1경기, 혼자면 없음. 예선은 늘 3판 2선.
    p = pool["players"]
    pairs = [(p[0], p[2]), (p[1], p[2]), (p[0], p[1])] if len(p) == 3 else [(p[0], p[1])] if len(p) == 2 else []
    return [{"id": f"{group_key}-p{pool['label']}-{i + 1}", "pool": pool["label"], "round": 0, "index": i, "players": list(pair),
             "winner": None, "games": None, "status": "pending", "best_of": 3, "next": None, "slot": 0, "prelim": True}
            for i, pair in enumerate(pairs)]


def _ratio(won, lost):
    return won / lost if lost else (float("inf") if won else 0.0)


def pool_ranking(pool, matches):
    # 탁구 규정대로 순위를 가린다: 승수 → 동률인 사람끼리의 승수 → 그 사람끼리의 게임 비율 → 점수 비율.
    # 한 단계에서 일부만 갈리면 남은 동률자끼리 처음 단계부터 다시 본다. 끝까지 같거나 게임 점수 없이 승자만 넣은 경기가 있어
    # 가릴 수 없으면 그 사람들은 한 묶음(동률)으로 남는다 — 운영진이 정한 순서(order)가 있으면 그 순서로 가른다.
    # 돌려주는 값: (순위 묶음 목록, 가릴 수 없었던 까닭 'equal'·'no_games') — 조의 경기가 다 끝나지 않았으면 (None, None)
    ms = [m for m in matches if m["pool"] == pool["label"]]
    if any(m["status"] != "confirmed" for m in ms):
        return None, None
    reasons = []

    def split(group, key):
        values = {n: key(n) for n in group}
        return [[n for n in group if values[n] == v] for v in sorted(set(values.values()), reverse=True)]

    def resolve(group):
        if len(group) == 1:
            return [group]
        among = [m for m in ms if m["players"][0] in group and m["players"][1] in group]

        def total(n, part):   # part: 0 이긴 게임, 1 진 게임, 2 딴 점수, 3 잃은 점수
            result = 0
            for m in among:
                if n not in m["players"]:
                    continue
                mine = 0 if m["players"][0] == n else 1
                for g in m["games"]:
                    own, other = g[mine], g[1 - mine]
                    result += ((own > other), (own < other), own, other)[part]
            return result

        steps = [lambda n: sum(1 for m in among if m["winner"] == n)]
        if all(m.get("games") for m in among):
            steps += [lambda n: _ratio(total(n, 0), total(n, 1)), lambda n: _ratio(total(n, 2), total(n, 3))]
        for step in steps:
            tiers = split(group, step)
            if len(tiers) > 1:
                return [t for tier in tiers for t in resolve(tier)]
        reasons.append("equal" if len(steps) == 3 else "no_games")
        return [group]

    tiers = resolve(list(pool["players"]))
    order = pool.get("order") or []
    if order and all(n in order for n in pool["players"]):
        tiers = [[n] for tier in tiers for n in sorted(tier, key=order.index)]
    return tiers, ("no_games" if "no_games" in reasons else reasons[0] if reasons else None)


def pool_result(pool, matches):
    # 조의 상태: playing(경기가 남음) · tie(다 끝났지만 동률이라 운영진이 순위를 정해야 함) · done(순위 확정)
    tiers, reason = pool_ranking(pool, matches)
    if tiers is None:
        return {"state": "playing", "ranking": None, "tiers": None, "reason": None}
    if any(len(t) > 1 for t in tiers):
        return {"state": "tie", "ranking": None, "tiers": tiers, "reason": reason}
    return {"state": "done", "ranking": [t[0] for t in tiers], "tiers": tiers, "reason": None}


def pool_table(pool, matches):
    # 조 순위표의 숫자: 승·패, 게임·점수 득실 (확정된 경기만)
    rows = {n: {"name": n, "wins": 0, "losses": 0, "games_won": 0, "games_lost": 0, "points_won": 0, "points_lost": 0, "rank": None}
            for n in pool["players"]}
    for m in matches:
        if m["pool"] != pool["label"] or m["status"] != "confirmed":
            continue
        for i, n in enumerate(m["players"]):
            row = rows[n]
            row["wins" if m["winner"] == n else "losses"] += 1
            for g in m.get("games") or []:
                own, other = g[i], g[1 - i]
                row["games_won" if own > other else "games_lost"] += 1
                row["points_won"] += own
                row["points_lost"] += other
    return list(rows.values())


def describe_pool(bracket, pool):
    # 화면에 쓰는 조 요약을 매번 다시 계산해 붙인다: 상태·순위·순위표·동률 묶음, 경기마다 되돌릴 수 있는지(locked)
    matches = bracket["prelim"]["matches"]
    result = pool_result(pool, matches)
    rows = pool_table(pool, matches)
    seat_index = {n: i for i, n in enumerate(pool["players"])}
    if result["tiers"]:
        rank_of, position = {}, 1
        for tier in result["tiers"]:
            for n in tier:
                rank_of[n] = position
            position += len(tier)
        for row in rows:
            row["rank"] = rank_of[row["name"]]
        rows.sort(key=lambda r: (r["rank"], seat_index[r["name"]]))
    else:
        rows.sort(key=lambda r: (-r["wins"], r["losses"], seat_index[r["name"]]))
    pool.update(standings=rows, state=result["state"], ranking=result["ranking"],
                tied=[t for t in result["tiers"] if len(t) > 1] if result["state"] == "tie" else [],
                tie_reason=result["reason"] if result["state"] == "tie" else None)
    locked = any(_unseat_blocked(bracket, seat) for seat in pool_seats(pool))
    for m in matches:
        if m["pool"] == pool["label"]:
            m["locked"] = locked


def _match_slots(fillers, slots, preferred, allowed):
    # 이분 매칭(증가 경로)으로 사람마다 서로 다른 자리를 준다. 먼저 바라는 조건(preferred)만으로 최대한 많이 놓고,
    # 남은 사람은 허용 조건(allowed)으로 마저 놓는다(앞서 놓은 사람을 옮겨서라도). 다 놓을 수 없으면 None.
    owner = {}

    def place(f, seen, ok):
        for s in slots:
            if s in seen or not ok(fillers[f], s):
                continue
            seen.add(s)
            if s not in owner or place(owner[s], seen, ok):
                owner[s] = f
                return True
        return False

    for f in range(len(fillers)):
        place(f, set(), preferred)
    placed = set(owner.values())
    for f in range(len(fillers)):
        if f not in placed and not place(f, set(), allowed):
            return None
    return {fillers[f]: s for s, f in owner.items()}


def _round1_sides(first):
    # 1라운드 배치대로 대진표를 만들어 보고, 1라운드 경기마다 결승의 어느 쪽(0·1)으로 올라가는지 돌려준다
    dummy = [[f"{i}a", f"{i}b" if pair[1] is not None else None] for i, pair in enumerate(first)]
    b = build_from_first("x", dummy, {"best_of": 3})
    by_id = {m["id"]: m for m in b["matches"]}
    sides = []
    for m in sorted((m for m in b["matches"] if m["round"] == 1), key=lambda m: m["index"]):
        node, side = m, 0
        while node["next"]:
            side = node["slot"]
            node = by_id[node["next"]]
        sides.append(side)
    return sides


def seat_layout(pools):
    # 본선 1라운드 배치(자리표). 조 1위는 다른 조 2위와 첫 경기를 하고, 같은 조 1·2위는 결승의 반대쪽에 둔다.
    # 1위는 조 순서(A가 가장 높은 시드)대로 표준 시드 위치에 퍼뜨리고, 인원이 홀수면 A조 1위가 1라운드 부전승(맨 끝 자리)이다.
    # 조를 고쳐 1위가 경기 수보다 많아지면 남는 1위도 2위 자리에 들어간다.
    winners = [seat_label(p["label"], 1) for p in pools]
    runners = [seat_label(p["label"], 2) for p in pools if len(p["players"]) >= 2]
    total = len(winners) + len(runners)
    count = (total + 1) // 2
    first = [[None, None] for _ in range(count)]
    open_count = count - 1 if total % 2 else count
    if total % 2:
        first[-1][0] = winners.pop(0)
    size = 1
    while size < open_count:
        size *= 2
    order = [seed for seed in seed_order(size) if seed <= open_count]   # order[자리] = 그 자리의 시드
    for i, seat in enumerate(winners[:open_count]):
        first[order.index(i + 1)][0] = seat
    fillers = winners[open_count:] + runners
    sides = _round1_sides([[1, 1] for _ in range(open_count)] + ([[1, None]] if total % 2 else []))
    pool_of = lambda seat: seat.rsplit(":", 1)[0]
    home = {pool_of(pair[0]): sides[i] for i, pair in enumerate(first) if pair[0]}
    slots = list(range(open_count))
    other_pool = lambda seat, i: pool_of(first[i][0]) != pool_of(seat)
    other_side = lambda seat, i: other_pool(seat, i) and home.get(pool_of(seat)) != sides[i]
    # 경기 수가 홀수라 결승의 두 쪽 크기가 다르면 모든 조를 반대쪽에 둘 수는 없다 — 그래도 되도록 많이
    placed = _match_slots(fillers, slots, other_side, other_pool) or dict(zip(fillers, slots))   # 조가 하나뿐이면 같은 조끼리(결승)
    for seat, i in placed.items():
        first[i][1] = seat
    return first


def build_main(group_key, pools, fmt):
    # 자리표로 본선 대진표를 만든다. build_from_first는 자리표를 이름처럼 다뤄 부전승을 올려 두므로 자리표만 남기고 비운다.
    bracket = build_from_first(group_key, seat_layout(pools), fmt)
    for m in bracket["matches"]:
        if m["round"] == 1:
            m["seats"] = list(m["players"])
        m.update({"players": [None, None], "winner": None, "games": None, "status": "waiting"})
    bracket["champion"] = None
    return bracket


def prelim_bracket(group_key, pools, fmt):
    bracket = build_main(group_key, pools, fmt)
    bracket["prelim"] = {"pools": pools, "matches": [m for p in pools for m in pool_matches(group_key, p)]}
    sync_pools(bracket)   # 본선 직행(혼자인 조)은 바로 자리에 들어간다
    return bracket


def build_group(group_key, names, seed_mode, divisions, fmt, rng=None):
    # 그룹 하나의 대진: 예선이 있으면 조를 짜고 본선 자리표를 만들고, 없으면 바로 토너먼트
    if wants_prelim(fmt, len(names)):
        return prelim_bracket(group_key, make_pools(names, seed_mode, divisions, rng), fmt)
    return build_bracket(group_key, names, seed_mode, divisions, fmt, rng)


def _seat_match(bracket, seat):
    for m in bracket["matches"]:
        if m["round"] == 1 and seat in (m.get("seats") or []):
            return m, m["seats"].index(seat)
    return None, None


def _unseat_blocked(bracket, seat):
    # 그 자리의 사람을 빼면 안 되는 경우: 본선 첫 경기가 이미 끝났거나, 부전승으로 올라가 다음 실제 경기가 끝났다
    m, slot = _seat_match(bracket, seat)
    if m is None or not m["players"][slot]:
        return False
    if m["status"] == "confirmed":
        return True
    if m["status"] == "bye":
        by_id = {x["id"]: x for x in bracket["matches"]}
        real = by_id.get(m["next"])
        while real is not None and real.get("bye"):
            real = by_id.get(real["next"])
        return real is not None and real["status"] == "confirmed"
    return False


def sync_pools(bracket):
    # 조 순위와 본선 자리를 맞춘다: 순위가 정해진 조는 1·2위를 자리에 넣고, 아직(또는 다시) 안 정해진 조는 자리를 비운다.
    # 비워야 하는 자리의 본선 경기가 이미 끝났으면 아무것도 바꾸지 않고 거절한다 (본선 경기부터 되돌려야 한다).
    prelim = bracket.get("prelim")
    if not prelim:
        return
    wanted = {}
    for pool in prelim["pools"]:
        ranking = pool_result(pool, prelim["matches"])["ranking"]
        for rank, seat in enumerate(pool_seats(pool)):
            wanted[seat] = ranking[rank] if ranking else None
    changes = []
    for seat, name in wanted.items():
        m, slot = _seat_match(bracket, seat)
        if m is not None and m["players"][slot] != name:
            changes.append((seat, m, slot, name))
    if any(m["players"][slot] and _unseat_blocked(bracket, seat) for seat, m, slot, _ in changes):
        raise LeagueError("본선 경기가 이미 끝나 예선 순위를 바꿀 수 없습니다. 본선 경기부터 되돌리세요.")
    by_id = {m["id"]: m for m in bracket["matches"]}
    for seat, m, slot, name in changes:   # 먼저 바뀌는 자리를 모두 비우고
        if m["players"][slot]:
            if m["status"] == "bye":
                _retract(bracket, by_id, m)
            m["players"][slot] = None
            m.update({"winner": None, "games": None, "status": "waiting"})
    for seat, m, slot, name in changes:   # 그다음 채운다 (상대도 정해졌으면 경기 가능, 부전승 자리면 바로 다음 라운드로)
        if not name:
            continue
        m["players"][slot] = name
        expected, present = len(_entries(m)), [p for p in m["players"] if p]
        if len(present) == expected == 2:
            m["status"] = "pending"
        elif len(present) == expected == 1:
            _advance(bracket, by_id, m, name, status="bye")
    sync_third(bracket)


def place_in_pools(state, key, names, bracket):
    # 편성 중(예선): 조와 참가자를 맞춘다. 떠난 사람은 조에서 빼고(빈 조는 없애고 이름표를 다시 붙인다), 새로 온 사람은
    # 3명이 안 된 조 가운데 가장 많이 찬 조(2명 조 → 혼자인 조)에, 그런 조가 없으면 새 조(본선 직행)에 넣는다.
    # 인원이 2명 이하가 되면 예선 없는 대진으로, 3명이 되면 예선 대진으로 새로 만든다. 상태가 같으면 어디서 해도 결과가 같다.
    fmt = state["format"]
    divisions = {p["name"]: p["division"] for p in state["participants"]}
    third_on = bool(bracket and bracket.get("third"))
    if not bracket or bool(bracket.get("prelim")) != wants_prelim(fmt, len(names)):
        state["brackets"][key] = build_group(key, names, state["seed"], divisions, fmt, random.Random("|".join(sorted(names))))
        _keep_third(state, key, third_on)
        return
    wanted = set(names)
    before = [list(p["players"]) for p in bracket["prelim"]["pools"]]
    pools = [p for p in ([n for n in pool if n in wanted] for pool in before) if p]
    placed = {n for p in pools for n in p}
    for name in names:
        if name in placed:
            continue
        open_pools = [p for p in pools if len(p) < POOL_SIZE]
        if open_pools:
            max(open_pools, key=len).append(name)
        else:
            pools.append([name])
    if pools == before:
        return
    state["brackets"][key] = prelim_bracket(key, [{"label": pool_label(i), "players": p, "order": None} for i, p in enumerate(pools)], fmt)
    _keep_third(state, key, third_on)


def move_in_pools(bracket, group_key, fmt, name, label, index):
    # 예선 조 사이에서 선수를 옮긴다(결과가 없을 때만). 그 자리에 사람이 있으면 서로 바꾸고, 빈 자리면 옮긴다.
    # 조는 3명까지이고, 옮겨서 빈 조가 생기면 거절한다. 바뀐 조로 예선 경기와 본선 자리표를 새로 만든다. 바뀐 게 없으면 None.
    labels = [p["label"] for p in bracket["prelim"]["pools"]]
    pools = [list(p["players"]) for p in bracket["prelim"]["pools"]]
    if label not in labels or isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < POOL_SIZE:
        raise LeagueError("옮길 조와 자리가 올바르지 않습니다.")
    source = next(((i, j) for i, p in enumerate(pools) for j, n in enumerate(p) if n == name), None)
    if source is None:
        raise LeagueError("이 그룹 예선 조에 없는 선수입니다.")
    target, (si, sj) = labels.index(label), source
    if index < len(pools[target]):
        if (si, sj) == (target, index):
            return None
        pools[target][index], pools[si][sj] = name, pools[target][index]
    else:
        if si == target:
            return None
        if len(pools[target]) >= POOL_SIZE:
            raise LeagueError("그 조는 이미 3명입니다. 다른 선수와 자리를 바꿔 주세요.")
        if len(pools[si]) == 1:
            raise LeagueError("그 자리로 옮기면 빈 조가 생깁니다. 다른 선수와 자리를 바꿔 주세요.")
        pools[si].pop(sj)
        pools[target].append(name)
    return prelim_bracket(group_key, [{"label": labels[i], "players": p, "order": None} for i, p in enumerate(pools)], fmt)


def set_pool_order(state, group, label, order):
    # 운영진: 규정으로 가려지지 않은 동률 조의 순위를 정한다 (조의 경기가 모두 끝난 뒤에만, 동률 묶음 안에서만 쓰인다)
    bracket = state["brackets"].get(group)
    if state["status"] != "running" or not bracket or not bracket.get("prelim"):
        raise LeagueError("진행 중인 예선에서만 순위를 정할 수 있습니다.")
    pool = next((p for p in bracket["prelim"]["pools"] if p["label"] == label), None)
    if pool is None:
        raise LeagueError("조가 올바르지 않습니다.")
    if sorted(order) != sorted(pool["players"]):
        raise LeagueError("조의 모든 선수를 순위대로 골라 주세요.")
    previous, pool["order"] = pool.get("order"), None
    state_now = pool_result(pool, bracket["prelim"]["matches"])["state"]
    if state_now != "tie":
        pool["order"] = previous
        raise LeagueError("조의 경기가 모두 끝난 뒤에 순위를 정할 수 있습니다." if state_now == "playing" else "규정으로 순위가 정해진 조입니다.")
    pool["order"] = list(order)
    sync_pools(bracket)
    refresh_status(state)


def _build_all(state, rng):
    divisions = {p["name"]: p["division"] for p in state["participants"]}
    state["brackets"] = {}
    for g in state["groups"]:
        names = [p["name"] for p in state["participants"] if p["group"] == g["key"]]
        if names:
            state["brackets"][g["key"]] = build_group(g["key"], names, state["seed"], divisions, state["format"], rng)


def make_draft(state, rng=None):
    # 접수 중 → 편성 중: 지금 참가자로 그룹별 대진표를 만든다. 참가 접수는 시작할 때까지 계속 열려 있다.
    if state["status"] != "lobby":
        raise LeagueError("이미 대진표를 만들었습니다." if state["status"] == "draft" else "이미 시작한 토너먼트입니다.")
    if len(state["participants"]) < 2:
        raise LeagueError("참가자가 2명 이상이어야 대진표를 만들 수 있습니다.")
    _build_all(state, rng)
    state["status"] = "draft"


def start_tournament(state, rng=None):
    # 편성 중 → 진행 중: 참가 접수를 마감하고 고쳐 둔 대진표 그대로 시작한다. (접수 중에서 바로 시작하면 대진표도 이때 만든다)
    if state["status"] not in OPEN_STATUSES:
        raise LeagueError("이미 시작한 토너먼트입니다.")
    if len(state["participants"]) < 2:
        raise LeagueError("참가자가 2명 이상이어야 시작할 수 있습니다.")
    if state["status"] == "lobby":
        _build_all(state, rng)
    state["status"] = "running"
    state["started_at"] = kst_now()
    refresh_status(state)


def apply_settings(state, best_of_from):
    # 5판 3선 전환 시점을 바꾸면 아직 안 끝난 경기의 판수만 다시 정한다
    if best_of_from not in BEST_OF_FROM_CHOICES:
        raise LeagueError("5판 3선 전환 시점이 올바르지 않습니다.")
    if state["format"].get("prelims"):
        raise LeagueError("예선이 있는 토너먼트는 예선 3판 2선, 본선 5판 3선으로 정해져 있습니다.")
    state["format"]["best_of_from"] = best_of_from if state["format"]["best_of"] == 3 else 0
    for bracket in state["brackets"].values():
        for m in _all_matches(bracket):
            if m["status"] not in ("confirmed", "bye"):
                m["best_of"] = match_best_of(state["format"], 2 if m.get("third") else level_entrants(bracket, m["round"]))


# ---------------------------------------------------------------------------
# 운영 기록: 운영진 동작 하나 = 로그 한 줄. 요청을 처리할 때와 로그에서 되살릴 때 같은 apply_op를 쓴다.
# ---------------------------------------------------------------------------
def new_op(kind, **params):
    return {"id": secrets.token_hex(6), "op": kind, **params}


def apply_op(state, op):
    # 무작위 배치는 기록 id를 씨앗으로 쓴다. 참가자가 같으면 어느 인스턴스에서 다시 적용해도 같은 대진이 나오고,
    # 동시에 들어온 추가 때문에 참가자가 늘었다면 되살릴 때 그 사람까지 넣어 다시 짠다.
    kind, rng = op.get("op"), random.Random(op["id"])
    if kind == "draft":
        make_draft(state, rng)
    elif kind == "start":
        start_tournament(state, rng)
    elif kind == "settings":
        apply_settings(state, op.get("best_of_from"))
    elif kind == "third":
        if state["status"] not in ("draft", "running", "finished"):
            raise LeagueError("대진표를 만든 뒤에 정할 수 있습니다.")
        if op.get("group") not in state["brackets"]:
            raise LeagueError("그룹이 올바르지 않습니다.")
        set_third_place(state["brackets"][op["group"]], op["group"], state["format"], bool(op.get("enabled")))
        refresh_status(state)
    elif kind == "rebuild":
        group = op.get("group")
        # 대진표가 아직 없는 그룹도 된다 (대진표 밖에 남은 참가자를 넣을 때)
        if state["status"] not in ("draft", "running") or group not in {g["key"] for g in state["groups"]}:
            raise LeagueError("대진표를 만든 뒤에 다시 만들 수 있습니다.")
        if op.get("seed") not in ("random", "division"):
            raise LeagueError("배치 방식이 올바르지 않습니다.")
        _editable_groups(state, {group})
        rebuild_group(state, group, op["seed"], rng)
        refresh_status(state)
    elif kind in ("move", "swap"):
        group = op.get("group")
        if state["status"] not in ("draft", "running") or group not in state["brackets"]:
            raise LeagueError("대진표를 만든 뒤에 고칠 수 있습니다.")
        _editable_groups(state, {group})
        if state["brackets"][group].get("prelim"):
            raise LeagueError("예선이 있는 그룹의 본선 자리는 예선 순위로 정해집니다. 예선 조에서 옮겨 주세요.")
        if kind == "move":
            move_player(state["brackets"][group], op.get("name"), op.get("match"), op.get("slot"))
        else:
            swap_slots(state["brackets"][group], op.get("a"), op.get("b"))
        refresh_status(state)
    elif kind == "confirm":
        if state["status"] != "running":
            raise LeagueError("진행 중인 토너먼트가 아닙니다.")
        _, match = find_match(state, op.get("match"))
        if op.get("players") and match["players"] != op["players"]:
            raise LeagueError("그사이 대진이 바뀌어 이 결과를 반영할 수 없습니다.")
        confirm_match(state, op["match"], op.get("winner"), op.get("games"))
    elif kind == "reset":
        reset_match(state, op.get("match"))
    elif kind == "pool_move":
        group = op.get("group")
        bracket = state["brackets"].get(group)
        if state["status"] not in ("draft", "running") or not bracket or not bracket.get("prelim"):
            raise LeagueError("예선 조를 만든 뒤에 고칠 수 있습니다.")
        _editable_groups(state, {group})
        moved = move_in_pools(bracket, group, state["format"], op.get("name"), op.get("pool"), op.get("index"))
        if moved:
            state["brackets"][group] = moved
            _keep_third(state, group, bool(bracket.get("third")))
        refresh_status(state)
    elif kind == "pool_order":
        set_pool_order(state, op.get("group"), op.get("pool"), op.get("order") or [])
    elif kind in ("group", "remove"):
        name = op.get("name")
        current = next((p for p in state["participants"] if p["name"] == name), None)
        if current is None:
            raise LeagueError("참가자 목록에 없는 이름입니다.")
        affected = {current["group"]}
        if kind == "remove":
            _editable_groups(state, affected)
            state["removed"].append(name)
            state["group_overrides"].pop(name, None)
        else:
            if op.get("group") not in {g["key"] for g in state["groups"]}:
                raise LeagueError("그룹이 올바르지 않습니다.")
            affected.add(op["group"])
            _editable_groups(state, affected)
            state["group_overrides"][name] = op["group"]
        compute_participants(state)
        _update_brackets(state, affected, rng)
    elif kind == "add":
        # 참가 로그는 이 기록과 같은 호출로 바로 앞에 들어간다 (요청을 처리할 때는 _joins에 미리 넣어 둔다)
        names = op.get("names") or []
        state["removed"] = [n for n in state["removed"] if n not in names]
        compute_participants(state)
        affected = {p["group"] for p in state["participants"] if p["name"] in names}
        _editable_groups(state, affected)
        _update_brackets(state, affected, rng)
    else:
        raise LeagueError("알 수 없는 운영 기록입니다.")


# ---------------------------------------------------------------------------
# 라우트
# ---------------------------------------------------------------------------
def _members():
    members, is_dummy = _deps["get_sheet_data"]()
    return {m["이름"]: m for m in members}, is_dummy


def _require_admin(state):
    if not _is_admin(state):
        raise LeagueError("운영진만 할 수 있는 작업입니다.", 403)


def _is_admin(state):
    key = request.headers.get("X-League-Key") or (request.get_json(silent=True) or {}).get("admin_key") or ""
    return bool(key) and secrets.compare_digest(key, state.get("admin_key", ""))


def _ok(state, **extra):
    return jsonify({"tournament": public_view(state, admin=_is_admin(state)), **extra})


def _run(state, op, joins=(), **extra):
    # 운영진 요청 처리: 먼저 지금 상태에 적용해 보고(안 되면 LeagueError로 아무것도 쓰지 않고 끝), 되면 기록한다.
    # 다른 운영진의 저장이 나중에 스냅숏을 덮더라도 이 기록은 로그에 있으므로 다음에 읽을 때 다시 적용된다.
    apply_op(state, op)
    _check_write_limit()
    commit_op(state, op, joins)
    refresh_derived(state)
    return _ok(state, **extra)


@league_bp.errorhandler(LeagueError)
def _handle_error(error):
    return jsonify({"error": str(error), **error.extra}), error.status


@league_bp.errorhandler(Exception)
def _handle_unexpected(error):
    import gspread

    if isinstance(error, gspread.exceptions.SpreadsheetNotFound):
        return jsonify({"error": f"'{_deps['league_sheet_name']}' 구글 시트를 찾을 수 없습니다. 파일을 만들고 서비스 계정에 편집자로 공유해 주세요."}), 503
    if isinstance(error, gspread.exceptions.APIError):
        _deps["logger"].error("리그전 구글 시트 오류: %s", error)
        return jsonify({"error": "구글 시트 응답이 늦거나 호출 한도를 넘었습니다. 잠시 후 다시 시도해 주세요."}), 503
    _deps["logger"].exception("리그전 처리 오류")
    return jsonify({"error": "처리 중 오류가 났습니다. 잠시 후 다시 시도해 주세요."}), 500


@league_bp.route("/league")
def league_home():
    return render_template("league/home.html")


@league_bp.route("/league/new")
def league_new():
    return render_template("league/new.html", default_groups=DEFAULT_GROUPS,
                           point_targets=_deps["point_targets"], best_of_options=_deps["best_of_options"])


@league_bp.route("/league/join")
def league_join():
    members, is_dummy = _members()
    try:
        rooms = [] if is_dummy else list_rooms()
    except Exception as e:  # 목록을 못 읽어도 코드 입력으로는 참가할 수 있어야 한다
        _deps["logger"].error("토너먼트 목록 조회 실패: %s", e)
        rooms = []
    return render_template("league/join.html", members=sorted(members.values(), key=_deps["member_sort_key"]),
                           code=normalize_code(request.args.get("code", "")), rooms=rooms, is_dummy=is_dummy)


@league_bp.route("/api/league")
def api_rooms():
    return jsonify({"rooms": list_rooms()})


@league_bp.route("/league/admin")
def league_admin_login():
    # 예전 '운영 화면 열기' 주소: 운영 화면은 열려 있는 토너먼트의 방 카드에서 '운영'으로 연다
    return redirect(url_for("league.league_join"))


def _room_name(code):
    # 탭 제목용 대회 이름 (못 읽으면 화면 스크립트가 불러온 뒤 채운다)
    try:
        return load_state(code)["name"]
    except Exception:
        return None


@league_bp.route("/league/<code>")
def league_room(code):
    return render_template("league/room.html", code=normalize_code(code), is_admin=False, room_name=_room_name(code), roster=[])


@league_bp.route("/league/v/<view>")
def league_view(view):
    # 보기 전용 대진표: 참가 코드 없이 본다 (참가·결과 보고·운영은 할 수 없다)
    try:
        room_name = load_state_by_view(view)["name"]
    except Exception:
        room_name = None
    return render_template("league/room.html", code="", is_admin=False, viewer=True, view=view, room_name=room_name, roster=[])


@league_bp.route("/league/<code>/admin")
def league_admin(code):
    members, _ = _members()
    roster = [{"이름": m["이름"], "부수": m["부수"]} for m in sorted(members.values(), key=_deps["member_sort_key"])]
    return render_template("league/room.html", code=normalize_code(code), is_admin=True, room_name=_room_name(code), roster=roster)


@league_bp.route("/api/league", methods=["POST"])
def api_create():
    _, is_dummy = _members()
    if is_dummy:
        raise LeagueError("구글 시트에 연결되어 있지 않아 토너먼트를 만들 수 없습니다.", 503)
    _check_write_limit()
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()[:40] or f"탁우회 리그전 {kst_now()[:10]}"
    # 경기 방식은 늘 3판 2선으로 시작하고, best_of_from(남은 선수 수)부터 5판 3선
    best_of = 3
    try:
        target = int(data.get("target", 11))
        best_of_from = int(data.get("best_of_from", 0) or 0)
    except (TypeError, ValueError):
        raise LeagueError("경기 방식이 올바르지 않습니다.")
    if target not in _deps["point_targets"]:
        raise LeagueError("지원하지 않는 경기 방식입니다.")
    if best_of_from not in BEST_OF_FROM_CHOICES:
        raise LeagueError("5판 3선 전환 시점이 올바르지 않습니다.")
    seed = data.get("seed", "random")
    if seed not in ("random", "division"):
        raise LeagueError("대진 배치 방식이 올바르지 않습니다.")
    groups = _parse_groups(data.get("groups"))
    # 예선을 하면 예선은 3판 2선, 본선(예선 뒤의 경기)은 모두 5판 3선이라 전환 시점은 쓰지 않는다
    prelims = bool(data.get("prelims"))
    if prelims:
        best_of_from = 0

    rows, _ = _read_all(force=True)
    # 참가 코드끼리, 관리자 코드끼리 겹치지 않게 한다 — 관리자 코드만으로 방을 찾아 운영 화면을 열기 때문이다
    used = {r[0].strip() for r in rows[1:] if r} | {c for _, c in _admin_codes(rows)}
    code = new_code(used)
    admin_code = new_code(used | {code})
    state = {
        "code": code, "name": name, "status": "lobby", "created_at": kst_now(), "updated_at": kst_now(),
        "admin_key": secrets.token_urlsafe(18), "admin_code": admin_code,
        "format": {"target": target, "best_of": best_of, "best_of_from": best_of_from, "prelims": prelims},
        "groups": groups, "seed": seed, "group_overrides": {}, "removed": [], "brackets": {}, "applied": [],
    }
    save_state(state)
    return jsonify({"code": code, "name": state["name"], "admin_key": state["admin_key"], "admin_code": admin_code,
                    "admin_url": url_for("league.league_admin", code=code, key=state["admin_key"]),
                    "join_url": url_for("league.league_room", code=code)})


def _check_admin_login_limit():
    now = time.monotonic()
    while _admin_logins and now - _admin_logins[0] > 600:
        _admin_logins.popleft()
    if len(_admin_logins) >= ADMIN_LOGIN_LIMIT:
        raise LeagueError("관리자 코드 시도가 너무 많습니다. 잠시 후 다시 해 주세요.", 429)
    _admin_logins.append(now)


def _admin_codes(rows):
    # (행의 상태JSON, 관리자 코드) — 지운 방은 빼고. 예전 방은 F열이 비어 있을 수 있어 JSON에서 읽는다.
    for row in rows[1:]:
        if len(row) < 5 or not row[4]:
            continue
        try:
            raw = json.loads(row[4])
        except ValueError:
            continue
        if raw.get("admin_code"):
            yield raw, raw["admin_code"]


def _login_response(state):
    return jsonify({"code": state["code"], "name": state["name"], "admin_key": state["admin_key"], "admin_code": state["admin_code"],
                    "admin_url": url_for("league.league_admin", code=state["code"])})


@league_bp.route("/api/league/admin-login", methods=["POST"])
def api_admin_login_by_code():
    # 열려 있는 토너먼트의 방 카드에서 '운영'을 누르고 관리자 코드를 넣은 경우: 카드의 보기 키(view)와 관리자 코드가
    # 같은 방을 가리키면 이 기기에 운영 키를 내준다. 다른 방의 관리자 코드면 그 방을 열지 않고 알려 준다.
    # 운영 화면은 카드로만 연다 — 보기 키 없이 관리자 코드만으로는 열지 않는다.
    _check_admin_login_limit()
    data = request.get_json(silent=True) or {}
    view = str(data.get("view", "")).strip()
    if not view:
        raise LeagueError("열려 있는 토너먼트에서 그 토너먼트 카드의 운영을 눌러 관리자 코드를 넣어 주세요.")
    admin_code = normalize_code(data.get("admin_code", ""))
    if len(admin_code) != 6:
        raise LeagueError("관리자 코드 6자리를 넣어 주세요.")
    rows, _ = _read_all()
    found = [raw for raw, c in _admin_codes(rows) if secrets.compare_digest(c, admin_code)]
    if not found:
        raise LeagueError("그 관리자 코드의 토너먼트가 없습니다.", 403)
    chosen = [raw for raw in found if raw.get("admin_key") and view_key(raw) == view]
    if not chosen:
        other = max(found, key=lambda raw: raw.get("created_at", ""))
        raise LeagueError(f"입력한 관리자 코드는 '{other.get('name', '')}' 토너먼트의 코드입니다. 고른 토너먼트의 관리자 코드를 다시 확인해 주세요.")
    found = chosen
    # 예전에 만든 방끼리 코드가 겹쳤다면 가장 최근 방
    latest = max(found, key=lambda raw: raw.get("created_at", ""))
    return _login_response(load_state(latest["code"]))


@league_bp.route("/api/league/<code>/admin-login", methods=["POST"])
def api_admin_login(code):
    # 방 화면에서 관리자 코드를 넣은 경우 (주소에 참가 코드가 있다)
    state = load_state(code)
    _check_admin_login_limit()
    admin_code = normalize_code((request.get_json(silent=True) or {}).get("admin_code", ""))
    if not admin_code or not secrets.compare_digest(admin_code, state.get("admin_code", "")):
        raise LeagueError("관리자 코드가 맞지 않습니다.", 403)
    return _login_response(state)


@league_bp.route("/api/league/<code>/settings", methods=["POST"])
def api_settings(code):
    # 운영진: 진행 중에도 5판 3선 전환 시점을 바꿀 수 있다 (아직 안 끝난 경기에만 적용)
    state = load_state(code, force=True)
    _require_admin(state)
    data = request.get_json(silent=True) or {}
    try:
        best_of_from = int(data.get("best_of_from", 0) or 0)
    except (TypeError, ValueError):
        raise LeagueError("5판 3선 전환 시점이 올바르지 않습니다.")
    return _run(state, new_op("settings", best_of_from=best_of_from))


@league_bp.route("/api/league/<code>/third-place", methods=["POST"])
def api_third_place(code):
    # 운영진: 그룹별 3·4위전을 진행 중에 켜거나 끈다
    state = load_state(code, force=True)
    _require_admin(state)
    data = request.get_json(silent=True) or {}
    return _run(state, new_op("third", group=data.get("group"), enabled=bool(data.get("enabled"))))


def _parse_groups(raw):
    # 그룹 경계: [{"name": "상위부", "max": 4}, {"name": "중위부", "max": 7}, {"name": "하위부"}] 처럼 상한만 받는다
    if not raw:
        return [dict(g) for g in DEFAULT_GROUPS]
    groups, low = [], -99
    try:
        for i, g in enumerate(raw[:5]):
            name = str(g.get("name", "")).strip()[:10] or DEFAULT_GROUPS[min(i, 2)]["name"]
            is_last = i == len(raw) - 1
            high = 99 if is_last else int(g["max"])
            if high < low:
                raise ValueError
            groups.append({"key": f"g{i + 1}", "name": name, "min": low, "max": high})
            low = high + 1
    except (TypeError, ValueError, KeyError):
        raise LeagueError("그룹 기준이 올바르지 않습니다.")
    if len(groups) < 1:
        raise LeagueError("그룹이 하나 이상 필요합니다.")
    return groups


@league_bp.route("/api/league/<code>")
def api_state(code):
    state = load_state(code)
    return _ok(state)


@league_bp.route("/api/league/v/<view>")
def api_view_state(view):
    return jsonify({"tournament": viewer_view(load_state_by_view(view))})


@league_bp.route("/api/league/<code>/join", methods=["POST"])
def api_join(code):
    # 참가 또는 다시 연결. 새 이름은 접수 중에만 받는다. 이미 참가자인 이름은 시작한 뒤에도 이 기기로 다시 연결할 수 있다
    # (폰을 바꿨거나 앱 안 브라우저로 참가했다가 다른 브라우저로 연 경우). 그 이름이 다른 기기에 연결돼 있으면 reconnect로
    # 확인을 받고, 예전 기기의 토큰은 끊는다. 운영진이 넣어 둔 이름(아직 기기 없음)은 확인 없이 연결한다.
    state = load_state(code)
    members, _ = _members()
    data = request.get_json(silent=True) or {}
    room = str(data.get("room", "")).strip()
    if room and room != state["name"]:
        raise LeagueError(f"입력한 코드는 '{state['name']}' 방의 코드입니다. '{room}' 방의 코드를 다시 확인해 주세요.")
    name = _deps["normalize_name"](data.get("name", ""))
    if name not in members:
        raise LeagueError("명단에 없는 이름입니다. 부수표에 등록된 이름을 골라 주세요.")
    if name in state["removed"]:
        raise LeagueError("운영진이 참가 목록에서 제외한 이름입니다. 운영진에게 문의하세요.")
    joined = name in state["_tokens"]
    if not joined and state["status"] not in OPEN_STATUSES:
        raise LeagueError("참가 접수가 끝난 토너먼트입니다. 이미 참가한 사람만 다시 연결할 수 있습니다.")
    reconnect = joined and bool(state["_tokens"][name])
    if reconnect and not data.get("reconnect"):
        raise LeagueError("이미 참가한 이름입니다. 본인이면 이 기기로 다시 연결할 수 있습니다. 예전 기기는 연결이 끊깁니다.", 409, can_reconnect=True)
    _check_write_limit()
    token = secrets.token_urlsafe(12)
    entry = {"name": name, "division": members[name]["부수"], "token": token}
    if reconnect:
        entry["reconnect"] = True
    at = append_logs(state, [("참가", entry)])
    # 방금 들어온 사람을 응답에 바로 넣어 준다 (다시 읽지 않음)
    state["_joins"].append({**entry, "at": at})
    compute_participants(state)
    return jsonify({"token": token, "name": name, "reconnected": reconnect, "tournament": public_view(state)})


def _editable_groups(state, keys):
    # 진행 중에는 결과가 하나도 없는 그룹만 고칠 수 있다
    if state["status"] == "finished":
        raise LeagueError("끝난 토너먼트는 바꿀 수 없습니다.")
    if state["status"] == "running":
        for key in keys:
            bracket = state["brackets"].get(key)
            if bracket and group_has_results(bracket):
                raise LeagueError(f"'{_group_name(state, key)}'은(는) 이미 확정된 결과가 있어 바꿀 수 없습니다. 먼저 되돌리세요.")


def _group_name(state, key):
    return next((g["name"] for g in state["groups"] if g["key"] == key), key)


def _update_brackets(state, keys, rng=None):
    # 참가자가 바뀐 그룹의 대진표: 편성 중이면 빈 자리에 넣고(고쳐 둔 자리 유지), 진행 중이면 그 그룹을 새로 짠다
    for key in sorted(keys):   # 씨앗 하나로 여러 그룹을 섞으므로 순서를 고정한다 (set 순서는 인스턴스마다 다르다)
        if state["status"] == "draft":
            place_players(state, key)
        elif state["status"] == "running":
            rebuild_group(state, key, rng=rng)
    refresh_status(state)


@league_bp.route("/api/league/<code>/participants", methods=["POST"])
def api_participants(code):
    # 운영진: 그룹 조정 또는 참가자 제외 (진행 중에는 결과가 없는 그룹만, 그 그룹 대진표는 다시 만든다)
    state = load_state(code, force=True)
    _require_admin(state)
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    op = new_op("remove", name=name) if data.get("remove") else new_op("group", name=name, group=data.get("group"))
    return _run(state, op)


@league_bp.route("/api/league/<code>/participants/add", methods=["POST"])
def api_participants_add(code):
    # 운영진: 명단에서 골라 참가자를 미리 넣는다 (본인이 나중에 같은 이름으로 참가하면 그 자리에 연결된다)
    state = load_state(code, force=True)
    _require_admin(state)
    members, _ = _members()
    names = []
    for raw in (request.get_json(silent=True) or {}).get("names", [])[:100]:
        name = _deps["normalize_name"](raw)
        if name not in members:
            raise LeagueError(f"'{raw}'은(는) 명단에 없는 이름입니다.")
        if name not in names and name not in state["_tokens"]:
            names.append(name)
    if not names:
        raise LeagueError("추가할 새 이름이 없습니다.")
    # 참가 줄과 운영 기록을 한 번에 덧붙인다. 적용해 보기 전에 참가 줄을 미리 넣어 두어 새 이름이 참가자로 잡히게 한다.
    joins = [{"name": n, "division": members[n]["부수"], "token": "", "by": "운영진"} for n in names]
    now = kst_now()
    state["_joins"].extend({**j, "at": now} for j in joins)
    return _run(state, new_op("add", names=names), joins=joins, added=names)


@league_bp.route("/api/league/<code>/brackets/<group>/rebuild", methods=["POST"])
def api_rebuild(code, group):
    # 운영진: 결과가 없는 그룹의 대진표를 무작위 또는 부수 순으로 다시 만든다 (대진표 밖에 남은 참가자도 이때 들어간다)
    state = load_state(code, force=True)
    _require_admin(state)
    seed = (request.get_json(silent=True) or {}).get("seed") or state["seed"]
    return _run(state, new_op("rebuild", group=group, seed=seed))


@league_bp.route("/api/league/<code>/brackets/<group>/swap", methods=["POST"])
def api_swap(code, group):
    # 운영진: 결과가 없는 그룹에서 두 선수의 1라운드 자리를 바꾼다
    state = load_state(code, force=True)
    _require_admin(state)
    data = request.get_json(silent=True) or {}
    a, b = str(data.get("a", "")).strip(), str(data.get("b", "")).strip()
    if not a or not b or a == b:
        raise LeagueError("서로 다른 두 선수를 골라 주세요.")
    return _run(state, new_op("swap", group=group, a=a, b=b))


@league_bp.route("/api/league/<code>/brackets/<group>/move", methods=["POST"])
def api_move(code, group):
    # 운영진: 결과가 없는 그룹에서 선수를 1라운드의 다른 자리로 끌어 놓는다 (자리에 사람이 있으면 서로 바꾼다)
    state = load_state(code, force=True)
    _require_admin(state)
    data = request.get_json(silent=True) or {}
    name, match_id = str(data.get("name", "")).strip(), str(data.get("match", "")).strip()
    slot = data.get("slot")
    if not name or not match_id or slot not in (0, 1):
        raise LeagueError("옮길 선수와 자리를 알려 주세요.")
    return _run(state, new_op("move", group=group, name=name, match=match_id, slot=slot))


@league_bp.route("/api/league/<code>/brackets/<group>/pool-move", methods=["POST"])
def api_pool_move(code, group):
    # 운영진: 결과가 없는 그룹에서 선수를 다른 예선 조(의 자리)로 옮긴다 (자리에 사람이 있으면 서로 바꾼다)
    state = load_state(code, force=True)
    _require_admin(state)
    data = request.get_json(silent=True) or {}
    name, pool, index = str(data.get("name", "")).strip(), str(data.get("pool", "")).strip(), data.get("index")
    if not name or not pool or isinstance(index, bool) or not isinstance(index, int):
        raise LeagueError("옮길 선수와 자리를 알려 주세요.")
    return _run(state, new_op("pool_move", group=group, name=name, pool=pool, index=index))


@league_bp.route("/api/league/<code>/brackets/<group>/pool-order", methods=["POST"])
def api_pool_order(code, group):
    # 운영진: 동률이라 규정으로 가려지지 않은 예선 조의 순위를 정한다
    state = load_state(code, force=True)
    _require_admin(state)
    data = request.get_json(silent=True) or {}
    order = [str(n).strip() for n in (data.get("order") or [])][:POOL_SIZE]
    return _run(state, new_op("pool_order", group=group, pool=str(data.get("pool", "")).strip(), order=order))


@league_bp.route("/api/league/<code>/draft", methods=["POST"])
def api_draft(code):
    # 운영진: 접수 중 → 편성 중 (대진표를 만들어 고칠 수 있게 한다. 참가 접수는 계속 열려 있다)
    state = load_state(code, force=True)
    _require_admin(state)
    return _run(state, new_op("draft"))


@league_bp.route("/api/league/<code>/start", methods=["POST"])
def api_start(code):
    state = load_state(code, force=True)
    _require_admin(state)
    return _run(state, new_op("start"))


@league_bp.route("/api/league/<code>/matches/<match_id>/report", methods=["POST"])
def api_report(code, match_id):
    # 선수(참가 토큰) 또는 점수판이 결과를 보고한다. 운영 키가 함께 오면 바로 확정한다.
    # record가 false면(점수판 결과 상자의 X) 대진표에만 반영하고 전적(경기기록)에는 남기지 않는다 — 선수 보고면 확정할 때까지 이어진다.
    state = load_state(code, force=True)
    if state["status"] != "running":
        raise LeagueError("진행 중인 토너먼트가 아닙니다.")
    data = request.get_json(silent=True) or {}
    _, match = find_match(state, match_id)
    if match["status"] != "pending":
        raise LeagueError("지금 결과를 받을 수 있는 경기가 아닙니다.")
    winner = str(data.get("winner", "")).strip()
    if winner not in match["players"]:
        raise LeagueError("승자는 이 경기의 두 선수 중 하나여야 합니다.")
    error, _, winner_index = validate_games(data.get("games"), state["format"]["target"], match["best_of"])
    if error:
        raise LeagueError(error)
    if winner_index is not None and match["players"][winner_index] != winner:
        raise LeagueError("게임 점수와 승자가 맞지 않습니다.")

    record = data.get("record") is not False
    admin_key = request.headers.get("X-League-Key") or data.get("admin_key") or ""
    if admin_key and secrets.compare_digest(admin_key, state["admin_key"]):
        return _confirm_and_record(state, match, winner, data.get("games"), record)

    token = str(data.get("token", ""))
    reporter = next((n for n, t in state["_tokens"].items() if t and secrets.compare_digest(t, token)), None)
    if reporter is None and token:
        # 토큰은 있는데 맞는 사람이 없다: 같은 이름이 다른 기기로 다시 연결해 이 기기가 끊긴 경우
        raise LeagueError("이 기기의 참가 연결이 끊겼습니다. 참가 화면에서 다시 연결해 주세요.", 403)
    if reporter not in match["players"]:
        raise LeagueError("이 경기의 선수만 결과를 보낼 수 있습니다.", 403)
    _check_write_limit()
    # 보고에 그때의 대진(두 선수)을 함께 남겨, 나중에 대진이 바뀌면 이 보고가 다른 경기에 붙지 않게 한다
    entry = {"match": match_id, "players": match["players"], "winner": winner, "games": data.get("games") or None, "by": reporter}
    if not record:
        entry["record"] = False
    at = append_logs(state, [("보고", entry)])
    state["_reports"][match_id] = {**entry, "at": at}
    refresh_derived(state)
    return _ok(state, reported=True)


@league_bp.route("/api/league/<code>/matches/<match_id>/confirm", methods=["POST"])
def api_confirm(code, match_id):
    state = load_state(code, force=True)
    _require_admin(state)
    if state["status"] != "running":
        raise LeagueError("진행 중인 토너먼트가 아닙니다.")
    data = request.get_json(silent=True) or {}
    _, match = find_match(state, match_id)
    # 승자를 따로 주지 않으면 선수가 보고한 결과대로 확정한다 (보고가 '전적에 안 남김'이었으면 그대로)
    report = match.get("report") or {}
    winner = str(data.get("winner") or report.get("winner") or "").strip()
    games = data.get("games") if "games" in data else report.get("games")
    explicit = "winner" in data or "games" in data
    record = (data.get("record") is not False) if explicit else (report.get("record") is not False)
    return _confirm_and_record(state, match, winner, games, record)


def _confirm_and_record(state, match, winner, games, record=True):
    # 확정 기록에 그때의 두 선수를 남긴다. 되살릴 때 대진이 바뀌어 있으면 엉뚱한 경기에 적용하지 않고 건너뛴다.
    response = _run(state, new_op("confirm", match=match["id"], players=list(match["players"]), winner=winner, games=games),
                    confirmed=True)
    parsed = match["games"]
    # 게임 점수까지 있으면 전적(경기기록)에도 남긴다 (점수판의 X로 보낸 결과는 남기지 않는다)
    if parsed and record:
        a, b = match["players"]
        wins = [sum(1 for x, y in parsed if x > y), sum(1 for x, y in parsed if y > x)]
        target, best_of = state["format"]["target"], match["best_of"]
        row = [kst_now()[:16], a, b, str(wins[0]), str(wins[1]), winner,
               f"{target}점 {best_of}판 {best_of // 2 + 1}선 · 토너먼트 {state['name']}",
               ", ".join(f"{x}:{y}" for x, y in parsed)]
        try:
            _deps["record_match"](row)
        except Exception as e:  # 전적 기록 실패는 토너먼트 진행을 막지 않는다
            _deps["logger"].error("리그전 결과의 전적 기록 실패: %s", e)
    return response


@league_bp.route("/api/league/<code>/matches/<match_id>/reset", methods=["POST"])
def api_reset(code, match_id):
    state = load_state(code, force=True)
    _require_admin(state)
    return _run(state, new_op("reset", match=match_id))


@league_bp.route("/api/league/<code>/delete", methods=["POST"])
def api_delete(code):
    state = load_state(code, force=True)
    _require_admin(state)
    _check_write_limit()
    delete_tournament(state)
    return jsonify({"deleted": True})
