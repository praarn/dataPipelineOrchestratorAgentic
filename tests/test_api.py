"""API-level smoke test using Flask's test client (no real server needed)."""
import io
import json
import os

from backend.app import app

SAMPLE = os.path.join(os.path.dirname(__file__), "..", "sample_data", "messy_sales_data.csv")


def main():
    client = app.test_client()

    r = client.post("/api/session")
    assert r.status_code == 200, r.get_data()
    session_id = r.get_json()["session_id"]
    print("session:", session_id)

    with open(SAMPLE, "rb") as f:
        data = f.read()
    r = client.post(
        f"/api/session/{session_id}/ingest",
        data={"file": (io.BytesIO(data), "messy_sales_data.csv")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 200, r.get_data()
    ing = r.get_json()
    print("ingest stage:", ing["stage"], "rows:", ing["ingestion_report"]["row_count"])

    r = client.post(f"/api/session/{session_id}/clean/preview")
    assert r.status_code == 200, r.get_data()
    plan = r.get_json()["cleaning_plan"]
    print("cleaning actions proposed:", len(plan["proposed_actions"]))

    approved_ids = [a["action_id"] for a in plan["proposed_actions"]]
    r = client.post(
        f"/api/session/{session_id}/clean/apply",
        data=json.dumps({"approved_action_ids": approved_ids}),
        content_type="application/json",
    )
    assert r.status_code == 200, r.get_data()
    log = r.get_json()["cleaning_log"]
    print("quality:", log["quality_score_before"], "->", log["quality_score_after"])

    r = client.post(f"/api/session/{session_id}/analyze")
    assert r.status_code == 200, r.get_data()
    analysis = r.get_json()["analysis_result"]
    print("findings:", len(analysis["findings"]))
    for f in analysis["findings"]:
        print("  -", f["summary"])

    # exercise the feedback loop
    if analysis["findings"]:
        col = analysis["findings"][0]["related_columns"][0]
        r = client.post(
            f"/api/session/{session_id}/analyze/flag",
            data=json.dumps({"column": col}),
            content_type="application/json",
        )
        assert r.status_code == 200, r.get_data()
        print("feedback loop reopened cleaning for column:", r.get_json()["reopened_for_column"])

        # re-apply cleaning to get back on track
        plan2 = r.get_json()["cleaning_plan"]
        approved_ids2 = [a["action_id"] for a in plan2["proposed_actions"]]
        r = client.post(
            f"/api/session/{session_id}/clean/apply",
            data=json.dumps({"approved_action_ids": approved_ids2}),
            content_type="application/json",
        )
        assert r.status_code == 200, r.get_data()
        r = client.post(f"/api/session/{session_id}/analyze")
        assert r.status_code == 200, r.get_data()
        analysis = r.get_json()["analysis_result"]

    r = client.post(f"/api/session/{session_id}/visualize")
    assert r.status_code == 200, r.get_data()
    viz = r.get_json()["visualization_result"]
    print("charts:", len(viz["charts"]), "skipped:", len(viz["findings_without_charts"]))

    r = client.post(
        f"/api/session/{session_id}/report",
        data=json.dumps({"audience": "technical"}),
        content_type="application/json",
    )
    assert r.status_code == 200, r.get_data()
    report = r.get_json()["report_result"]
    print("report length:", len(report["markdown"]))

    # downloads
    for fmt in ("md", "pdf", "docx"):
        r = client.get(f"/api/session/{session_id}/report/download?format={fmt}")
        assert r.status_code == 200, (fmt, r.get_data())
        print(f"download {fmt}: {len(r.get_data())} bytes, content-type={r.content_type}")

    # state reload
    r = client.get(f"/api/session/{session_id}/state")
    assert r.status_code == 200
    print("final stage:", r.get_json()["stage"])

    # error handling checks
    r = client.get("/api/session/doesnotexist/state")
    assert r.status_code == 404
    print("404 for missing session: OK")

    r = client.post(f"/api/session/{session_id}/ingest")
    assert r.status_code == 400
    print("400 for missing file: OK")

    print("\nALL API TESTS PASSED")


if __name__ == "__main__":
    main()
