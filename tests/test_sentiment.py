"""Phase 2: AI sentiment, summaries, the Overview page and staff corrections.

No real API calls: a fake client stands in for Claude and labels by simple rules.
"""

from __future__ import annotations

import io
import json
import re
from types import SimpleNamespace

from openpyxl import load_workbook

from server import ai
from server.analysis import BRIEF_KEY, MAX_ATTEMPTS, label_next_batch, post_needing_summary, run_once
from server.db import now, transaction
from server.ingest import create_post, save_scrape
from tests.conftest import comment_id

POST = "1557299289776837"
COMMENTS = ["pabor po aq dito", "ayaw namin nito", "kailan po ito?", "", "ok lang"]


def rule_label(text: str) -> str:
    if "pabor" in text:
        return "supportive"
    if "ayaw" in text:
        return "against"
    if "?" in text:
        return "question"
    return "neutral"


class FakeClient:
    """Answers like Claude would, with structured JSON; records every request."""

    def __init__(self, stop_reason: str = "end_turn", skip: str | None = None) -> None:
        self.stop_reason = stop_reason
        self.skip = skip  # comments containing this are left out of the answer
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        system = kw["system"][0]["text"]
        user = kw["messages"][0]["content"]
        if system == ai.LABEL_SYSTEM:
            labels = []
            for n, line in re.findall(r"^\[(\d+)\](.*)$", user, re.M):
                if self.skip and self.skip in line:
                    continue
                labels.append({"n": int(n), "label": rule_label(line), "reason": f"rule for {line.strip()[:20]}"})
            answer = {"labels": labels}
        elif system == ai.SUMMARY_SYSTEM:
            answer = {"summary": "Most commenters back the plan.", "issues": ["kolorum", "traffic"]}
        else:
            answer = {"summary": "This month residents mostly supported the LGU.", "issues": ["kolorum"]}
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="text", text=json.dumps(answer))],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200,
                                  cache_read_input_tokens=500, cache_creation_input_tokens=0),
        )

    def label_calls(self) -> list[dict]:
        return [c for c in self.calls if c["system"][0]["text"] == ai.LABEL_SYSTEM]


def add_post(conn, texts, link="https://www.facebook.com/share/p/s1/", post_id=POST):
    pid = create_post(conn, link, None)
    recent = now() - 3 * 86400  # inside the Overview's 30 days
    with transaction(conn):
        save_scrape(conn, pid, {"post_id": post_id, "text": "Bawal na ang kolorum", "page_name": "Lopez LGU"}, [
            {"id": comment_id(post_id, n), "author_id": str(100 + n), "author_name": f"P{n}",
             "text": t, "created_time": recent + n, "reaction_count": n}
            for n, t in enumerate(texts, 1)
        ])
    return pid


def run_all(conn, client, settings, limit=50):
    for _ in range(limit):
        if not run_once(conn, client, settings):
            return
    raise AssertionError("the worker never ran out of work")


def labels(conn):
    return {
        r["text"] or "": (r["label"], r["model"])
        for r in conn.execute(
            "SELECT c.text, s.label, s.model FROM comments c JOIN comment_sentiment s ON s.comment_id = c.id"
        )
    }


def rid(conn, text):
    return conn.execute("SELECT rowid FROM comments WHERE text = ?", (text,)).fetchone()[0]


def test_labels_summary_brief_and_usage(conn, settings):
    pid = add_post(conn, COMMENTS)
    client = FakeClient()
    run_all(conn, client, settings)

    got = labels(conn)
    assert got["pabor po aq dito"] == ("supportive", settings.sentiment_model)
    assert got["ayaw namin nito"][0] == "against"
    assert got["kailan po ito?"][0] == "question"
    assert got[""] == ("neutral", "rule")  # no text: no API call needed
    assert len(client.label_calls()) == 1
    request = client.label_calls()[0]
    assert request["model"] == "claude-haiku-5-5" and request["output_config"]["effort"] == "low"
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Label all 4 comments" in request["messages"][0]["content"]

    summary = conn.execute("SELECT * FROM post_summaries WHERE post_id = ?", (pid,)).fetchone()
    assert summary["summary"] == "Most commenters back the plan." and summary["basis"] == "1,1,2,1"
    brief = conn.execute("SELECT * FROM briefs WHERE key = ?", (BRIEF_KEY,)).fetchone()
    assert "mostly supported" in brief["text"]
    purposes = [r[0] for r in conn.execute("SELECT purpose FROM api_usage ORDER BY id")]
    assert purposes == ["labels", "summary", "brief"]

    # Nothing changed: no more calls.
    calls = len(client.calls)
    assert not run_once(conn, client, settings)
    assert len(client.calls) == calls


def test_left_out_comment_is_retried_alone_then_given_up(conn, settings):
    add_post(conn, ["pabor po", "skip me please", "ayaw ko"])
    client = FakeClient(skip="skip me")
    assert label_next_batch(conn, client, settings) == 3
    assert conn.execute("SELECT attempts FROM sentiment_failures").fetchone()[0] == 1
    for attempt in range(2, MAX_ATTEMPTS + 1):
        assert label_next_batch(conn, client, settings) == 1
        assert "Label all 1 comments" in client.label_calls()[-1]["messages"][0]["content"]
        assert conn.execute("SELECT attempts FROM sentiment_failures").fetchone()[0] == attempt
    # Given up: no more labeling, and the post can be summarized without it.
    assert label_next_batch(conn, client, settings) == 0
    assert post_needing_summary(conn) is not None


def test_refusal_records_failure_and_cost(conn, settings):
    add_post(conn, ["pabor po", "ayaw ko"])
    client = FakeClient(stop_reason="refusal")
    label_next_batch(conn, client, settings)
    assert conn.execute("SELECT COUNT(*) FROM comment_sentiment").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sentiment_failures").fetchone()[0] == 2
    assert conn.execute("SELECT input_tokens FROM api_usage").fetchone()[0] == 1000


def test_cut_off_answer_is_a_failure(conn, settings):
    add_post(conn, ["pabor po"])
    label_next_batch(conn, FakeClient(stop_reason="max_tokens"), settings)
    assert "cut off" in conn.execute("SELECT last_error FROM sentiment_failures").fetchone()[0]


def test_usage_cost():
    # Haiku 5.5: $0.10 in, $0.50 out per million; cache reads at a tenth of input.
    cost = ai.usage_cost("claude-haiku-5-5", 1_000_000, 1_000_000, 1_000_000, 0)
    assert abs(cost - (0.10 + 0.50 + 0.01)) < 1e-9


def test_staff_correct_labels_and_mayor_cannot(browsers, conn, settings):
    pid = add_post(conn, COMMENTS)
    run_all(conn, FakeClient(), settings)
    target = rid(conn, "ok lang")
    page = f"/posts/{pid}"

    assert browsers["mayor"].post(f"/comments/{target}/label", {"label": "supportive"}).status_code == 403
    assert 'class="correct"' not in browsers["mayor"].get(page).text

    staff = browsers["staff"]
    response = staff.post(f"/comments/{target}/label", {"label": "supportive", "next": page})
    assert response.status_code == 303 and response.headers["location"].endswith(f"#c{target}")
    assert conn.execute("SELECT label FROM comment_labels WHERE comment_id = ?",
                        (comment_id(POST, 5),)).fetchone()[0] == "supportive"
    assert "Corrected by staff" in staff.get(page).text
    # The summary is written again, since the counts changed.
    assert post_needing_summary(conn) == (pid, "2,1,1,1")

    assert staff.post(f"/comments/{target}/label", {"label": "bogus", "next": page}).status_code == 400
    staff.post(f"/comments/{target}/label", {"label": "", "next": page})
    assert conn.execute("SELECT COUNT(*) FROM comment_corrections").fetchone()[0] == 0
    # An outside "next" is not followed.
    away = staff.post(f"/comments/{target}/label", {"label": "neutral", "next": "https://evil.example/"})
    assert away.headers["location"].startswith("/")


def test_post_page_sentiment_filter_and_summary(browsers, conn, settings):
    pid = add_post(conn, COMMENTS)
    run_all(conn, FakeClient(), settings)
    page = browsers["mayor"].get(f"/posts/{pid}").text
    assert "Most commenters back the plan." in page and "kolorum · traffic" in page
    against = browsers["mayor"].get(f"/posts/{pid}?sentiment=against").text
    assert "ayaw namin nito" in against and "pabor po aq dito" not in against
    assert 'name="sentiment" value="against"' in against  # kept when the other filters change
    assert browsers["mayor"].get(f"/posts/{pid}?sentiment=junk").status_code == 200


def test_overview_for_every_role(browsers, conn, settings):
    assert browsers["mayor"].get("/", follow_redirects=False).headers["location"] == "/overview"
    empty = browsers["mayor"].get("/overview")
    assert empty.status_code == 200 and "No supportive comments" in empty.text

    add_post(conn, COMMENTS)
    run_all(conn, FakeClient(), settings)
    for role, browser in browsers.items():
        page = browser.get("/overview")
        assert page.status_code == 200, role
        assert "mostly supported the LGU" in page.text
        assert "pabor po aq dito" in page.text  # supporters' comments
        assert ">20%<" in page.text  # 1 of 5 comments (the no-text one counts as neutral)
    for period in ("7", "all", "junk"):
        assert browsers["mayor"].get(f"/overview?period={period}").status_code == 200


def test_reanalyze_keeps_corrections(browsers, conn, settings):
    pid = add_post(conn, COMMENTS)
    run_all(conn, FakeClient(), settings)
    browsers["staff"].post(f"/comments/{rid(conn, 'ok lang')}/label", {"label": "supportive", "next": f"/posts/{pid}"})
    assert browsers["staff"].post("/settings/ai/reanalyze").status_code == 403
    assert browsers["admin"].post("/settings/ai/reanalyze").status_code == 303
    assert conn.execute("SELECT COUNT(*) FROM comment_sentiment").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM post_summaries").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM comment_corrections").fetchone()[0] == 1


def test_edited_comment_is_labeled_again(conn, settings):
    pid = add_post(conn, ["pabor po", "ok lang"])
    run_all(conn, FakeClient(), settings)
    with transaction(conn):
        conn.execute("INSERT INTO comment_corrections (comment_id, label, user_id, corrected_at) VALUES (?, 'question', NULL, ?)",
                     (comment_id(POST, 2), now()))
        save_scrape(conn, pid, {"post_id": POST}, [
            {"id": comment_id(POST, 1), "author_name": "P1", "text": "ayaw ko na pala", "created_time": now()},
            {"id": comment_id(POST, 2), "author_name": "P2", "text": "ok lang", "created_time": now()},
        ])
    left = {r[0] for r in conn.execute("SELECT comment_id FROM comment_sentiment")}
    assert comment_id(POST, 1) not in left and comment_id(POST, 2) in left
    assert conn.execute("SELECT COUNT(*) FROM comment_corrections").fetchone()[0] == 1
    run_all(conn, FakeClient(), settings)
    assert labels(conn)["ayaw ko na pala"][0] == "against"


def test_exports_and_settings_show_sentiment(browsers, conn, settings):
    pid = add_post(conn, COMMENTS)
    run_all(conn, FakeClient(), settings)
    sheet = load_workbook(io.BytesIO(browsers["mayor"].get(f"/posts/{pid}/export.xlsx").content)).active
    header = [c.value for c in sheet[1]]
    rows = {r[header.index("Comment")]: r for r in sheet.iter_rows(min_row=2, values_only=True)}
    assert rows["pabor po aq dito"][header.index("Sentiment")] == "Supportive"
    assert rows["pabor po aq dito"][header.index("Why")]

    people = load_workbook(io.BytesIO(browsers["mayor"].get("/people/export.xlsx").content)).worksheets[0]
    assert "Usually" in [c.value for c in people[1]]

    settings_page = browsers["admin"].get("/settings").text
    assert "AI sentiment" in settings_page and "claude-haiku-5-5" in settings_page
    assert "5 of 5" in settings_page


def test_compare_supportive(conn, settings, tmp_path):
    import csv

    from server import accuracy

    add_post(conn, COMMENTS)
    run_all(conn, FakeClient(), settings)

    def write(path, numbers):
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["id", "text"])
            w.writeheader()
            for n in numbers:
                w.writerow({"id": comment_id(POST, n), "text": COMMENTS[n - 1]})

    picks = tmp_path / "positive_abc.csv"
    write(picks, [1, 5])            # "pabor…" (AI agrees) and "ok lang" (AI: neutral)
    write(tmp_path / "comments_abc.csv", [1, 2, 3, 4, 5])
    assert accuracy.comments_file_for(picks) == tmp_path / "comments_abc.csv"
    result = accuracy.compare_file(conn, picks, tmp_path / "comments_abc.csv")
    assert (result.reviewed, result.picked) == (5, 2)
    assert [i.text for i in result.agreed] == ["pabor po aq dito"]
    assert [(i.text, i.label) for i in result.missed] == [("ok lang", "neutral")]
    assert result.extra == [] and result.not_analyzed == 0
    text = accuracy.report([result])
    assert "1 of 2 (50%)" in text and "1 of 1 (100%)" in text and "MISSED (AI: neutral) ok lang" in text
