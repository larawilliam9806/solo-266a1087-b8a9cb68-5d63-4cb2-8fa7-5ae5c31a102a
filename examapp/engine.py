"""Form/answer parsing and the item-analysis engine.

The engine is deliberately pure: it takes the current forms, answer rows and
human review decisions, and returns a complete, deterministic snapshot.  The
caller persists that snapshot in one transaction together with the change that
triggered it, so forms, mapping corrections, review decisions and statistics
always become visible at the same moment.
"""
from __future__ import annotations

import csv
import io
import re
from collections import OrderedDict


class ValidationError(ValueError):
    """Raised for bad CSV input or an invalid form/mapping edit."""


# --------------------------------------------------------------------------
# CSV parsing
# --------------------------------------------------------------------------

FORM_HEADERS = {
    "q_no": {"题号", "卷式题号", "印刷题号", "q_no", "qno", "question", "题号\n"},
    "letter": {"选项字母", "字母", "涂选字母", "option", "letter", "选项"},
    "std_code": {"标准题码", "题码", "标准题号", "std_code", "code", "standard_code"},
    "std_option": {"标准选项", "标准答案选项", "std_option", "standard_option"},
}
ANSWER_HEADERS = {
    "card_no": {"卡号", "答题卡号", "卡编号", "card_no", "card", "card_id"},
    "q_no": {"题号", "卷式题号", "印刷题号", "q_no", "qno", "question"},
    "letter": {"涂选字母", "选项字母", "字母", "letter", "option", "作答"},
    "scanned_code": {"实扫题码", "扫描题码", "实扫题号", "scanned_code", "scan_code", "scanned"},
}
SCORE_HEADERS = {
    "std_code": {"标准题码", "题码", "标准题号", "std_code", "code", "standard_code"},
    "std_option": {"标准选项", "正确标准选项", "标准答案", "答案",
                   "std_option", "standard_option", "answer"},
    "points": {"分值", "正整数分值", "分数", "得分", "points", "score", "weight"},
}


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValidationError("无法识别文件编码，请使用 UTF-8 或 GBK 编码的 CSV")


def _norm(value) -> str:
    return (value or "").strip()


def _read_rows(raw: bytes, spec: dict, kind: str):
    """Return (data_rows, text) so callers can persist the decoded CSV."""
    text = _decode(raw)
    reader = csv.reader(io.StringIO(text))
    rows = [[_norm(c) for c in r] for r in reader if any(_norm(c) for c in r)]
    if not rows:
        raise ValidationError(f"{kind}为空")

    def locate(header):
        found = {}
        for idx, cell in enumerate(header):
            key = cell.lower().replace(" ", "")
            if not key:
                continue
            for field, aliases in spec.items():
                if key in {a.lower() for a in aliases} and field not in found:
                    found[field] = idx
        return found

    positions = locate(rows[0])
    if positions:
        missing = [f for f in spec if f not in positions]
        if missing:
            raise ValidationError(f"{kind}缺少列：{', '.join(missing)}")
        data_rows = rows[1:]
    else:
        # Headerless file: accept the canonical column order.
        positions = {field: i for i, field in enumerate(spec)}
        data_rows = rows

    out = []
    for r in data_rows:
        out.append(tuple(r[positions[f]] if positions[f] < len(r) else "" for f in spec))
    return out, text


def decode_csv(raw: bytes) -> str:
    """Decode an uploaded CSV to text so job payloads stay UTF-8 safe."""
    return _decode(raw)


def validate_form_rows(rows):
    """Validate one form's mapping rows. Returns the same rows on success."""
    seen = set()
    q_to_code: dict[str, str] = {}
    code_to_q: dict[str, str] = {}
    code_letters: dict[str, dict[str, str]] = {}
    for q_no, letter, std_code, std_option in rows:
        if not q_no or not letter:
            raise ValidationError(f"卷式行缺少题号或选项字母：{q_no or '?'}/{letter or '?'}")
        if not std_code or not std_option:
            raise ValidationError(f"卷式行缺少标准题码或标准选项：{q_no}/{letter}")
        if (q_no, letter) in seen:
            raise ValidationError(f"卷式内题号 {q_no} 的选项 {letter} 重复")
        seen.add((q_no, letter))
        if q_to_code.setdefault(q_no, std_code) != std_code:
            raise ValidationError(
                f"卷式内题号 {q_no} 映射到多个标准题码（{q_to_code[q_no]} / {std_code}）"
            )
        if code_to_q.setdefault(std_code, q_no) != q_no:
            raise ValidationError(
                f"标准题码 {std_code} 在本卷出现多次（题号 {code_to_q[std_code]} / {q_no}）"
            )
        letters = code_letters.setdefault(std_code, {})
        if std_option in letters:
            raise ValidationError(
                f"标准题码 {std_code} 的标准选项 {std_option} 重复"
            )
        letters[std_option] = letter
    return rows


def parse_form_csv(raw: bytes):
    """Return [(q_no, letter, std_code, std_option), ...] validated per form."""
    rows, _ = _read_rows(raw, FORM_HEADERS, "卷式 CSV")
    if not rows:
        raise ValidationError("卷式 CSV 没有数据行")
    return validate_form_rows(rows)


def parse_answer_csv(raw: bytes):
    """Return [{card_no, q_no, letter, scanned_code}, ...]."""
    rows, _ = _read_rows(raw, ANSWER_HEADERS, "答题 CSV")
    if not rows:
        raise ValidationError("答题 CSV 没有数据行")
    seen = set()
    out = []
    for card_no, q_no, letter, scanned in rows:
        if not card_no or not q_no:
            raise ValidationError("答题行缺少卡号或题号")
        if (card_no, q_no) in seen:
            raise ValidationError(f"卡号 {card_no} 的题号 {q_no} 出现多行")
        seen.add((card_no, q_no))
        out.append(
            {"card_no": card_no, "q_no": q_no, "letter": letter, "scanned_code": scanned}
        )
    return out


def parse_score_csv(raw: bytes):
    """Return [{std_code, std_option, points}, ...].

    Each standard code appears exactly once, with a correct standard option
    and a positive-integer point value.
    """
    rows, _ = _read_rows(raw, SCORE_HEADERS, "计分 CSV")
    if not rows:
        raise ValidationError("计分 CSV 没有数据行")
    seen: set[str] = set()
    out = []
    for std_code, std_option, points in rows:
        if not std_code:
            raise ValidationError("计分行缺少标准题码")
        if std_code in seen:
            raise ValidationError(f"标准题码 {std_code} 在计分表中重复，每个题码只能填一次")
        if not std_option:
            raise ValidationError(f"题码 {std_code} 缺少正确标准选项")
        value = points.strip()
        if not value or not value.isdigit():
            raise ValidationError(f"题码 {std_code} 的分值必须是正整数")
        n = int(value)
        if n <= 0:
            raise ValidationError(f"题码 {std_code} 的分值必须是正整数")
        seen.add(std_code)
        out.append({"std_code": std_code, "std_option": std_option, "points": n})
    return out


def parse_pass_line(value: str) -> int:
    """Validate the pass-line form field: a positive integer."""
    text = (value or "").strip()
    if not text or not text.isdigit():
        raise ValidationError("及格线必须是正整数")
    n = int(text)
    if n <= 0:
        raise ValidationError("及格线必须是正整数")
    return n


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def _natural_key(text: str):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", text)]


def build_forms(form_rows) -> "OrderedDict[int, dict]":
    """form_rows: iterable of (form_id, form_name, q_no, letter, std_code, std_option)."""
    forms: "OrderedDict[int, dict]" = OrderedDict()
    for form_id, form_name, q_no, letter, std_code, std_option in form_rows:
        form = forms.setdefault(
            form_id,
            {"id": form_id, "name": form_name, "qmap": {}, "codes": {},
             "options": OrderedDict()},
        )
        entry = form["qmap"].setdefault(
            q_no, {"std_code": std_code, "letters": {}, "rev": {}}
        )
        entry["letters"][letter] = std_option
        entry["rev"][std_option] = letter
        form["codes"][std_code] = q_no
        form["options"].setdefault(std_code, set()).add(std_option)
    return forms


def form_is_compatible(form: dict, card_rows: dict) -> bool:
    """Full-card question numbers match and every non-empty code/letter fits."""
    if set(form["qmap"].keys()) != set(card_rows.keys()):
        return False
    for q_no, row in card_rows.items():
        mapping = form["qmap"][q_no]
        if row["scanned_code"] and mapping["std_code"] != row["scanned_code"]:
            return False
        if row["letter"] and row["letter"] not in mapping["letters"]:
            return False
    return True


class Analysis:
    def __init__(self):
        self.question_codes: list[str] = []
        self.options: dict[str, list[str]] = {}
        self.stats: dict[str, dict] = {}
        self.card_results: list[dict] = []
        self.unmatched_cards: list[dict] = []
        self.flagged: list[dict] = []
        self.cards_total = 0
        self.cards_matched = 0

    def summary(self) -> dict:
        return {
            "cards_total": self.cards_total,
            "cards_matched": self.cards_matched,
            "cards_unmatched": len(self.unmatched_cards),
            "questions": len(self.question_codes),
            "flagged_cells": len(self.flagged),
        }


def analyse(forms, card_rows_by_card: dict, decisions: dict) -> Analysis:
    """Compute the whole analysis snapshot.

    forms: OrderedDict from build_forms
    card_rows_by_card: {card_no: {q_no: {"letter":..., "scanned_code":...}}}
    decisions: {(card_no, std_code): {"resolution":..., "note":...}}
    """
    result = Analysis()

    # Standard questions and their option universe are defined by live forms.
    codes = set()
    options: dict[str, set[str]] = {}
    for form in forms.values():
        for code, letters in form["options"].items():
            codes.add(code)
            options.setdefault(code, set()).update(letters)
    result.question_codes = sorted(codes, key=_natural_key)
    result.options = {c: sorted(options[c]) for c in result.question_codes}

    all_forms = list(forms.values())
    for code in result.question_codes:
        result.stats[code] = {
            "counted": 0, "blank": 0, "flagged": 0,
            "options": {o: 0 for o in result.options[code]},
        }

    result.cards_total = len(card_rows_by_card)
    for card_no in sorted(card_rows_by_card):
        rows = card_rows_by_card[card_no]
        compatible = [f for f in all_forms if form_is_compatible(f, rows)]
        if not compatible:
            detail = "无可相容卷式（整卡题号不一致或非空题码/涂选字母不吻合）"
            result.unmatched_cards.append({"card_no": card_no, "detail": detail})
            result.card_results.append(
                {"card_no": card_no, "std_code": "", "status": "unmatched",
                 "std_option": None, "detail": detail}
            )
            continue
        result.cards_matched += 1
        form_names = "/".join(f["name"] for f in compatible)

        for code in result.question_codes:
            stat = result.stats[code]
            decision = decisions.get((card_no, code))

            containing = [f for f in compatible if code in f["codes"]]
            if not containing:
                # No compatible form printed this question: skip entirely.
                continue

            answers = []
            for form in containing:
                q_no = form["codes"][code]
                letter = rows[q_no]["letter"]
                if letter:
                    std_option = form["qmap"][q_no]["letters"][letter]
                else:
                    std_option = ""
                answers.append((form["name"], letter, std_option))

            if len(containing) != len(compatible):
                missing = [f["name"] for f in compatible if code not in f["codes"]]
                status, option, detail = (
                    "flagged", None,
                    f"部分相容卷式未出该题：{'/'.join(missing)}",
                )
            else:
                distinct = {a[2] for a in answers}
                if len(distinct) == 1:
                    option = answers[0][2]
                    if option:
                        status, detail = "counted", f"相容卷式：{form_names}"
                    else:
                        status, detail = "blank", f"相容卷式：{form_names}（空答）"
                else:
                    shown = "；".join(
                        f"{name}: {letter or '空'}" for name, letter, _ in answers
                    )
                    status, option, detail = (
                        "flagged", None, f"卷式归一作答分歧：{shown}"
                    )

            if decision and status in ("flagged", "blank"):
                # A human conclusion is only meaningful where the automatic
                # verdict could not be trusted (disagreement / unprinted).
                # Once a correction makes the cell consistent, recalculated
                # statistics win and the stale conclusion stops applying.
                res = decision["resolution"]
                if res == "skip":
                    status, option, detail = "skipped", None, (
                        "人工核对：跳过（不计入）" +
                        (f"；{decision['note']}" if decision.get("note") else "")
                    )
                elif res == "blank":
                    status, option, detail = "blank", None, (
                        "人工核对：漏答" +
                        (f"；{decision['note']}" if decision.get("note") else "")
                    )
                elif res.startswith("opt:") and res[4:] in stat["options"]:
                    chosen = res[4:]
                    status, option, detail = "counted", chosen, (
                        "人工核对：" + chosen +
                        (f"；{decision['note']}" if decision.get("note") else "")
                    )

            if status == "skipped":
                continue
            if status == "counted":
                stat["counted"] += 1
                stat["options"][option] += 1
            elif status == "blank":
                stat["blank"] += 1
            else:
                stat["flagged"] += 1
                result.flagged.append(
                    {"card_no": card_no, "std_code": code, "detail": detail}
                )
            result.card_results.append(
                {"card_no": card_no, "std_code": code, "status": status,
                 "std_option": option, "detail": detail}
            )

    result.flagged.sort(key=lambda x: (_natural_key(x["card_no"]), _natural_key(x["std_code"])))
    return result


# --------------------------------------------------------------------------
# Grading (成绩划线)
# --------------------------------------------------------------------------

class GradeResult:
    def __init__(self):
        # [{std_code, std_option, points}] that no live form supports.
        self.invalid_items: list[dict] = []
        # One entry per card that has at least one compatible form.
        self.grades: list[dict] = []
        # Cards with no compatible form, listed separately.
        self.unmatched_cards: list[str] = []
        self.paused = False

    def summary(self) -> dict:
        verdicts = {"pass": 0, "fail": 0, "rescan": 0}
        for g in self.grades:
            verdicts[g["verdict"]] += 1
        return {
            "graded_cards": len(self.grades),
            "unmatched_cards": len(self.unmatched_cards),
            "invalid_items": len(self.invalid_items),
            **verdicts,
        }


def find_invalid_score_items(forms, score_items: list[dict]) -> list[dict]:
    """Score items whose (code, correct option) pair no form provides."""
    valid_pairs = set()
    for form in forms.values():
        for code, opts in form["options"].items():
            for opt in opts:
                valid_pairs.add((code, opt))
    return [
        item for item in score_items
        if (item["std_code"], item["std_option"]) not in valid_pairs
    ]


def grade(forms, card_rows_by_card: dict, score_items: list[dict],
          pass_line: int) -> GradeResult:
    """Compute per-form whole-card totals and pass/fail verdicts.

    For each compatible form the card's scanned answers are scored
    independently: a blank, a wrong option, or a question the form does not
    print all earn zero for that item.
    """
    result = GradeResult()
    result.invalid_items = find_invalid_score_items(forms, score_items)
    if result.invalid_items:
        # Item statistics are still published; the grades page must wait.
        result.paused = True
        return result

    points = {item["std_code"]: item for item in score_items}
    all_forms = list(forms.values())

    for card_no in sorted(card_rows_by_card, key=_natural_key):
        rows = card_rows_by_card[card_no]
        compatible = [f for f in all_forms if form_is_compatible(f, rows)]
        if not compatible:
            result.unmatched_cards.append(card_no)
            continue

        per_form: dict[str, int] = {}
        for form in compatible:
            total = 0
            for code, item in points.items():
                q_no = form["codes"].get(code)
                if q_no is None:
                    continue  # form does not print this question -> 0
                letter = rows[q_no]["letter"]
                if not letter:
                    continue  # blank -> 0
                std_option = form["qmap"][q_no]["letters"].get(letter)
                if std_option == item["std_option"]:
                    total += item["points"]
                # wrong option -> 0
            per_form[form["name"]] = total

        # First form achieving each extremum, in stable form order.
        min_form = min(per_form, key=lambda n: per_form[n])
        max_form = max(per_form, key=lambda n: per_form[n])
        min_score, max_score = per_form[min_form], per_form[max_form]
        if min_score >= pass_line:
            verdict = "pass"
        elif max_score < pass_line:
            verdict = "fail"
        else:
            verdict = "rescan"
        result.grades.append({
            "card_no": card_no,
            "min_score": min_score,
            "max_score": max_score,
            "min_form": min_form,
            "max_form": max_form,
            "verdict": verdict,
            "scores": per_form,
        })
    return result


# --------------------------------------------------------------------------
# DB loading + persistence
# --------------------------------------------------------------------------

def load_forms(conn):
    rows = conn.execute(
        "SELECT f.id, f.name, m.q_no, m.letter, m.std_code, m.std_option "
        "FROM forms f JOIN form_mappings m ON m.form_id = f.id "
        "ORDER BY f.id, m.q_no, m.letter"
    ).fetchall()
    return build_forms(
        [(r["id"], r["name"], r["q_no"], r["letter"], r["std_code"], r["std_option"])
         for r in rows]
    )


def load_card_rows(conn):
    cards: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT card_no, q_no, letter, scanned_code FROM answer_rows "
        "ORDER BY card_no, q_no"
    ):
        cards.setdefault(r["card_no"], {})[r["q_no"]] = {
            "letter": r["letter"], "scanned_code": r["scanned_code"],
        }
    return cards


def load_decisions(conn):
    return {
        (r["card_no"], r["std_code"]): {
            "resolution": r["resolution"], "note": r["note"],
        }
        for r in conn.execute("SELECT card_no, std_code, resolution, note FROM review_decisions")
    }


def compute(conn) -> Analysis:
    return analyse(load_forms(conn), load_card_rows(conn), load_decisions(conn))


# --------------------------------------------------------------------------
# Score-table loading + grade persistence
# --------------------------------------------------------------------------

def load_active_score(conn):
    """Return (score_id, pass_line, items) for the active table, or None."""
    from .db import get_meta
    try:
        score_id = int(get_meta(conn, "active_score_id", "0"))
    except (TypeError, ValueError):
        score_id = 0
    if not score_id:
        return None
    table = conn.execute(
        "SELECT id, pass_line FROM score_tables WHERE id = ?", (score_id,)
    ).fetchone()
    if table is None:
        return None
    items = [
        {"std_code": r["std_code"], "std_option": r["std_option"],
         "points": r["points"]}
        for r in conn.execute(
            "SELECT std_code, std_option, points FROM score_items "
            "WHERE score_id = ? ORDER BY std_code",
            (score_id,),
        )
    ]
    return table["id"], table["pass_line"], items


def compute_grades(conn) -> GradeResult | None:
    """Grade the current state against the active score table, or None."""
    loaded = load_active_score(conn)
    if loaded is None:
        return None
    score_id, pass_line, items = loaded
    return grade(load_forms(conn), load_card_rows(conn), items, pass_line)


def replace_grades(conn, run_id: int, score_id: int, pass_line: int,
                   grades: GradeResult, graded_at: str) -> None:
    """Persist grading output for one snapshot. Caller owns the transaction."""
    import json
    conn.execute("DELETE FROM run_grades_meta WHERE run_id = ?", (run_id,))
    conn.execute("DELETE FROM run_grades WHERE run_id = ?", (run_id,))
    conn.execute(
        "INSERT INTO run_grades_meta(run_id, score_id, pass_line, paused, "
        "invalid_json, graded_at) VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, score_id, pass_line, 1 if grades.paused else 0,
         json.dumps(grades.invalid_items, ensure_ascii=False), graded_at),
    )
    if grades.paused:
        return
    conn.executemany(
        "INSERT INTO run_grades(run_id, card_no, min_score, max_score, "
        "min_form, max_form, verdict, scores_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(run_id, g["card_no"], g["min_score"], g["max_score"],
          g["min_form"], g["max_form"], g["verdict"],
          json.dumps(g["scores"], ensure_ascii=False))
         for g in grades.grades],
    )


def persist_run(conn, analysis: Analysis, created_at: str) -> int:
    """Insert a full snapshot and return its run id. Caller owns the transaction."""
    cur = conn.execute(
        "INSERT INTO runs(created_at, summary_json) VALUES (?, '{}')", (created_at,)
    )
    run_id = cur.lastrowid
    import json
    conn.execute(
        "UPDATE runs SET summary_json = ? WHERE id = ?",
        (json.dumps(analysis.summary(), ensure_ascii=False), run_id),
    )
    conn.executemany(
        "INSERT INTO run_question_stats(run_id, std_code, counted, blank, flagged) "
        "VALUES (?, ?, ?, ?, ?)",
        [(run_id, code, st["counted"], st["blank"], st["flagged"])
         for code, st in analysis.stats.items()],
    )
    conn.executemany(
        "INSERT INTO run_option_counts(run_id, std_code, std_option, cnt) VALUES (?, ?, ?, ?)",
        [(run_id, code, opt, cnt)
         for code, st in analysis.stats.items() for opt, cnt in st["options"].items()],
    )
    conn.executemany(
        "INSERT INTO run_card_results(run_id, card_no, std_code, status, std_option, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(run_id, r["card_no"], r["std_code"], r["status"], r["std_option"], r["detail"])
         for r in analysis.card_results],
    )
    conn.executemany(
        "INSERT INTO run_unmatched(run_id, card_no, detail) VALUES (?, ?, ?)",
        [(run_id, u["card_no"], u["detail"]) for u in analysis.unmatched_cards],
    )
    return run_id


def prune_runs(conn, keep: int = 4) -> None:
    rows = conn.execute(
        "SELECT id FROM runs ORDER BY id DESC LIMIT -1 OFFSET ?", (keep,)
    ).fetchall()
    for r in rows:
        conn.execute("DELETE FROM runs WHERE id = ?", (r["id"],))
