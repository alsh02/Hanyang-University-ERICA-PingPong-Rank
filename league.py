"""리그전(토너먼트) 운영.

운영진이 방을 개설하면 참가 코드가 생기고, 부원들은 코드로 들어와 명단에서 자기 이름을 고른다.
운영진이 시작하면 참가자를 부수 기준으로 상위부·중위부·하위부로 나눠 그룹마다 단판 토너먼트 대진표를 만든다.
경기가 끝나면 선수(또는 점수판)가 결과를 보고하고, 운영진이 확정하면 승자가 다음 대진으로 올라간다.
모든 그룹의 우승자가 정해지면 리그전이 끝난다.

상태는 경기 기록 파일의 두 시트에 둔다.
- '토너먼트': 방마다 한 줄. 운영진만 고치는 상태(JSON) — 대진표, 확정 결과, 그룹 조정.
- '토너먼트로그': 참가·결과 보고를 한 줄씩 덧붙이기만 한다(append-only). 여러 명이 동시에 눌러도 서로 덮어쓰지 않는다.
운영진의 상태 변경은 항상 최신 행을 다시 읽은 뒤 한 번에 쓰므로, 쓰는 사람이 운영진 하나뿐이라 충돌이 없다.
"""
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
STATUS_LABELS = {"lobby": "참가 접수 중", "running": "진행 중", "finished": "종료"}

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
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _spreadsheet():
    return _deps["open_records_spreadsheet"]()


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


def _read_all(force=False):
    # 두 시트를 API 호출 한 번(values_batch_get)으로 읽어 잠깐 캐시한다.
    # 구글 시트 읽기 한도가 사용자당 분당 60회라, 참가자 폰이 몇 초마다 새로 고쳐도 인스턴스당 호출은 CACHE_TTL마다 한 번이다.
    with _lock:
        now = time.monotonic()
        if force or now >= _cache["expires_at"] or _cache["rows"] is None:
            _ensure_sheets()
            ranges = _spreadsheet().values_batch_get(RANGES).get("valueRanges", [])
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
    # 방 상태(JSON) + 로그(참가·보고)를 합쳐 하나의 상태로 돌려준다
    code = normalize_code(code)
    rows, logs = _read_all(force)
    row_no, row = _find_row(rows, code)
    if row is None or len(row) < 5 or not row[4]:
        raise LeagueError("그 코드의 토너먼트가 없습니다.", 404)
    state = json.loads(row[4])
    state["_row_no"] = row_no   # 저장할 때 행을 다시 찾지 않도록 기억해 둔다
    return assemble(state, [l for l in logs[1:] if l and l[0].strip().upper() == code])


_WRITE_PARAMS = {"valueInputOption": "RAW"}


def save_state(state):
    # 운영진의 변경을 방 행에 한 번의 호출로 다시 쓴다 (행 번호는 읽을 때 기억해 둔 것)
    state["updated_at"] = kst_now()
    values = [state["code"], state["status"], state["created_at"], state["updated_at"],
              json.dumps(persistable(state), ensure_ascii=False), state.get("admin_code", "")]
    row_no = state.get("_row_no")
    if row_no is None:
        _ensure_sheets()
        _spreadsheet().values_append(RANGES[0], {**_WRITE_PARAMS, "insertDataOption": "INSERT_ROWS"}, {"values": [values]})
    else:
        _spreadsheet().values_update(f"'{TOURNAMENT_SHEET}'!A{row_no}:F{row_no}", _WRITE_PARAMS, {"values": [values]})
    _invalidate()


def append_log(code, kind, payload):
    _ensure_sheets()
    _spreadsheet().values_append(RANGES[1], {**_WRITE_PARAMS, "insertDataOption": "INSERT_ROWS"},
                                 {"values": [[code, kind, kst_now(), json.dumps(payload, ensure_ascii=False)]]})
    _invalidate()


def delete_tournament(code):
    worksheet = _spreadsheet().worksheet(TOURNAMENT_SHEET)
    row_no, _ = _find_row(worksheet.get_all_values(), code)
    if row_no:
        worksheet.delete_rows(row_no)
    _invalidate()


# ---------------------------------------------------------------------------
# 상태 조립
# ---------------------------------------------------------------------------
PERSIST_KEYS = ("code", "name", "status", "created_at", "updated_at", "started_at", "finished_at", "admin_key", "admin_code",
                "format", "groups", "seed", "group_overrides", "removed", "brackets")


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
    # 초반은 3판 2선, 정해 둔 라운드(남은 선수 수 기준)부터 5판 3선
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
    # 참가 로그 → 참가자 목록 (같은 이름은 처음 한 번만), 보고 로그 → 매치별 최신 보고
    participants, tokens, reports = [], {}, {}
    for _, kind, at, payload in (l[:4] for l in logs if len(l) >= 4):
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        if kind == "참가":
            name = data.get("name", "")
            if name and name not in tokens and name not in state.get("removed", []):
                tokens[name] = data.get("token", "")
                participants.append({"name": name, "division": data.get("division", ""), "joined_at": at})
        elif kind == "보고":
            reports[data.get("match")] = {**data, "at": at}

    state["participants"] = participants
    apply_groups(state)

    for bracket in state.get("brackets", {}).values():
        for match in bracket["matches"] + ([bracket["third"]] if bracket.get("third") else []):
            report = reports.get(match["id"])
            match["report"] = None if match["status"] == "confirmed" or not report else {
                k: report.get(k) for k in ("winner", "games", "by", "at")}

    state["_tokens"] = tokens
    return state


def apply_groups(state):
    # 참가자마다 그룹을 정한다 (운영진이 바꾼 것이 있으면 그것을 우선)
    for p in state["participants"]:
        auto_group, decided = group_for(p["division"], state["groups"])
        override = state.get("group_overrides", {}).get(p["name"])
        p["group"] = override or auto_group
        p["group_undecided"] = not decided and not override


def list_rooms():
    # 참가 화면의 방 목록: 이름·상태·참가 인원만 내보낸다. 코드는 참가가 끝난(진행 중·종료) 방만 — 접수 중인 방의 코드는 입장 암호다.
    rows, logs = _read_all()
    joined, removed_cache = {}, {}
    for l in logs[1:]:
        if len(l) >= 4 and l[1] == "참가":
            try:
                name = json.loads(l[3]).get("name", "")
            except ValueError:
                continue
            joined.setdefault(l[0].strip(), set()).add(name)
    rooms = []
    for row in rows[1:]:
        if len(row) < 5 or not row[4]:
            continue
        try:
            state = json.loads(row[4])
        except ValueError:
            continue
        if state.get("status") == "finished":
            try:
                finished = datetime.strptime(state.get("finished_at", state["updated_at"]), "%Y-%m-%d %H:%M:%S")
                if (datetime.now(_deps["kst"]).replace(tzinfo=None) - finished).days >= FINISHED_ROOM_DAYS:
                    continue
            except (ValueError, KeyError):
                continue
        count = len(joined.get(state["code"], set()) - set(state.get("removed", [])))
        rooms.append({
            "name": state["name"], "status": state["status"], "status_label": STATUS_LABELS.get(state["status"], state["status"]),
            "participants": count, "created_at": state["created_at"][:16],
            "groups": [g["name"] for g in state.get("groups", [])],
            "code": state["code"] if state["status"] != "lobby" else None,
        })
    rooms.sort(key=lambda r: r["created_at"], reverse=True)
    return rooms[:ROOM_LIST_LIMIT]


def public_view(state):
    # 참가자 화면에 보내는 상태: 운영 키·참가 토큰·내부 값은 뺀다
    view = {k: v for k, v in state.items() if k not in ("admin_key", "admin_code", "_tokens", "_row_no")}
    view["status_label"] = STATUS_LABELS.get(state["status"], state["status"])
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


def round_label(size, round_no, rounds):
    if round_no == rounds:
        return "결승"
    if round_no == rounds - 1:
        return "준결승"
    return f"{size >> (round_no - 1)}강"


def build_bracket(group_key, names, seed_mode, divisions, fmt):
    # names: 그룹 참가자 이름 (seed_mode가 'division'이면 부수 순, 아니면 무작위)
    names = list(names)
    if seed_mode == "division":
        names.sort(key=lambda n: (_deps["division_number"](divisions.get(n, "")) is None,
                                  _deps["division_number"](divisions.get(n, "")) or 0, n))
    else:
        random.shuffle(names)

    if len(names) == 1:
        return {"size": 1, "rounds": 0, "matches": [], "champion": names[0], "labels": {}, "third": None, "third_winner": None}

    size = 1
    while size < len(names):
        size *= 2
    rounds = size.bit_length() - 1
    slots = [names[seed - 1] if seed <= len(names) else None for seed in seed_order(size)]

    matches = []
    for r in range(1, rounds + 1):
        for i in range(size >> r):
            matches.append({
                "id": f"{group_key}-{r}-{i}", "round": r, "index": i,
                "players": [slots[2 * i], slots[2 * i + 1]] if r == 1 else [None, None],
                "winner": None, "games": None, "status": "waiting", "best_of": match_best_of(fmt, size >> (r - 1)),
                "next": f"{group_key}-{r + 1}-{i // 2}" if r < rounds else None, "slot": i % 2,
            })
    bracket = {"size": size, "rounds": rounds, "matches": matches, "champion": None, "third": None, "third_winner": None,
               "labels": {str(r): round_label(size, r, rounds) for r in range(1, rounds + 1)}}

    # 1라운드: 둘 다 있으면 경기 대기, 한쪽만 있으면 부전승
    by_id = {m["id"]: m for m in matches}
    for m in [m for m in matches if m["round"] == 1]:
        present = [p for p in m["players"] if p]
        if len(present) == 2:
            m["status"] = "pending"
        elif len(present) == 1:
            _advance(bracket, by_id, m, present[0], status="bye")
    return bracket


def _advance(bracket, by_id, match, winner, status="confirmed"):
    match["winner"] = winner
    match["status"] = status
    if match.get("third"):
        bracket["third_winner"] = winner
    elif match["next"]:
        nxt = by_id[match["next"]]
        nxt["players"][match["slot"]] = winner
        if all(nxt["players"]):
            nxt["status"] = "pending"
    else:
        bracket["champion"] = winner
    sync_third(bracket)


def sync_third(bracket):
    # 3·4위전이 켜져 있으면 준결승 패자를 채운다. 준결승이 부전승이면 그 자리는 비고, 한 명뿐이면 자동 3위.
    third = bracket.get("third")
    if not third or third["status"] in ("confirmed", "bye") or bracket["rounds"] < 2:
        return
    semis = [m for m in bracket["matches"] if m["round"] == bracket["rounds"] - 1]
    for sf in semis:
        third["players"][sf["index"]] = ([p for p in sf["players"] if p and p != sf["winner"]] or [None])[0] if sf["status"] == "confirmed" else None
    present = [p for p in third["players"] if p]
    if len(present) == 2:
        third["status"] = "pending"
    elif len(present) == 1 and all(sf["status"] in ("confirmed", "bye") for sf in semis):
        third["winner"], third["status"], bracket["third_winner"] = present[0], "bye", present[0]
    else:
        third["status"] = "waiting"


def set_third_place(bracket, group_key, fmt, enabled):
    if enabled:
        if bracket["rounds"] < 2:
            raise LeagueError("준결승이 없는 그룹에는 3·4위전을 둘 수 없습니다.")
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
        for match in bracket["matches"] + ([bracket["third"]] if bracket.get("third") else []):
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

    by_id = {m["id"]: m for m in bracket["matches"]}
    match["games"] = parsed
    match["report"] = None
    _advance(bracket, by_id, match, winner)
    refresh_status(state)
    return match, parsed


def reset_match(state, match_id):
    bracket, match = find_match(state, match_id)
    if match["status"] != "confirmed":
        raise LeagueError("확정된 경기만 되돌릴 수 있습니다.")
    by_id = {m["id"]: m for m in bracket["matches"]}
    third = bracket.get("third")
    if match.get("third"):
        bracket["third_winner"] = None
    elif match["next"]:
        nxt = by_id[match["next"]]
        if nxt["status"] in ("confirmed", "bye"):
            raise LeagueError("다음 경기가 이미 끝나 되돌릴 수 없습니다. 다음 경기부터 되돌리세요.")
        if third and third["status"] in ("confirmed", "bye") and match["round"] == bracket["rounds"] - 1:
            raise LeagueError("3·4위전이 이미 끝나 되돌릴 수 없습니다. 3·4위전부터 되돌리세요.")
        nxt["players"][match["slot"]] = None
        nxt["status"] = "waiting"
    else:
        bracket["champion"] = None
    match.update({"winner": None, "games": None, "status": "pending"})
    sync_third(bracket)
    refresh_status(state)
    return match


def start_tournament(state):
    if state["status"] != "lobby":
        raise LeagueError("이미 시작한 토너먼트입니다.")
    if len(state["participants"]) < 2:
        raise LeagueError("참가자가 2명 이상이어야 시작할 수 있습니다.")
    divisions = {p["name"]: p["division"] for p in state["participants"]}
    state["brackets"] = {}
    for g in state["groups"]:
        names = [p["name"] for p in state["participants"] if p["group"] == g["key"]]
        if names:
            state["brackets"][g["key"]] = build_bracket(g["key"], names, state["seed"], divisions, state["format"])
    state["status"] = "running"
    state["started_at"] = kst_now()
    refresh_status(state)


# ---------------------------------------------------------------------------
# 라우트
# ---------------------------------------------------------------------------
def _members():
    members, is_dummy = _deps["get_sheet_data"]()
    return {m["이름"]: m for m in members}, is_dummy


def _require_admin(state):
    key = request.headers.get("X-League-Key") or (request.get_json(silent=True) or {}).get("admin_key") or ""
    if not key or not secrets.compare_digest(key, state.get("admin_key", "")):
        raise LeagueError("운영진만 할 수 있는 작업입니다.", 403)


def _ok(state, **extra):
    return jsonify({"tournament": public_view(state), **extra})


@league_bp.errorhandler(LeagueError)
def _handle_error(error):
    return jsonify({"error": str(error)}), error.status


@league_bp.errorhandler(Exception)
def _handle_unexpected(error):
    import gspread

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
    return render_template("league/admin.html", code=normalize_code(request.args.get("code", "")))


@league_bp.route("/league/<code>")
def league_room(code):
    return render_template("league/room.html", code=normalize_code(code), is_admin=False)


@league_bp.route("/league/<code>/admin")
def league_admin(code):
    return render_template("league/room.html", code=normalize_code(code), is_admin=True)


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
    if best_of_from not in (0, 2, 4, 8, 16, 32):
        raise LeagueError("5판 3선 전환 시점이 올바르지 않습니다.")
    seed = data.get("seed", "random")
    if seed not in ("random", "division"):
        raise LeagueError("시드 방식이 올바르지 않습니다.")
    groups = _parse_groups(data.get("groups"))

    rows, _ = _read_all(force=True)
    used = {r[0].strip() for r in rows[1:] if r}
    code = new_code(used)
    admin_code = new_code(used | {code})   # 참가 코드와 다른 관리자 코드
    state = {
        "code": code, "name": name, "status": "lobby", "created_at": kst_now(), "updated_at": kst_now(),
        "admin_key": secrets.token_urlsafe(18), "admin_code": admin_code,
        "format": {"target": target, "best_of": best_of, "best_of_from": best_of_from},
        "groups": groups, "seed": seed, "group_overrides": {}, "removed": [], "brackets": {},
    }
    save_state(state)
    return jsonify({"code": code, "admin_key": state["admin_key"], "admin_code": admin_code,
                    "admin_url": url_for("league.league_admin", code=code, key=state["admin_key"]),
                    "join_url": url_for("league.league_room", code=code)})


@league_bp.route("/api/league/<code>/admin-login", methods=["POST"])
def api_admin_login(code):
    # 관리자 코드를 맞히면 이 기기에 운영 키를 내준다 (다른 운영진 폰에서도 운영 화면을 열 수 있게)
    state = load_state(code)
    now = time.monotonic()
    while _admin_logins and now - _admin_logins[0] > 600:
        _admin_logins.popleft()
    if len(_admin_logins) >= ADMIN_LOGIN_LIMIT:
        raise LeagueError("관리자 코드 시도가 너무 많습니다. 잠시 후 다시 해 주세요.", 429)
    _admin_logins.append(now)
    admin_code = normalize_code((request.get_json(silent=True) or {}).get("admin_code", ""))
    if not admin_code or not secrets.compare_digest(admin_code, state.get("admin_code", "")):
        raise LeagueError("관리자 코드가 맞지 않습니다.", 403)
    return jsonify({"admin_key": state["admin_key"], "admin_code": state["admin_code"],
                    "admin_url": url_for("league.league_admin", code=state["code"])})


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
    if best_of_from not in (0, 2, 4, 8, 16, 32):
        raise LeagueError("5판 3선 전환 시점이 올바르지 않습니다.")
    _check_write_limit()
    state["format"]["best_of_from"] = best_of_from if state["format"]["best_of"] == 3 else 0
    for bracket in state["brackets"].values():
        for m in bracket["matches"] + ([bracket["third"]] if bracket.get("third") else []):
            if m["status"] not in ("confirmed", "bye"):
                m["best_of"] = match_best_of(state["format"], 2 if m.get("third") else bracket["size"] >> (m["round"] - 1))
    save_state(state)
    return _ok(state)


@league_bp.route("/api/league/<code>/third-place", methods=["POST"])
def api_third_place(code):
    # 운영진: 그룹별 3·4위전을 진행 중에 켜거나 끈다
    state = load_state(code, force=True)
    _require_admin(state)
    if state["status"] not in ("running", "finished"):
        raise LeagueError("시작한 뒤에 정할 수 있습니다.")
    data = request.get_json(silent=True) or {}
    group = data.get("group")
    if group not in state["brackets"]:
        raise LeagueError("그룹이 올바르지 않습니다.")
    _check_write_limit()
    set_third_place(state["brackets"][group], group, state["format"], bool(data.get("enabled")))
    refresh_status(state)
    save_state(state)
    return _ok(state)


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


@league_bp.route("/api/league/<code>/join", methods=["POST"])
def api_join(code):
    state = load_state(code)
    if state["status"] != "lobby":
        raise LeagueError("참가 접수가 끝난 토너먼트입니다.")
    _check_write_limit()
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
    if name in state["_tokens"]:
        raise LeagueError("이미 참가한 이름입니다. 본인이 맞다면 처음 참가한 기기에서 열어 주세요.", 409)
    token = secrets.token_urlsafe(12)
    append_log(state["code"], "참가", {"name": name, "division": members[name]["부수"], "token": token})
    # 방금 들어온 사람을 응답에 바로 넣어 준다 (다시 읽지 않음)
    state["participants"].append({"name": name, "division": members[name]["부수"], "joined_at": kst_now()})
    apply_groups(state)
    return jsonify({"token": token, "name": name, "tournament": public_view(state)})


@league_bp.route("/api/league/<code>/participants", methods=["POST"])
def api_participants(code):
    # 운영진: 시작 전 그룹 조정 또는 참가자 제외
    state = load_state(code, force=True)
    _require_admin(state)
    if state["status"] != "lobby":
        raise LeagueError("시작한 뒤에는 참가자를 바꿀 수 없습니다.")
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if name not in {p["name"] for p in state["participants"]}:
        raise LeagueError("참가자 목록에 없는 이름입니다.")
    if data.get("remove"):
        state["removed"].append(name)
        state["group_overrides"].pop(name, None)
    else:
        group = data.get("group")
        if group not in {g["key"] for g in state["groups"]}:
            raise LeagueError("그룹이 올바르지 않습니다.")
        state["group_overrides"][name] = group
    if data.get("remove"):
        state["participants"] = [p for p in state["participants"] if p["name"] != name]
    apply_groups(state)
    save_state(state)
    return _ok(state)


@league_bp.route("/api/league/<code>/start", methods=["POST"])
def api_start(code):
    state = load_state(code, force=True)
    _require_admin(state)
    start_tournament(state)
    save_state(state)
    return _ok(state)


@league_bp.route("/api/league/<code>/matches/<match_id>/report", methods=["POST"])
def api_report(code, match_id):
    # 선수(참가 토큰) 또는 점수판이 결과를 보고한다. 운영 키가 함께 오면 바로 확정한다.
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

    admin_key = request.headers.get("X-League-Key") or data.get("admin_key") or ""
    _check_write_limit()
    if admin_key and secrets.compare_digest(admin_key, state["admin_key"]):
        return _confirm_and_save(state, match_id, winner, data.get("games"), by="운영진")

    token = str(data.get("token", ""))
    reporter = next((n for n, t in state["_tokens"].items() if t and secrets.compare_digest(t, token)), None)
    if reporter not in match["players"]:
        raise LeagueError("이 경기의 선수만 결과를 보낼 수 있습니다.", 403)
    append_log(state["code"], "보고", {"match": match_id, "winner": winner, "games": data.get("games") or None, "by": reporter})
    match["report"] = {"winner": winner, "games": data.get("games") or None, "by": reporter, "at": kst_now()}
    return _ok(state, reported=True)


@league_bp.route("/api/league/<code>/matches/<match_id>/confirm", methods=["POST"])
def api_confirm(code, match_id):
    state = load_state(code, force=True)
    _require_admin(state)
    if state["status"] != "running":
        raise LeagueError("진행 중인 토너먼트가 아닙니다.")
    data = request.get_json(silent=True) or {}
    _, match = find_match(state, match_id)
    # 승자를 따로 주지 않으면 선수가 보고한 결과대로 확정한다
    winner = str(data.get("winner") or (match.get("report") or {}).get("winner") or "").strip()
    games = data.get("games") if "games" in data else (match.get("report") or {}).get("games")
    _check_write_limit()
    return _confirm_and_save(state, match_id, winner, games, by="운영진")


def _confirm_and_save(state, match_id, winner, games, by):
    match, parsed = confirm_match(state, match_id, winner, games)
    save_state(state)
    # 게임 점수까지 있으면 전적(경기기록)에도 남긴다
    if parsed:
        a, b = match["players"]
        wins = [sum(1 for x, y in parsed if x > y), sum(1 for x, y in parsed if y > x)]
        target, best_of = state["format"]["target"], match["best_of"]
        row = [kst_now()[:16], a, b, str(wins[0]), str(wins[1]), winner,
               f"{target}점 {best_of}판 {best_of // 2 + 1}선 · 리그전 {state['name']}",
               ", ".join(f"{x}:{y}" for x, y in parsed)]
        try:
            _deps["record_match"](row)
        except Exception as e:  # 전적 기록 실패는 토너먼트 진행을 막지 않는다
            _deps["logger"].error("리그전 결과의 전적 기록 실패: %s", e)
    return _ok(state, confirmed=True)


@league_bp.route("/api/league/<code>/matches/<match_id>/reset", methods=["POST"])
def api_reset(code, match_id):
    state = load_state(code, force=True)
    _require_admin(state)
    _check_write_limit()
    reset_match(state, match_id)
    save_state(state)
    return _ok(state)


@league_bp.route("/api/league/<code>/delete", methods=["POST"])
def api_delete(code):
    state = load_state(code, force=True)
    _require_admin(state)
    _check_write_limit()
    delete_tournament(state["code"])
    return jsonify({"deleted": True})
