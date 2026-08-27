import json
import subprocess
import sys
from datetime import datetime, timezone


def test_schedule_has_twice_daily_primary_runs_and_delayed_fallbacks():
    workflow = open(".github/workflows/build.yml", encoding="utf-8").read()

    assert 'cron: "17 9,17 * * *"' in workflow
    assert 'cron: "47 9,17 * * *"' in workflow
    assert "age >= dt.timedelta(hours=2)" in workflow
    assert "cancel-in-progress: false" in workflow
    current_head_checkout = (
        "ref: ${{ github.event_name == 'schedule' && "
        "github.event.repository.default_branch || github.ref }}"
    )
    assert workflow.count(current_head_checkout) == 2
    assert "0 */3 * * *" not in workflow
    utc_hours = (9, 17)
    assert tuple((datetime(2026, 1, 1, hour, tzinfo=timezone.utc).hour + 3) % 24 for hour in utc_hours) == (12, 20)


def test_page_displays_last_update_in_amsterdam_time():
    page = open("docs/index.html", encoding="utf-8").read()

    assert 'timeZone: "Europe/Amsterdam"' in page
    assert 'timeZoneName: "short"' in page
    assert "(Амстердам)" in page
    assert 'timeZone: "Europe/Moscow"' not in page


def test_workflow_keeps_source_health_diagnostic_only():
    workflow = open(".github/workflows/build.yml", encoding="utf-8").read()

    validation = workflow.split("- name: Validate and publish candidate", 1)[1].split(
        "- name: Commit output", 1
    )[0]
    assert "--source-health .cache/candidates/source-health.json" in validation
    assert "--strict-source-health" not in validation


def test_repeated_source_failure_promotes_with_zero_cli_exit(tmp_path):
    candidate = tmp_path / "candidate.json"
    published = tmp_path / "published.json"
    health = tmp_path / "candidate-health.json"
    published_health = tmp_path / "published-health.json"
    candidate_state = tmp_path / "candidate-state.json"
    published_state = tmp_path / "published-state.json"
    sources = tmp_path / "sources.json"
    item = {
        "source": "failed source",
        "url": "https://example.com/article",
        "published_at": "2026-08-09T00:00:00Z",
        "content_text": "valid candidate content",
    }
    failure = {
        "source": "failed source",
        "index_fetch_status": "failed",
        "consecutive_fetch_failures": 3,
        "consecutive_failures": 3,
        "last_error": "blocked",
    }
    candidate.write_text(json.dumps({"items": [item]}), encoding="utf-8")
    health.write_text(json.dumps({"sources": [failure]}), encoding="utf-8")
    candidate_state.write_text('{"candidate": true}', encoding="utf-8")
    sources.write_text(json.dumps([{"name": "failed source", "enabled": True}]), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable, "scripts/metrics.py", str(candidate), "--sources", str(sources),
            "--source-health", str(health), "--promote-feed", str(published),
            "--promote-source-health", str(published_health),
            "--candidate-state", str(candidate_state), "--promote-state", str(published_state),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(published.read_text(encoding="utf-8"))["items"] == [item]
    assert json.loads(published_health.read_text(encoding="utf-8"))["sources"] == [failure]
    assert "crawl_failures: 1" in result.stdout
    assert "failed for 3 consecutive runs (blocked)" in result.stdout
    assert f"promoted_feed: {published}" in result.stdout
