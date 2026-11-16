"""Paid kie tasks survive a local timeout without submitting or charging twice."""

import ast
import contextlib
import io
import json
import threading
from pathlib import Path

import pytest
from PIL import Image

from app import artwork, fal, file_lock, genai, kie_images


@pytest.fixture
def api(monkeypatch):
    calls = []
    state = {"status": "success"}
    out = io.BytesIO()
    Image.new("RGB", (16, 16)).save(out, "PNG")

    def request(url, body=None, key="", timeout=60):
        calls.append((url, body, key))
        if url.endswith("credit"):
            return {"data": 100}
        if "upload" in url:
            return {"data": {"downloadUrl": "https://cdn.example/ref.png"}}
        if url.endswith("createTask"):
            return {"data": {"taskId": "paid-task"}}
        if state["status"] == "timeout":
            raise kie_images.KieError("kie.ai took too long to answer")
        return {
            "data": {
                "state": state["status"],
                "failMsg": "Content declined",
                "resultJson": json.dumps({"resultUrls": ["https://cdn.example/out.png"]}),
            }
        }

    monkeypatch.setattr(kie_images, "_json", request)
    monkeypatch.setattr(kie_images, "_download", lambda url: out.getvalue())
    monkeypatch.setattr(kie_images, "POLL_S", 0)
    return calls, state, out.getvalue()


def client(tmp_path, charges, **kw):
    return fal.FalClient(
        "fal-key",
        image_service="kie",
        kie_key="kie-key",
        task_dir=tmp_path,
        charge=lambda c, m: charges.append((c, m)),
        **kw,
    )


def test_selected_provider_and_edit_upload(tmp_path, api):
    calls, _, png = api
    charges = []
    c = client(tmp_path, charges)
    assert c.generate("nano_banana", "paint bird", refs=[fal.data_uri(png)], aspect="3:4") == png
    create = next(b for u, b, k in calls if u.endswith("createTask"))
    assert create == {
        "model": "google/nano-banana-edit",
        "input": {
            "prompt": "paint bird",
            "image_urls": ["https://cdn.example/ref.png"],
            "output_format": "png",
            "aspect_ratio": "3:4",
        },
    }
    assert charges == [(2, "nano_banana_kie")]
    assert all(k == "kie-key" for _, _, k in calls)


def test_timeout_then_new_client_collects_once(tmp_path, api):
    calls, state, png = api
    charges = []
    state["status"] = "timeout"
    with pytest.raises(fal.FalError, match=r"paid task.*saved"):
        client(tmp_path, charges, timeout=0.01).generate("nano_banana_t2i", "bird")
    assert "paid-task" in (tmp_path / "0.json").read_text()
    state["status"] = "success"
    assert client(tmp_path, charges).generate("nano_banana_t2i", "bird") == png
    assert len([u for u, _, _ in calls if u.endswith("createTask")]) == 1
    assert charges == [(2, "nano_banana_kie_t2i")]
    # A crash after download but before the widget saves the picture is safe too.
    assert client(tmp_path, charges).generate("nano_banana_t2i", "bird") == png
    assert len(charges) == 1


def test_failed_task_is_reported_without_paying_again(tmp_path, api):
    calls, state, _ = api
    state["status"] = "fail"
    charges = []
    for _ in range(2):
        with pytest.raises(fal.FalError, match="Content declined"):
            client(tmp_path, charges).generate("nano_banana_t2i", "bird")
    assert len([u for u, _, _ in calls if u.endswith("createTask")]) == 1
    assert len(charges) == 1


def test_uncertain_submission_is_never_repeated(tmp_path, api, monkeypatch):
    original = kie_images._json

    def request(url, *args, **kw):
        if url.endswith("createTask"):
            raise TimeoutError()
        return original(url, *args, **kw)

    monkeypatch.setattr(kie_images, "_json", request)
    charges = []
    with pytest.raises(fal.FalError):
        client(tmp_path, charges).generate("nano_banana_t2i", "bird")
    with pytest.raises(fal.FalError, match=r"submission.*unknown"):
        client(tmp_path, charges).generate("nano_banana_t2i", "bird")
    assert len(charges) == 1


def test_explicit_choice_never_falls_back_to_other_paid_service(tmp_path):
    with pytest.raises(fal.FalError, match=r"kie.ai key"):
        fal.FalClient("fal-key", image_service="kie", kie_key="", task_dir=tmp_path).generate(
            "nano_banana_t2i", "bird"
        )


@pytest.mark.parametrize("cap,ceiling,message", [(2, 10, "budget"), (10, 2, "ceiling")])
def test_budget_blocks_new_task_but_allows_collection(tmp_path, api, cap, ceiling, message):
    calls, state, png = api
    c = artwork.Collection("kie_test", "paintings", make=lambda j: (b"", {}), label="Kie test")
    artwork.host_configure(
        ledger=genai.Ledger(tmp_path / "ledger"), ceilings=lambda: (ceiling, ceiling)
    )
    try:
        state["status"] = "timeout"
        f = c._fal_for(tmp_path, "key", threading.Event(), cap)
        f.image_service, f.kie_key, f.task_dir = "kie", "key", tmp_path / "tasks"
        f._timeout = 0.01
        with pytest.raises(fal.FalError):
            f.generate("nano_banana_t2i", "bird")
        assert c.budget_used(tmp_path) == 2
        state["status"] = "success"
        f = c._fal_for(tmp_path, "key", threading.Event(), cap)
        f.image_service, f.kie_key, f.task_dir = "kie", "key", tmp_path / "tasks"
        f._timeout = 0.01
        assert f.generate("nano_banana_t2i", "bird") == png
        assert c.budget_used(tmp_path) == 2
        with pytest.raises(artwork.ArtworkError, match=message):
            f.generate("nano_banana_t2i", "another bird")
        assert len([u for u, _, _ in calls if u.endswith("createTask")]) == 1
    finally:
        artwork.host_configure()


def test_running_job_payload_survives_restart_at_cap(tmp_path, api, monkeypatch):
    calls, _state, png = api
    entered, release = threading.Event(), threading.Event()

    def make(job):
        assert job.payload == {"bird": "robin"}
        raw = job.fal.generate("nano_banana_t2i", "robin")
        entered.set()
        release.wait(3)
        return raw, {}

    monkeypatch.setattr(kie_images, "configuration", lambda: ("kie", "key"))
    c = artwork.Collection("kie_resume", "birds", make=make, label="Birds")
    c.start(c.job("robin", {"bird": "robin"}, "Robin", tmp_path, fal.DIRECT_ONLY), 2)
    assert entered.wait(3)
    rows = json.loads((tmp_path / "birds" / "queue.json").read_text())
    assert rows[0]["payload"] == {"bird": "robin"}
    c.stop("robin", tmp_path)
    monkeypatch.delitem(artwork._COLLECTIONS, ("kie_resume", "birds"))
    restarted = artwork.Collection("kie_resume", "birds", make=make, label="Birds")
    assert restarted.resume(tmp_path, fal.DIRECT_ONLY) == 1
    release.set()
    end = __import__("time").monotonic() + 3
    while restarted.path("robin", tmp_path) is None and __import__("time").monotonic() < end:
        __import__("time").sleep(0.01)
    assert restarted.picture("robin", tmp_path)[0] == png
    assert len([u for u, _, _ in calls if u.endswith("createTask")]) == 1


def test_transport_authenticates_create_and_poll(monkeypatch):
    seen = []

    def urlopen(req, timeout):
        seen.append(req)
        return io.BytesIO(b'{"code":200,"data":{}}')

    monkeypatch.setattr(kie_images.urllib.request, "urlopen", urlopen)
    kie_images._json(kie_images.BASE + "/jobs/createTask", {"input": {}}, "secret")
    kie_images._json(kie_images.BASE + "/jobs/recordInfo?taskId=paid", key="secret")
    assert [r.get_method() for r in seen] == ["POST", "GET"]
    assert all(r.get_header("Authorization") == "Bearer secret" for r in seen)


@pytest.mark.parametrize("code,message", [(401, "Invalid key"), (402, "Insufficient credits")])
def test_api_failure_messages_even_on_http_200(monkeypatch, code, message):
    monkeypatch.setattr(
        kie_images.urllib.request,
        "urlopen",
        lambda *a, **k: io.BytesIO(json.dumps({"code": code, "msg": message}).encode()),
    )
    with pytest.raises(kie_images.KieError, match=message):
        kie_images._json(kie_images.BASE + "/jobs/createTask", {}, "key")


def test_credits_refuse_payment(tmp_path, api, monkeypatch):
    calls, _, _ = api
    monkeypatch.setattr(kie_images, "credits", lambda key: 0)
    charges = []
    with pytest.raises(fal.FalError, match="insufficient credits"):
        client(tmp_path, charges).generate("nano_banana_t2i", "bird")
    assert not calls and not charges


def test_pro_keeps_aspect_and_1k_resolution(tmp_path, api):
    calls, _, png = api
    charges = []
    assert (
        client(tmp_path, charges).generate(
            "nano_banana_pro", "bird", refs=["https://ref.example/bird.jpg"], aspect="16:9"
        )
        == png
    )
    body = next(b for u, b, k in calls if u.endswith("createTask"))
    assert body["model"] == "nano-banana-pro"
    assert body["input"]["image_input"] == ["https://ref.example/bird.jpg"]
    assert body["input"]["resolution"] == "1K"
    assert body["input"]["aspect_ratio"] == "16:9"
    assert charges == [(9, "nano_banana_pro_kie")]


def test_widget_fallback_cannot_submit_after_a_pending_task(tmp_path, api):
    calls, state, _ = api
    state["status"] = "timeout"
    charges = []
    c = client(tmp_path, charges, timeout=0.01)
    for prompt in ("named spacecraft", "unnamed spacecraft"):
        with pytest.raises(fal.FalError, match="paid task"):
            c.generate("nano_banana_t2i", prompt)
    assert len([u for u, _, _ in calls if u.endswith("createTask")]) == 1
    assert len(charges) == 1


def test_render_now_collects_paid_task_when_daily_budget_is_spent(tmp_path, api, monkeypatch):
    import time

    calls, state, png = api
    monkeypatch.setattr(kie_images, "configuration", lambda: ("kie", "key"))

    def make(job):
        job.fal._timeout = 0.01
        return job.fal.generate("nano_banana_t2i", "robin"), {}

    c = artwork.Collection("kie_render", "birds", make=make, label="Birds")
    settings = {"gen_daily_usd": "0.02"}
    ctx = {"data_dir": tmp_path, "page_name": "Birds"}
    state["status"] = "timeout"
    art = c.open(ctx, settings)
    art.request("robin", {"bird": "robin"})
    art.finish(ctx)
    deadline = time.monotonic() + 3
    while c.state("robin", tmp_path) == "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert c.budget_used(tmp_path) == 2
    c.clear_error("robin", tmp_path)
    state["status"] = "success"
    assert c.render_now(tmp_path, fal.DIRECT_ONLY, settings) == (1, 1)
    deadline = time.monotonic() + 3
    while c.path("robin", tmp_path) is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert c.picture("robin", tmp_path)[0] == png
    assert len([u for u, _, _ in calls if u.endswith("createTask")]) == 1


def test_the_task_lock_lets_one_caller_in_at_a_time(tmp_path):
    inside = threading.Event()
    release = threading.Event()
    second = threading.Event()

    def first():
        with file_lock.locked(tmp_path / ".lock"):
            inside.set()
            release.wait(5)

    def then():
        with file_lock.locked(tmp_path / ".lock"):
            second.set()

    a = threading.Thread(target=first)
    a.start()
    assert inside.wait(5)
    b = threading.Thread(target=then)
    b.start()
    assert not second.wait(0.2)
    release.set()
    assert second.wait(5)
    a.join()
    b.join()


def test_no_module_needs_a_unix_only_import_to_load():
    """Windows has no fcntl, pwd, grp, resource or termios. One of them
    imported at the top of a module stops the server from starting there."""
    unix_only = {"fcntl", "pwd", "grp", "resource", "termios", "tty", "pty", "syslog"}
    root = Path(kie_images.__file__).resolve().parents[1]
    found = []
    for folder in ("app", "plugins", "transports", "renderers"):
        for path in (root / folder).rglob("*.py"):
            for node in ast.parse(path.read_text(encoding="utf-8")).body:
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.split(".")[0]]
                found += [f"{path.relative_to(root)}: {n}" for n in names if n in unix_only]
    assert found == []


def test_explicit_retry_archives_terminal_failure_only(tmp_path, api):
    calls, state, png = api
    state["status"] = "fail"
    charges = []
    with pytest.raises(fal.FalError):
        client(tmp_path, charges).generate("nano_banana_t2i", "bird")
    kie_images.retry(tmp_path)
    state["status"] = "success"
    assert client(tmp_path, charges).generate("nano_banana_t2i", "bird") == png
    assert len([u for u, _, _ in calls if u.endswith("createTask")]) == 2
    assert len(list((tmp_path / "archive").glob("*.json"))) == 1


def test_ambiguous_retry_requires_expiry_and_explicit_action(tmp_path, api, monkeypatch):
    calls, _, _ = api
    original = kie_images._json

    def request(url, *args, **kwargs):
        if url.endswith("createTask"):
            calls.append((url, None, "fake"))
            raise TimeoutError()
        return original(url, *args, **kwargs)

    monkeypatch.setattr(kie_images, "_json", request)
    charges = []
    with pytest.raises(fal.FalError):
        client(tmp_path, charges).generate("nano_banana_t2i", "bird")
    with pytest.raises(kie_images.KieError, match="Repaint"):
        kie_images.retry(tmp_path)
    monkeypatch.setattr(kie_images.time, "time", lambda: 10**12)
    with pytest.raises(fal.FalError, match="Repaint"):
        client(tmp_path, charges).generate("nano_banana_t2i", "bird")
    assert len([u for u, _, _ in calls if u.endswith("createTask")]) == 1
    kie_images.retry(tmp_path)
    with pytest.raises(fal.FalError):
        client(tmp_path, charges).generate("nano_banana_t2i", "bird")
    assert len([u for u, _, _ in calls if u.endswith("createTask")]) == 2


def test_download_failure_and_concurrent_collectors_submit_once(tmp_path, api, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    calls, _, png = api
    charges = []

    def failed_download(url):
        raise kie_images.KieError("temporary download failure")

    monkeypatch.setattr(kie_images, "_download", failed_download)
    with pytest.raises(fal.FalError, match="paid task"):
        client(tmp_path, charges, timeout=0.01).generate("nano_banana_t2i", "bird")
    monkeypatch.setattr(kie_images, "_download", lambda url: png)
    with ThreadPoolExecutor(2) as pool:
        list(
            pool.map(
                lambda _: client(tmp_path, charges).generate("nano_banana_t2i", "bird"), range(2)
            )
        )
    assert len([u for u, _, _ in calls if u.endswith("createTask")]) == 1
    assert len(charges) == 1


def test_unusable_download_is_not_paid_for_twice(tmp_path, api, monkeypatch):
    calls, _, _ = api
    charges = []
    monkeypatch.setattr(kie_images, "_download", lambda url: b"not an image")
    for _ in range(2):
        raw = client(tmp_path, charges).generate("nano_banana_t2i", "bird")
        with pytest.raises(OSError):
            Image.open(io.BytesIO(raw))
    assert len([u for u, _, _ in calls if u.endswith("createTask")]) == 1
    assert len(charges) == 1


def test_unreadable_ceiling_blocks_payment(tmp_path, api):
    calls, _, _ = api

    def broken():
        raise OSError("disk unavailable")

    ledger = genai.Ledger(tmp_path / "ledger")
    artwork.host_configure(ledger=ledger, ceilings=broken)
    try:
        c = artwork.Collection("closed", "images", make=lambda j: (b"", {}), label="Test")
        f = c._fal_for(tmp_path, "key", threading.Event(), 100)
        f.image_service, f.kie_key, f.task_dir = "kie", "key", tmp_path / "tasks"
        with pytest.raises(artwork.ArtworkError, match="ceiling"):
            f.generate("nano_banana_t2i", "bird")
        assert not [u for u, _, _ in calls if u.endswith("createTask")]
        assert ledger.today_cents() == 0
    finally:
        artwork.host_configure()


def test_ledger_failure_blocks_payment(tmp_path, api, monkeypatch):
    calls, _, _ = api
    ledger = genai.Ledger(tmp_path / "ledger")
    ledger.folder.mkdir()
    ledger.path.mkdir()  # A ledger that cannot be appended must fail closed.
    artwork.host_configure(ledger=ledger, ceilings=lambda: (100, 100))
    try:
        c = artwork.Collection("disk", "images", make=lambda j: (b"", {}), label="Test")
        f = c._fal_for(tmp_path, "key", threading.Event(), 100)
        f.image_service, f.kie_key, f.task_dir = "kie", "key", tmp_path / "tasks"
        with pytest.raises((OSError, artwork.ArtworkError)):
            f.generate("nano_banana_t2i", "bird")
        assert not [u for u, _, _ in calls if u.endswith("createTask")]
    finally:
        artwork.host_configure()


def test_two_processes_collect_same_task_once(tmp_path, api, monkeypatch):
    import multiprocessing

    _, _, png = api
    create_log = tmp_path / "paid.log"
    original = kie_images._json

    def request(url, *args, **kwargs):
        if url.endswith("createTask"):
            with create_log.open("a") as out:
                out.write("paid\n")
        return original(url, *args, **kwargs)

    monkeypatch.setattr(kie_images, "_json", request)
    ctx = multiprocessing.get_context("fork")
    start = ctx.Event()

    def run():
        start.wait(3)
        assert client(tmp_path, []).generate("nano_banana_t2i", "bird") == png

    workers = [ctx.Process(target=run) for _ in range(2)]
    for worker in workers:
        worker.start()
    start.set()
    for worker in workers:
        worker.join(5)
        assert worker.exitcode == 0
        worker.close()
    assert create_log.read_text() == "paid\n"


@pytest.mark.parametrize("pro,cost", [(False, 2), (True, 9)])
def test_price_matches_budget_and_ledger(tmp_path, api, pro, cost):
    ledger = genai.Ledger(tmp_path / "ledger")
    artwork.host_configure(ledger=ledger, ceilings=lambda: (cost, cost))
    try:
        c = artwork.Collection("prices", "images", make=lambda j: (b"", {}), label="Test")
        f = c._fal_for(tmp_path, "key", threading.Event(), cost)
        f.image_service, f.kie_key, f.task_dir = "kie", "key", tmp_path / "tasks"
        f.generate("nano_banana_pro" if pro else "nano_banana_t2i", "bird")
        assert c.budget_used(tmp_path) == ledger.today_cents() == ledger.month_cents() == cost
        assert len(ledger.rows()) == 1
    finally:
        artwork.host_configure()


def test_corrupt_ledger_blocks_new_submission(tmp_path, api):
    calls, _, _ = api
    ledger = genai.Ledger(tmp_path / "ledger")
    ledger.folder.mkdir()
    ledger.path.write_text("partial paid record")
    artwork.host_configure(ledger=ledger, ceilings=lambda: (100, 100))
    try:
        with pytest.raises(artwork.ArtworkError, match="ceiling"):
            artwork.charge("test", "images", 2, "nano_banana_kie", lambda: None)
        assert not [u for u, _, _ in calls if u.endswith("createTask")]
    finally:
        artwork.host_configure()


def test_repaint_failed_first_picture_recovers_its_payload(tmp_path, api, monkeypatch):
    import time

    calls, state, png = api
    monkeypatch.setattr(kie_images, "configuration", lambda: ("kie", "key"))

    def make(job):
        assert job.payload == {"bird": "robin"}
        return job.fal.generate("nano_banana_t2i", "robin"), {}

    c = artwork.Collection("first_failure", "birds", make=make, label="Birds")
    state["status"] = "fail"
    c._run_one(c.job("robin", {"bird": "robin"}, "Robin", tmp_path, fal.DIRECT_ONLY), 100)
    assert c.path("robin", tmp_path) is None
    state["status"] = "success"
    assert c.repaint("robin", tmp_path, fal.DIRECT_ONLY, {})
    end = time.monotonic() + 3
    while c.path("robin", tmp_path) is None and time.monotonic() < end:
        time.sleep(0.01)
    assert c.picture("robin", tmp_path)[0] == png
    assert len([u for u, _, _ in calls if u.endswith("createTask")]) == 2


@pytest.mark.parametrize("stage", ["create", "poll"])
def test_malformed_paid_reply_cannot_trigger_widget_fallback(tmp_path, api, monkeypatch, stage):
    calls, _, _ = api
    original = kie_images._json

    def request(url, *args, **kwargs):
        answer = original(url, *args, **kwargs)
        if (stage == "create" and url.endswith("createTask")) or (
            stage == "poll" and "recordInfo" in url
        ):
            return {"data": ["invalid provider reply"]}
        return answer

    monkeypatch.setattr(kie_images, "_json", request)
    charges = []
    c = client(tmp_path, charges, timeout=0.01)
    for prompt in ("bird", "widget fallback"):
        with pytest.raises(fal.FalError):
            c.generate("nano_banana_t2i", prompt, retries=5)
    assert len([u for u, _, _ in calls if u.endswith("createTask")]) == 1
    assert len(charges) == 1


def test_crash_between_create_and_task_save_stays_closed(tmp_path, api, monkeypatch):
    calls, _, _ = api
    original = kie_images._write

    def disk_failure(path, raw):
        if path.suffix == ".json" and json.loads(raw).get("task_id"):
            raise OSError("crash before task id reaches disk")
        original(path, raw)

    monkeypatch.setattr(kie_images, "_write", disk_failure)
    charges = []
    for _ in range(2):
        with pytest.raises(fal.FalError, match="unknown"):
            client(tmp_path, charges).generate("nano_banana_t2i", "bird")
    assert len([u for u, _, _ in calls if u.endswith("createTask")]) == 1
    assert len(charges) == 1


def test_two_processes_cannot_race_past_shared_ceiling(tmp_path, api, monkeypatch):
    import multiprocessing
    import time

    original = kie_images._json
    paid = tmp_path / "paid.log"

    def request(url, *args, **kwargs):
        if url.endswith("createTask"):
            with paid.open("a") as out:
                out.write("paid\n")
        return original(url, *args, **kwargs)

    monkeypatch.setattr(kie_images, "_json", request)
    ctx = multiprocessing.get_context("fork")
    start = ctx.Event()

    def run(number):
        ledger = genai.Ledger(tmp_path / "ledger")
        today = ledger.today_cents

        def slow_read():
            value = today()
            time.sleep(0.05)  # Both callers could otherwise check the old total.
            return value

        ledger.today_cents = slow_read
        artwork.host_configure(ledger=ledger, ceilings=lambda: (2, 2))
        c = artwork.Collection("race", "images", make=lambda j: (b"", {}), label="Test")
        f = c._fal_for(tmp_path, "key", threading.Event(), 100)
        f.image_service, f.kie_key, f.task_dir = "kie", "key", tmp_path / str(number)
        start.wait(3)
        with contextlib.suppress(artwork.ArtworkError):
            f.generate("nano_banana_t2i", "bird")

    workers = [ctx.Process(target=run, args=(n,)) for n in range(2)]
    for worker in workers:
        worker.start()
    start.set()
    for worker in workers:
        worker.join(5)
        assert worker.exitcode == 0
        worker.close()
    assert paid.read_text() == "paid\n"
    ledger = genai.Ledger(tmp_path / "ledger")
    assert ledger.today_cents() == ledger.month_cents() == 2


def test_changed_prompt_keeps_pending_task_protected(tmp_path, api):
    calls, state, _ = api
    state["status"] = "timeout"
    charges = []
    with pytest.raises(fal.FalError):
        client(tmp_path, charges, timeout=0.01).generate("nano_banana_t2i", "bird")
    state["status"] = "success"
    with pytest.raises(fal.FalError, match="different prompt"):
        client(tmp_path, charges).generate("nano_banana_t2i", "another bird")
    assert len([u for u, _, _ in calls if u.endswith("createTask")]) == 1


def test_restart_does_not_buy_an_interrupted_picture_again(tmp_path, monkeypatch):
    """A job under way at the restart was already charged. Without a saved
    kie.ai task to collect, starting it again would pay a second time."""
    made = []
    c = artwork.Collection(
        "kie_norequeue", "birds", make=lambda j: made.append(j.key) or (b"", {}), label="Birds"
    )
    folder = c.folder(tmp_path)
    folder.mkdir(parents=True, exist_ok=True)
    row = {"key": "robin", "payload": {"bird": "robin"}, "title": "Robin", "running": True}
    (folder / "queue.json").write_text(json.dumps([row]))
    assert c.resume(tmp_path, fal.DIRECT_ONLY) == 0
    assert not made
