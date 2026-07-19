"""Web 对话必须可见每轮真实注入 LLM 的上下文。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.engines.agent import AgentRun  # noqa: E402
from sentinel.permissions import PermissionBroker  # noqa: E402
from sentinel import webapp  # noqa: E402


def test_run_output_shows_exact_injected_context():
    context = (
        "[LAST SCAN]\n"
        "上次扫描仓库 | repo: d:\\Code\\haulhero-frontend\n"
        "语言缺口：typescript(6 文件) 因缺少解析器未被扫描。\n"
        "获明确同意后 install_language_support，再 scan。"
    )
    output = webapp._format_run(AgentRun(goal="补齐后再扫", context=context))

    assert "实际注入 LLM 的上下文" in output
    assert context in output


def test_run_output_shows_query_rewrite_trace():
    run = AgentRun(goal="补齐", context="[QUERY REWRITE] 已验证的短查询补全")
    run.rewrite_trace = {"raw_query": "补齐", "validated": {"languages": ["typescript"]}}

    output = webapp._format_run(run)

    assert "Query rewrite 与约束校验" in output
    assert '"raw_query": "补齐"' in output


def test_gradio_rich_message_is_normalized_to_text():
    assert webapp._message_text({"text": "扫一下 haulhero", "type": "text"}) == "扫一下 haulhero"
    assert webapp._message_text([{"text": "补齐"}, {"text": "后重扫"}]) == "补齐\n后重扫"


def test_github_repo_becomes_focus_without_overwriting_last_scan(tmp_path):
    github_repo = tmp_path / "haulhero-frontend"
    old_repo = tmp_path / "sentinel"
    github_repo.mkdir()
    old_repo.mkdir()
    state = {
        "last_scan": {"repo": str(old_repo), "language_gap": {"python": 1}, "spots": []},
        "focus_repo": str(old_repo),
    }
    run = AgentRun(goal="查看 haulhero-frontend 最近一次 PR")
    run.transcript = [{
        "type": "action",
        "tool": "github__list_pull_requests",
        "observation": {"result": {"local_repo": str(github_repo), "result": {}}},
    }]

    webapp._stash_focus_from_run(state, run)
    snapshot = webapp._focus_snapshot(state)

    assert state["focus_repo"] == str(github_repo)
    assert state["last_scan"]["repo"] == str(old_repo)
    assert snapshot == {"repo": str(github_repo), "language_gap": {}, "spots": []}


def test_recent_context_prefers_focus_over_stale_scan(tmp_path):
    github_repo = tmp_path / "haulhero-frontend"
    old_repo = tmp_path / "sentinel"
    github_repo.mkdir()
    old_repo.mkdir()
    state = {
        "focus_repo": str(github_repo),
        "last_scan": {
            "repo": str(old_repo),
            "language_gap": {"python": 1},
            "spots": [],
        },
    }

    context = webapp._recent_context([
        {"role": "user", "content": "查看 haulhero-frontend 最近一次 PR"},
        {"role": "assistant", "content": "PR #1 修改了遥测。"},
        {"role": "user", "content": "那你扫一下这个仓库吧"},
    ], state)

    assert f"focus repo: {github_repo}" in context
    assert "语言缺口：python" not in context


def test_explicit_scan_query_only_matches_simple_scan_requests():
    assert webapp._explicit_scan_query("扫一下haulhero-frontend") == "haulhero-frontend"
    assert webapp._explicit_scan_query("扫描 sentinel") == "sentinel"
    assert webapp._explicit_scan_query("scan enterprise-rag-lab") == "enterprise-rag-lab"
    assert webapp._explicit_scan_query("补齐后再扫一下 haulhero-frontend") is None


def test_explicit_scan_uses_local_permission_fast_path(tmp_path):
    repo = tmp_path / "haulhero-frontend"
    repo.mkdir()
    state = {"broker": PermissionBroker(str(tmp_path)), "pending": [], "candidates": []}

    reply, pending, candidates = webapp._fast_scan_request(
        state, "扫一下haulhero-frontend")

    assert "需要你的授权" in reply
    assert pending == [str(repo)]
    assert candidates == []
    assert state["goal"] == "扫一下haulhero-frontend"


def test_approve_callback_returns_updated_state(monkeypatch):
    state = {"pending": ["d:/Code/haulhero-frontend"], "candidates": [], "goal": {"text": "扫一下 haulhero"}}

    def fake_approve(callback_state, selected):
        assert callback_state is state
        assert selected is None
        callback_state["pending"] = []
        return "扫描完成"

    monkeypatch.setattr(webapp, "_ensure", lambda value: value)
    monkeypatch.setattr(webapp, "_approve", fake_approve)
    result = webapp._ui_approve([], state, None)

    assert result[1] is state
    assert result[0][-2]["content"] == "✅ 同意扫描 `扫一下 haulhero`"
    assert result[0][-1]["content"] == "扫描完成"


def test_approve_button_hides_controls_before_scan(monkeypatch):
    state = {
        "pending": ["d:/Code/haulhero-frontend"],
        "candidates": [],
        "goal": "扫一下 haulhero-frontend",
    }
    monkeypatch.setattr(webapp, "_ensure", lambda value: value)

    result = webapp._ui_begin_approve([], state, None)

    assert state["approved_target"] == "d:/Code/haulhero-frontend"
    assert state["pending"] == []
    assert result[2]["visible"] is False
    assert result[3]["visible"] is False
    assert result[4]["visible"] is False


def test_web_embedder_uses_configured_fallback(monkeypatch):
    marker = object()
    monkeypatch.setattr(webapp, "_EMBEDDER", None)
    monkeypatch.setattr("sentinel.cognition.default_embedder", lambda: marker)

    assert webapp._get_embedder() is marker


def test_open_in_editor_uses_vscode_uri_without_code_command(tmp_path, monkeypatch):
    target = tmp_path / "src" / "client.ts"
    target.parent.mkdir()
    target.write_text("export const client = {};", encoding="utf-8")
    opened = []

    monkeypatch.setattr(webapp, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setattr(webapp, "_is_open_authorized", lambda path: True)
    monkeypatch.setattr(webapp, "_code_executable", lambda: None)
    monkeypatch.setattr(webapp.os, "startfile", lambda uri: opened.append(uri), raising=False)

    ok, message = webapp._open_in_editor(str(target), 13)

    assert ok is True and "VS Code" in message
    assert opened == ["vscode://file/" + str(target).replace("\\", "/") + ":13"]


def test_open_in_editor_uses_detected_code_executable(tmp_path, monkeypatch):
    target = tmp_path / "src" / "client.ts"
    target.parent.mkdir()
    target.write_text("export const client = {};", encoding="utf-8")
    launched = []

    monkeypatch.setattr(webapp, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setattr(webapp, "_is_open_authorized", lambda path: True)
    monkeypatch.setattr(webapp, "_code_executable", lambda: "C:/VSCode/Code.exe")
    monkeypatch.setattr(
        webapp.subprocess,
        "run",
        lambda command, check: launched.append(command) or type("Result", (), {"returncode": 0})(),
    )

    ok, _ = webapp._open_in_editor(str(target), 13)

    assert ok is True
    assert launched == [["C:/VSCode/Code.exe", "--goto", f"{target}:13"]]


def test_report_file_links_open_in_a_new_tab():
    report = {
        "repo": "d:/Code/example",
        "total_units": 1,
        "blind_spot_count": 1,
        "blind_spots": [{
            "file": "src/client.ts",
            "function": "loadClient",
            "signals": ["no_log"],
            "lines": "13-20",
        }],
    }

    output = webapp._render_report(report)

    assert 'href="/open?path=' in output
    assert '&line=13"' in output
    assert 'target="_blank"' in output
    assert 'rel="noopener"' in output


def test_applied_output_distinguishes_source_emitter_from_delivery():
    output = webapp._format_applied({
        "message": "源码事件已写入",
        "units_fixed": ["src/offline/queue.ts::read"],
        "emitter": "grafana-faro",
        "receiver_configured": False,
        "delivery": "pending_configuration",
        "delivery_note": "未检测到 VITE_GRAFANA_FARO_URL。",
    })

    assert "emitter=`grafana-faro`" in output
    assert "Receiver=未检测到配置" in output
    assert "delivery=`pending_configuration`" in output
    assert "未检测到 VITE_GRAFANA_FARO_URL" in output