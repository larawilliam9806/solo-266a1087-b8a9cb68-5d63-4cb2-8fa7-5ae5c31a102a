import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from examapp import db, engine  # noqa: E402
from examapp.app import create_app, read_grades, read_run  # noqa: E402

FORM_HEADER = "题号,选项字母,标准题码,标准选项\n"
ANSWER_HEADER = "卡号,题号,涂选字母,实扫题码\n"
SCORE_HEADER = "标准题码,正确标准选项,分值\n"


def form_csv(rows):
    return FORM_HEADER + "".join(f"{q},{l},{c},{o}\n" for q, l, c, o in rows)


def answer_csv(rows):
    return ANSWER_HEADER + "".join(
        f"{card},{q},{letter},{code}\n" for card, q, letter, code in rows
    )


def score_csv(rows):
    return SCORE_HEADER + "".join(f"{c},{o},{p}\n" for c, o, p in rows)


A_FORM = form_csv([
    ("1", "A", "Q1", "A"), ("1", "B", "Q1", "B"),
    ("2", "A", "Q2", "A"), ("2", "B", "Q2", "B"),
])

SCORES = score_csv([("Q1", "A", 10), ("Q2", "B", 5)])


@pytest.fixture()
def app(tmp_path):
    os.environ["EXAM_SYNC_JOBS"] = "1"
    return create_app(str(tmp_path / "test.db"), sync_jobs=True)


@pytest.fixture()
def client(app):
    return app.test_client()


def upload(client, path, csv_text, name=None, filename="upload.csv", data_extra=None):
    data = {"file": (io.BytesIO(csv_text.encode("utf-8")), filename)}
    if name is not None:
        data["name"] = name
    if data_extra:
        data.update(data_extra)
    response = client.post(
        path, data=data, content_type="multipart/form-data", follow_redirects=True
    )
    assert response.status_code == 200
    return response


def active_run_id(app):
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        try:
            return db.active_run_id(conn)
        finally:
            conn.close()


def current_grades(app):
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        try:
            return read_grades(conn, db.active_run_id(conn))
        finally:
            conn.close()


def grades_by_card(grade_view):
    return {g["card_no"]: g for g in grade_view["grades"]}


def setup_data(client):
    upload(client, "/forms", A_FORM, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", "Q1"), ("C1", "2", "B", "Q2"),   # 10 + 5 = 15
        ("C2", "1", "B", ""), ("C2", "2", "", ""),         # 0 + 0 = 0
    ]))


# --------------------------------------------------------------------------
# Parsing / validation
# --------------------------------------------------------------------------

def test_score_csv_requires_unique_code():
    bad = score_csv([("Q1", "A", 10), ("Q1", "B", 5)])
    with pytest.raises(engine.ValidationError):
        engine.parse_score_csv(bad.encode())


def test_score_csv_requires_positive_integer_points():
    for bad_points in ("0", "-3", "1.5", "", "abc"):
        with pytest.raises(engine.ValidationError):
            engine.parse_score_csv(
                (SCORE_HEADER + f"Q1,A,{bad_points}\n").encode()
            )


def test_score_csv_requires_correct_option():
    with pytest.raises(engine.ValidationError):
        engine.parse_score_csv((SCORE_HEADER + "Q1,,10\n").encode())


def test_pass_line_must_be_positive_integer():
    for bad in ("", "0", "-1", "6.5", "abc"):
        with pytest.raises(engine.ValidationError):
            engine.parse_pass_line(bad)
    assert engine.parse_pass_line(" 12 ") == 12


def test_invalid_score_upload_is_rejected_before_job(client, app):
    setup_data(client)
    resp = upload(client, "/scores", score_csv([("Q1", "A", 10), ("Q1", "B", 5)]),
                  data_extra={"pass_line": "10"})
    assert "重复" in resp.get_data(as_text=True)
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        try:
            assert db.get_meta(conn, "active_score_id") == "0"
            assert conn.execute("SELECT COUNT(*) AS n FROM score_tables").fetchone()["n"] == 0
        finally:
            conn.close()


def test_bad_pass_line_rejected(client, app):
    setup_data(client)
    resp = upload(client, "/scores", SCORES, data_extra={"pass_line": "0"})
    assert "及格线" in resp.get_data(as_text=True)
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        try:
            assert db.get_meta(conn, "active_score_id") == "0"
        finally:
            conn.close()


# --------------------------------------------------------------------------
# Grading rules
# --------------------------------------------------------------------------

def test_pass_and_fail_verdicts(client, app):
    setup_data(client)
    upload(client, "/scores", SCORES, data_extra={"pass_line": "10"})
    grades = current_grades(app)
    assert grades is not None
    assert grades["paused"] is False
    assert grades["pass_line"] == 10
    by_card = grades_by_card(grades)

    c1 = by_card["C1"]
    assert c1["min_score"] == c1["max_score"] == 15
    assert c1["min_form"] == c1["max_form"] == "A卷"
    assert c1["scores"] == {"A卷": 15}
    assert c1["verdict"] == "pass"

    c2 = by_card["C2"]
    assert c2["min_score"] == c2["max_score"] == 0
    assert c2["verdict"] == "fail"


def test_wrong_and_blank_answers_earn_zero(client, app):
    setup_data(client)
    # Pass line 1: even one correct point passes; C2 has none.
    upload(client, "/scores", SCORES, data_extra={"pass_line": "1"})
    by_card = grades_by_card(current_grades(app))
    assert by_card["C1"]["verdict"] == "pass"
    assert by_card["C2"]["max_score"] == 0
    assert by_card["C2"]["verdict"] == "fail"


def test_rescan_verdict_between_min_and_max(client, app):
    form_b = form_csv([
        ("1", "A", "Q1", "B"), ("1", "B", "Q1", "A"),  # reversed Q1 options
        ("2", "A", "Q2", "A"), ("2", "B", "Q2", "B"),
    ])
    upload(client, "/forms", A_FORM, name="A卷")
    upload(client, "/forms", form_b, name="B卷")
    # Blank scanned codes keep both forms compatible.
    upload(client, "/answers", answer_csv([
        ("C3", "1", "A", ""), ("C3", "2", "B", ""),
    ]))
    upload(client, "/scores", SCORES, data_extra={"pass_line": "10"})
    by_card = grades_by_card(current_grades(app))
    g = by_card["C3"]
    # A卷: Q1 correct (10) + Q2 correct (5) = 15
    # B卷: Q1 letter A normalises to B, wrong (0) + Q2 correct (5) = 5
    assert g["scores"] == {"A卷": 15, "B卷": 5}
    assert g["min_score"] == 5 and g["min_form"] == "B卷"
    assert g["max_score"] == 15 and g["max_form"] == "A卷"
    assert g["verdict"] == "rescan"


def test_min_reaching_pass_line_is_pass(client, app):
    form_b = form_csv([
        ("1", "A", "Q1", "B"), ("1", "B", "Q1", "A"),
        ("2", "A", "Q2", "A"), ("2", "B", "Q2", "B"),
    ])
    upload(client, "/forms", A_FORM, name="A卷")
    upload(client, "/forms", form_b, name="B卷")
    upload(client, "/answers", answer_csv([
        ("C3", "1", "A", ""), ("C3", "2", "B", ""),
    ]))
    # Pass line 5: even the lowest form total (5) reaches it.
    upload(client, "/scores", SCORES, data_extra={"pass_line": "5"})
    assert grades_by_card(current_grades(app))["C3"]["verdict"] == "pass"


def test_unmatched_card_listed_separately_from_grades(client, app):
    upload(client, "/forms", A_FORM, name="A卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", "Q1"), ("C1", "2", "B", "Q2"),
        ("C9", "1", "A", "Q9"), ("C9", "2", "B", "Q2"),  # incompatible
    ]))
    upload(client, "/scores", SCORES, data_extra={"pass_line": "10"})
    grades = current_grades(app)
    assert list(grades_by_card(grades)) == ["C1"]
    run = read_run_by_id(app, active_run_id(app))
    assert [u["card_no"] for u in run["unmatched"]] == ["C9"]


def test_question_absent_from_form_scores_zero(client, app):
    # Two compatible forms; B卷 does not print Q2 (different code on q2).
    form_b = form_csv([
        ("1", "A", "Q1", "A"), ("1", "B", "Q1", "B"),
        ("2", "A", "Q2B", "A"), ("2", "B", "Q2B", "B"),
    ])
    upload(client, "/forms", A_FORM, name="A卷")
    upload(client, "/forms", form_b, name="B卷")
    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", ""), ("C1", "2", "B", ""),
    ]))
    upload(client, "/scores", SCORES, data_extra={"pass_line": "1"})
    g = grades_by_card(current_grades(app))["C1"]
    # A卷: 10 + 5 = 15 ; B卷: 10 + (Q2 not printed -> 0) = 10
    assert g["scores"] == {"A卷": 15, "B卷": 10}
    assert g["min_form"] == "B卷" and g["min_score"] == 10
    assert g["max_form"] == "A卷" and g["max_score"] == 15


# --------------------------------------------------------------------------
# Snapshot semantics
# --------------------------------------------------------------------------

def test_replacing_score_table_keeps_snapshot_id(client, app):
    setup_data(client)
    upload(client, "/scores", SCORES, data_extra={"pass_line": "10"})
    run_before = active_run_id(app)
    first = current_grades(app)
    assert first["pass_line"] == 10

    upload(client, "/scores", SCORES, data_extra={"pass_line": "20"})
    run_after = active_run_id(app)
    assert run_after == run_before  # item snapshot id unchanged
    second = current_grades(app)
    assert second["score_id"] != first["score_id"]
    assert second["pass_line"] == 20
    # C1 scores 15: now below the stricter line.
    assert grades_by_card(second)["C1"]["verdict"] == "fail"


def test_score_upload_before_any_run_grades_first_snapshot(client, app):
    upload(client, "/forms", A_FORM, name="A卷")
    run_without_scores = active_run_id(app)
    # That snapshot was created before any scoring table existed, so it has
    # no grades attached yet.
    assert current_grades(app) is None
    upload(client, "/scores", SCORES, data_extra={"pass_line": "10"})
    # Swapping the table must not mint a new item snapshot.
    assert active_run_id(app) == run_without_scores
    assert current_grades(app)["paused"] is False

    upload(client, "/answers", answer_csv([
        ("C1", "1", "A", "Q1"), ("C1", "2", "B", "Q2"),
    ]))
    grades = current_grades(app)
    assert grades is not None and grades["paused"] is False
    assert grades_by_card(grades)["C1"]["verdict"] == "pass"


def test_invalid_item_pauses_grades_but_stats_still_publish(client, app):
    setup_data(client)
    bad_scores = score_csv([("Q1", "A", 10), ("Q9", "A", 5)])  # Q9/A in no form
    upload(client, "/scores", bad_scores, data_extra={"pass_line": "10"})
    grades = current_grades(app)
    assert grades["paused"] is True
    assert grades["grades"] == []
    invalid = {(i["std_code"], i["std_option"]) for i in grades["invalid_items"]}
    assert invalid == {("Q9", "A")}
    run = read_run_by_id(app, active_run_id(app))
    assert run["stats"]["Q1"]["counted"] == 2  # item statistics still served

    page = client.get("/grades")
    text = page.get_data(as_text=True)
    assert "成绩供数暂停" in text
    assert "Q9" in text
    assert f"题项快照 #{run['id']}" in text


def test_form_update_invalidating_item_pauses_new_snapshot_keeps_old(client, app):
    setup_data(client)
    upload(client, "/scores", SCORES, data_extra={"pass_line": "10"})
    old_run = active_run_id(app)
    assert current_grades(app)["paused"] is False

    # Rename Q1 -> Q1X in the only form: pair (Q1, A) no longer exists anywhere.
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        form_id = conn.execute("SELECT id FROM forms").fetchone()["id"]
        conn.close()
    client.post("/forms/correct", data={
        "form_id": str(form_id), "q_no": "1", "new_code": "Q1X",
    }, follow_redirects=True)

    new_run = active_run_id(app)
    assert new_run != old_run
    assert current_grades(app)["paused"] is True
    # Previous snapshot and its grades stay fully readable.
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        try:
            old_grades = read_grades(conn, old_run)
            old_view = read_run(conn, old_run)
        finally:
            conn.close()
    assert old_grades["paused"] is False
    assert grades_by_card(old_grades)["C1"]["verdict"] == "pass"
    assert old_view["stats"]["Q1"]["counted"] == 2


def test_fixed_score_table_resumes_grades(client, app):
    setup_data(client)
    upload(client, "/scores",
           score_csv([("Q1", "A", 10), ("Q9", "A", 5)]),
           data_extra={"pass_line": "10"})
    assert current_grades(app)["paused"] is True
    upload(client, "/scores", SCORES, data_extra={"pass_line": "10"})
    grades = current_grades(app)
    assert grades["paused"] is False
    assert grades_by_card(grades)["C1"]["verdict"] == "pass"


# --------------------------------------------------------------------------
# Transactionality
# --------------------------------------------------------------------------

def test_failed_score_save_keeps_previous_state(app):
    client = app.test_client()
    setup_data(client)
    upload(client, "/scores", SCORES, data_extra={"pass_line": "10"})
    before = current_grades(app)
    before_score_id = before["score_id"]

    from examapp import app as app_module
    original = app_module._do_upload_scores

    def boom(conn, payload):
        original(conn, payload)
        raise RuntimeError("disk full")

    app_module._do_upload_scores = boom
    try:
        resp = upload(client, "/scores", SCORES, data_extra={"pass_line": "20"})
        assert "失败" in resp.get_data(as_text=True)
    finally:
        app_module._do_upload_scores = original

    after = current_grades(app)
    assert after["score_id"] == before_score_id
    assert after["pass_line"] == 10
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        try:
            assert db.get_meta(conn, "active_score_id") == str(before_score_id)
        finally:
            conn.close()


def test_grades_page_lists_snapshot_and_verdicts(client, app):
    setup_data(client)
    upload(client, "/scores", SCORES, data_extra={"pass_line": "10"})
    page = client.get("/grades")
    assert page.status_code == 200
    text = page.get_data(as_text=True)
    run_id = active_run_id(app)
    assert f"题项快照 #{run_id}" in text
    assert "及格线 10" in text
    assert "C1" in text and "C2" in text
    assert "及格" in text and "不及格" in text
    assert "A卷：15" in text


def test_grades_page_without_score_table(client, app):
    setup_data(client)
    page = client.get("/grades")
    assert page.status_code == 200
    assert "尚未上传计分表" in page.get_data(as_text=True)


def test_score_upload_on_empty_database_is_kept(client, app):
    # No forms, no answers, no snapshot: the table is stored and used later.
    upload(client, "/scores", SCORES, data_extra={"pass_line": "10"})
    assert active_run_id(app) == 0
    assert current_grades(app) is None
    setup_data(client)
    grades = current_grades(app)
    assert grades is not None and grades["paused"] is False
    assert grades_by_card(grades)["C1"]["verdict"] == "pass"


def read_run_by_id(app, run_id):
    with app.app_context():
        conn = db.connect(app.config["DB_PATH"])
        try:
            return read_run(conn, run_id)
        finally:
            conn.close()
