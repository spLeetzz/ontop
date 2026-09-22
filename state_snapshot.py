"""Crash-safe registration state snapshot.

The bot keeps live registration state only in memory (constants.*).
If the process dies mid-registration everything is lost. This module
persists that state to state_snapshot.json after every successful
mutation (booking / cancel / claim / reset) with an atomic
write (tmp file + os.replace), and restores it in on_ready.

Design constraints:
- Never raise: both save and load swallow all exceptions and only print.
  Registration must never break because snapshotting failed.
- No discord imports: pure stdlib so it can be tested standalone.
- Handles config growth: if SLOTS_LIMIT changed since the snapshot
  was written, lobby lists are padded/truncated to the current size.
"""

import datetime
import json
import os

SNAPSHOT_VERSION = 1


def _snapshot_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "state_snapshot.json")


def save_state_snapshot():
    """Write current in-memory registration state to disk atomically. Never raises."""
    try:
        from constants import constants

        def _norm_teams(d):
            # registered_teams values are sheet rows (lists of str); keep as-is if JSON-safe
            out = {}
            for k, v in (d or {}).items():
                try:
                    json.dumps(v)
                    out[k] = v
                except Exception:
                    out[k] = [str(x) for x in v] if isinstance(v, (list, tuple)) else str(v)
            return out

        payload = {
            "version": SNAPSHOT_VERSION,
            "saved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "open": {
                "slots_limit": constants.SLOTS_LIMIT,
                "lobby_size": constants.LOBBY_SIZE,
                "registered_set": [list(e) for e in (constants.registered_set or set())],
                "registered_teams": _norm_teams(constants.registered_teams),
                "lobby_teams": [dict(l) for l in (constants.lobby_teams or [])],
                "disabled_status": bool(constants.disabled_status),
                "captcha": list(constants.captcha_question_variables or []),
                "temp_json": dict(constants.temp_json_dict or {}),
            },
            "t3": {
                "slots_limit": constants.SPECIAL_SLOTS_LIMIT,
                "lobby_size": constants.SPECIAL_LOBBY_SIZE,
                "registered_set": [list(e) for e in (constants.special_registered_set or set())],
                "registered_teams": _norm_teams(constants.special_registered_teams),
                "lobby_teams": [dict(l) for l in (constants.special_lobby_teams or [])],
                "disabled_status": bool(constants.special_disabled_status),
                "temp_json": dict(constants.temp_json_dict2 or {}),
            },
        }

        path = _snapshot_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, path)
    except Exception as e:
        print(f"[snapshot] save failed (non-fatal): {e}")


def _fit_lobbies(loaded, expected):
    """Pad with {} or truncate to expected length. Never raises."""
    try:
        loaded = [dict(l) if isinstance(l, dict) else {} for l in (loaded or [])]
    except Exception:
        loaded = []
    if len(loaded) < expected:
        loaded = loaded + [{} for _ in range(expected - len(loaded))]
    elif len(loaded) > expected:
        loaded = loaded[:expected]
    return loaded


def load_state_snapshot():
    """Restore in-memory state from disk. Returns True on success, False otherwise. Never raises."""
    try:
        from constants import constants

        path = _snapshot_path()
        if not os.path.exists(path):
            return False
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        if not isinstance(payload, dict):
            print("[snapshot] invalid payload, ignoring")
            return False

        expected_open = int(constants.SLOTS_LIMIT) // int(constants.LOBBY_SIZE)
        expected_t3 = int(constants.SPECIAL_SLOTS_LIMIT) // int(constants.SPECIAL_LOBBY_SIZE)

        data_open = payload.get("open", {}) or {}
        data_t3 = payload.get("t3", {}) or {}

        # Open
        try:
            constants.registered_set = set(
                tuple(e) for e in (data_open.get("registered_set") or []) if isinstance(e, list)
            )
        except Exception:
            constants.registered_set = set()
        try:
            constants.registered_teams = dict(data_open.get("registered_teams") or {})
        except Exception:
            constants.registered_teams = {}
        constants.lobby_teams = _fit_lobbies(data_open.get("lobby_teams"), expected_open)
        constants.disabled_status = bool(data_open.get("disabled_status", constants.disabled_status))
        if isinstance(data_open.get("captcha"), list) and data_open["captcha"]:
            try:
                constants.captcha_question_variables.clear()
                constants.captcha_question_variables.extend(data_open["captcha"])
            except Exception:
                pass
        if isinstance(data_open.get("temp_json"), dict):
            try:
                constants.temp_json_dict.clear()
                constants.temp_json_dict.update(data_open["temp_json"])
            except Exception:
                pass

        # T3
        try:
            constants.special_registered_set = set(
                tuple(e) for e in (data_t3.get("registered_set") or []) if isinstance(e, list)
            )
        except Exception:
            constants.special_registered_set = set()
        try:
            constants.special_registered_teams = dict(data_t3.get("registered_teams") or {})
        except Exception:
            constants.special_registered_teams = {}
        constants.special_lobby_teams = _fit_lobbies(data_t3.get("lobby_teams"), expected_t3)
        constants.special_disabled_status = bool(
            data_t3.get("disabled_status", constants.special_disabled_status)
        )
        if isinstance(data_t3.get("temp_json"), dict):
            try:
                constants.temp_json_dict2.clear()
                constants.temp_json_dict2.update(data_t3["temp_json"])
            except Exception:
                pass

        print(
            f"[snapshot] restored open={len(constants.registered_set)} "
            f"t3={len(constants.special_registered_set)} "
            f"from {payload.get('saved_at')}"
        )
        return True
    except Exception as e:
        print(f"[snapshot] load failed (non-fatal): {e}")
        return False
