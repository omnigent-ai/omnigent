"""Design a DAG, export it, then test that exact graph through the real API."""

import re
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from omnigent.workflows.models import Workflow
from tests._helpers.live_server import isolated_local_server
from tests.e2e_ui.scheduled.test_scheduled_tasks_page import _open_automations_page


@pytest.fixture(scope="module", params=[False, True], ids=["default-off", "enabled"])
def workflow_server(built_spa, tmp_path_factory, request):
    directory = tmp_path_factory.mktemp("workflow-server")
    with isolated_local_server(
        directory,
        env={"OMNIGENT_FEATURES": "dynamic_workflows" if request.param else None},
    ) as url:
        yield url, request.param


@pytest.mark.parametrize(
    "entry",
    [
        "sidebar",
        pytest.param("direct-mobile", marks=pytest.mark.browser_context_args(has_touch=True)),
    ],
)
def test_dynamic_workflow_review_and_repair(
    page: Page, workflow_server, entry: str, tmp_path: Path, output_path: str
) -> None:
    live_server, enabled = workflow_server
    with httpx.Client(base_url=live_server, timeout=10) as client:
        assert client.get("/v1/info").json()["features"]["dynamic_workflows"] is enabled
        assert client.get("/v1/workflows/options").status_code == (200 if enabled else 404)
    if not enabled:
        _open_automations_page(page, live_server)
        expect(page.get_by_role("link", name="Workflows", exact=True)).to_have_count(0)
        if entry == "direct-mobile":
            page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{live_server}/workflows")
        expect(page.get_by_role("heading", name="Page not found")).to_be_visible()
        expect(page.get_by_role("heading", name="Create workflow")).to_have_count(0)
        page.screenshot(path=str(Path(output_path) / "workflows-disabled.png"), full_page=True)
        return
    if entry == "direct-mobile":
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{live_server}/workflows")
    else:
        page.set_viewport_size({"width": 1440, "height": 1100})
        _open_automations_page(page, live_server)
        page.get_by_role("link", name="Workflows", exact=True).click()
    expect(page.get_by_role("heading", name="Create workflow")).to_be_visible()
    expect(page.get_by_text("Your runs", exact=True)).to_have_count(0)
    expect(page.get_by_label("Generated YAML")).to_have_count(0)
    expect(page.get_by_role("region", name="Task settings")).not_to_contain_text("team")
    expect(page.get_by_role("region", name="Workflow settings")).not_to_contain_text(
        "Execution settings"
    )
    page.get_by_text("Execution settings", exact=True).click()
    page.get_by_label("owner agent ID", exact=True).fill("workflow-agent")
    page.get_by_label("owner sandbox provider", exact=True).fill("analysis-sandbox")
    page.get_by_text("Execution settings", exact=True).click()
    graph = page.get_by_role("region", name="Workflow DAG")
    expect(graph.get_by_text("delivery", exact=True)).to_have_count(0)
    expect(graph.locator(".react-flow__edge-path")).to_have_count(4)
    for edge in graph.locator(".react-flow__edge-path").all():
        expect(edge).to_have_attribute("marker-end", re.compile("url"))
    graph.get_by_role("button", name="Propose a change", exact=True).click()
    expect(page.get_by_role("checkbox", name="Summarize", exact=True)).to_be_disabled()

    if entry == "sidebar":
        graph.get_by_label("Output from Propose a change").drag_to(
            graph.get_by_label("Input for Review constraints")
        )
        edge_id = "propose->constraints"
        source, target = "Propose a change", "Review constraints"
    else:
        page.get_by_role("checkbox", name="Review constraints", exact=True).check()
        edge_id = "constraints->propose"
        source, target = "Review constraints", "Propose a change"
    expect(graph.locator(".react-flow__edge-path")).to_have_count(5)
    graph.scroll_into_view_if_needed()
    edge = graph.locator(f'.react-flow__edge[data-id="{edge_id}"]')
    point = edge.locator(".react-flow__edge-path").evaluate("""path => {
        const point = path.getPointAtLength(path.getTotalLength() / 2)
            .matrixTransform(path.getScreenCTM());
        return {x: point.x, y: point.y};
    }""")
    if entry == "direct-mobile":
        page.touchscreen.tap(point["x"], point["y"])
    else:
        page.mouse.click(point["x"], point["y"])
    selection = graph.get_by_role("region", name="Selected arrows")
    expect(selection.get_by_text(f"{source} → {target}", exact=True)).to_be_visible()
    expect(edge).to_have_class(re.compile(r"\bselected\b"))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(Path(output_path) / "selected-arrow.png"))
    remove = selection.get_by_role("button", name="Remove arrow", exact=True)
    if entry == "direct-mobile":
        remove.tap()
    else:
        remove.click()
    expect(edge).to_have_count(0)
    expect(graph.locator(".react-flow__edge-path")).to_have_count(4)
    expect(graph.locator(".react-flow__node")).to_have_count(4)
    expect(selection).to_have_count(0)
    graph.get_by_role("button", name=target, exact=True).click()
    prerequisite = page.get_by_role("checkbox", name=source, exact=True)
    expect(prerequisite).not_to_be_checked()
    if entry == "sidebar":
        prerequisite.check()
        edge.focus()
        edge.press("Enter")
        page.keyboard.press("Delete")
        expect(edge).to_have_count(0)
        expect(prerequisite).not_to_be_checked()
        expect(selection).to_have_count(0)
    graph.get_by_role("button", name="Propose a change", exact=True).click()

    page.get_by_role("button", name="Add task", exact=True).click()
    task_dialog = page.get_by_role("dialog", name="Add task", exact=True)
    expect(task_dialog).not_to_contain_text("team")
    expect(task_dialog.get_by_label("Task name", exact=True)).to_be_focused()
    expect(task_dialog.get_by_role("button", name="Create task", exact=True)).to_be_disabled()
    task_dialog.get_by_label("Task name", exact=True).fill("Discard this draft")
    task_dialog.get_by_text("Execution settings", exact=True).click()
    task_dialog.get_by_label("owner agent ID", exact=True).fill("discarded-agent")
    task_dialog.get_by_label("owner sandbox provider", exact=True).fill("discarded-sandbox")
    task_dialog.get_by_role("button", name="Cancel", exact=True).click()
    expect(task_dialog).to_have_count(0)
    expect(graph.locator(".react-flow__node")).to_have_count(4)
    expect(page.get_by_role("button", name="Add task", exact=True)).to_be_focused()
    page.get_by_role("button", name="Add task", exact=True).click()
    expect(task_dialog.get_by_label("Task name", exact=True)).to_have_value("")
    task_dialog.get_by_text("Execution settings", exact=True).click()
    expect(task_dialog.get_by_label("owner agent ID", exact=True)).to_have_value(
        "replace-with-registered-agent-id"
    )
    expect(task_dialog.get_by_label("owner sandbox provider", exact=True)).to_have_value(
        "replace-with-sandbox-provider"
    )
    task_dialog.get_by_label("owner agent ID", exact=True).fill("acceptance-agent")
    task_dialog.get_by_label("owner sandbox provider", exact=True).fill("acceptance-sandbox")
    task_dialog.get_by_label("owner repository (optional)", exact=True).fill(
        "https://github.com/example/acceptance"
    )
    task_dialog.get_by_role("button", name="Add agent", exact=True).click()
    task_dialog.get_by_label("specialist_1 agent ID", exact=True).fill("review-agent")
    task_dialog.get_by_label("specialist_1 sandbox provider", exact=True).fill("review-sandbox")
    task_dialog.get_by_role("combobox", name="Execution mode", exact=True).select_option(
        "parallel"
    )
    task_dialog.get_by_text("Execution settings", exact=True).click()
    expect(
        task_dialog.get_by_role("checkbox", name="Propose a change", exact=True)
    ).to_be_checked()
    task_dialog.get_by_label("Task name", exact=True).fill("Acceptance check")
    task_dialog.get_by_label("Task goal", exact=True).fill("Verify the accepted evidence.")
    task_dialog.get_by_role("checkbox", name="Review constraints", exact=True).check()
    task_dialog.get_by_text("Task options", exact=True).click()
    expect(
        task_dialog.get_by_role("checkbox", name="Allow follow-ups", exact=True)
    ).to_be_checked()
    expect(task_dialog.get_by_label("Retries", exact=True)).to_have_value("2")
    expect(graph.locator(".react-flow__node")).to_have_count(4)
    task_dialog.get_by_text("Task options", exact=True).click()
    task_dialog.get_by_text("Execution settings", exact=True).click()
    task_dialog.get_by_label("owner agent ID", exact=True).scroll_into_view_if_needed()
    page.screenshot(path=str(Path(output_path) / "add-task-dialog.png"))
    create_task = task_dialog.get_by_role("button", name="Create task", exact=True)
    if entry == "direct-mobile":
        create_task.tap()
    else:
        create_task.click()
    expect(task_dialog).to_have_count(0)
    expect(graph.locator(".react-flow__node")).to_have_count(5)
    expect(page.get_by_label("Task name", exact=True)).to_have_value("Acceptance check")
    page.get_by_text("Execution settings", exact=True).click()
    expect(page.get_by_label("owner agent ID", exact=True)).to_have_value("acceptance-agent")
    expect(page.get_by_label("owner repository (optional)", exact=True)).to_have_value(
        "https://github.com/example/acceptance"
    )
    expect(page.get_by_label("specialist_1 agent ID", exact=True)).to_have_value("review-agent")
    expect(page.get_by_role("combobox", name="Execution mode", exact=True)).to_have_value(
        "parallel"
    )
    expect(page.get_by_label("owner sandbox provider", exact=True)).to_have_value(
        "acceptance-sandbox"
    )
    expect(page.get_by_label("specialist_1 sandbox provider", exact=True)).to_have_value(
        "review-sandbox"
    )
    page.get_by_label("owner repository (optional)", exact=True).fill(
        "https://github.com/example/edited"
    )
    page.get_by_label("owner agent ID", exact=True).fill("acceptance-agent-edited")
    graph.get_by_role("button", name="Summarize", exact=True).click()
    page.get_by_text("Execution settings", exact=True).click()
    expect(page.get_by_label("owner agent ID", exact=True)).to_have_value(
        "replace-with-registered-agent-id"
    )
    page.get_by_text("Execution settings", exact=True).click()
    page.get_by_role("checkbox", name="Acceptance check", exact=True).check()
    page.get_by_role("button", name="Export YAML", exact=True).click()
    dialog = page.get_by_role("dialog")
    generated = dialog.get_by_label("Generated YAML")
    expect(generated).to_have_attribute("readonly", "")
    definition = yaml.safe_load(generated.input_value())
    assert len(definition["teams"]) == 5
    assert definition["teams"][0]["members"][0]["agent_id"] == "workflow-agent"
    assert definition["teams"][0]["members"][0]["placement"]["provider"] == "analysis-sandbox"
    assert all(node["team"] == node["id"] for node in definition["nodes"])
    assert definition["teams"][-1]["members"][0]["agent_id"] == "acceptance-agent-edited"
    assert definition["teams"][-1]["members"][0]["placement"] == {
        "kind": "sandbox",
        "provider": "acceptance-sandbox",
        "workspace": "https://github.com/example/edited",
    }
    assert definition["teams"][-1]["strategy"] == "parallel"
    assert definition["teams"][-1]["members"][1]["agent_id"] == "review-agent"
    assert definition["teams"][-1]["members"][1]["placement"] == {
        "kind": "sandbox",
        "provider": "review-sandbox",
    }
    assert len(definition["nodes"]) == 5
    assert all(node["allow_expansion"] for node in definition["nodes"])
    assert all(node["max_rounds"] == 3 for node in definition["nodes"])
    added = next(node for node in definition["nodes"] if node.get("name") == "Acceptance check")
    summary = next(node for node in definition["nodes"] if node["id"] == "summarize")
    assert added["depends_on"] == ["propose", "constraints"]
    assert added["id"] in summary["depends_on"]
    with page.expect_download() as download:
        dialog.get_by_role("button", name="Download YAML", exact=True).click()
    path = tmp_path / "workflow.yaml"
    download.value.save_as(path)
    assert yaml.safe_load(path.read_text()) == definition
    dialog.get_by_role("button", name="Close", exact=True).click()

    with page.expect_response(
        lambda response: (
            response.url.endswith("/v1/workflows") and response.request.method == "POST"
        )
    ) as created:
        page.get_by_role("button", name="Test workflow", exact=True).click()
    assert created.value.status == 201
    assert Workflow.model_validate(created.value.json()["workflow"]) == Workflow.model_validate(
        definition
    )
    run_id = created.value.json()["run_id"]
    expect(page.get_by_role("heading", name="Test workflow")).to_be_visible()
    expect(graph.get_by_text("Select an arrow to remove it.", exact=True)).to_have_count(0)
    task = graph.get_by_role("button", name="Propose a change waiting", exact=True)
    expect(task).to_be_visible(timeout=15000)
    task.click()
    details = page.get_by_role("region", name="Task propose")
    expect(details.get_by_text("Candidate propose-1", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    with httpx.Client(base_url=live_server, timeout=10) as client:
        initial = client.get(f"/v1/workflows/{run_id}").json()
        assert initial["nodes"]["investigate_check"]["status"] == "succeeded"
        session = initial["nodes"]["propose"]["members"]["owner"]["session_id"]
        assert initial["nodes"]["summarize"]["status"] == "pending"
        details.get_by_label("Feedback or validation evidence").fill("Missing edge-case evidence")
        details.get_by_role("button", name="Fail review", exact=True).click()
        expect(details.get_by_text("Candidate propose-2", exact=True)).to_be_visible(timeout=15000)
        details.get_by_label("Feedback or validation evidence").fill("One more case is missing")
        details.get_by_role("button", name="Fail review", exact=True).click()
        expect(details.get_by_text("Candidate propose-3", exact=True)).to_be_visible(timeout=15000)
        details.get_by_label("Feedback or validation evidence").fill(
            "Revision three covers the edge cases"
        )
        details.get_by_role("button", name="Pass review", exact=True).click()
        expect(
            graph.get_by_role("button", name="summarize_check succeeded", exact=True)
        ).to_be_visible(timeout=15000)
        final = client.get(f"/v1/workflows/{run_id}").json()
        assert final["status"] == "succeeded"
        assert final["nodes"]["propose"]["members"]["owner"]["session_id"] == session
        assert final["nodes"]["propose"]["artifacts"][0]["revision"] == "sim-3"
        assert final["nodes"]["propose"]["outputs"] == {
            "result": {"task": "propose", "revision": "sim-3"}
        }
        assert len(final["workflow"]["nodes"]) == 2 * len(definition["nodes"])
        members = final["nodes"][added["id"]]["members"]
        assert set(members) == {"owner", "specialist_1"}
        assert len({member["session_id"] for member in members.values()}) == 2
        assert len(members["owner"]["turns"]) == 2
        assert len(members["specialist_1"]["turns"]) == 1
    expect(details.get_by_text("Accepted outcome", exact=True)).to_be_visible()
    expect(details.get_by_text("Outputs for dependent tasks", exact=True)).to_be_visible()
    expect(details.locator("pre:visible")).to_contain_text('"revision": "sim-3"')
    page.screenshot(path=str(Path(output_path) / "accepted-outcome.png"), full_page=True)
    page.get_by_role("button", name="Back to designer", exact=True).click()
    expect(page.get_by_role("heading", name="Create workflow")).to_be_visible()
    expect(graph.get_by_role("button", name="Acceptance check", exact=True)).to_be_visible()
    graph.scroll_into_view_if_needed()
