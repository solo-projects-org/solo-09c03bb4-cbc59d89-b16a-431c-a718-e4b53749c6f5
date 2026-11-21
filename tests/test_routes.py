"""Flask 端到端：收件规则、打包状态机、下载门槛、快照隔离。"""
import io
import time
import zipfile

from app import create_app, db


def _client():
    app = create_app()
    app.config["TESTING"] = True
    # 每个测试文件共用一个临时库，这里清表保证独立
    with db.open_db() as conn:
        conn.execute("DELETE FROM arrivals")
        conn.execute("DELETE FROM package_runs")
    return app.test_client()


def _upload(client, company, bno, ref, body=b"x,y\n1,2\n", fname=None):
    data = {
        "company": company,
        "batch_no": str(bno),
        "max_other_ref": str(ref),
        "csv_file": (io.BytesIO(body), fname or f"{company}{bno}.csv"),
    }
    return client.post("/upload", data=data, content_type="multipart/form-data",
                       follow_redirects=True)


def _text(resp):
    return resp.data.decode("utf-8")


def test_index_ok():
    client = _client()
    assert client.get("/").status_code == 200


def test_upload_accepts_out_of_order_and_lists_gaps():
    client = _client()
    # 乱序：先到 A3，再到 A1/A2；B 尚无一档
    r = _upload(client, "A", 3, 0)
    assert "已收件" in _text(r)
    _upload(client, "A", 1, 0)
    page = _text(_upload(client, "A", 2, 0))
    # B 一批未到：B 纳入为 0；A 三批已齐且不引用对方，可行对为 (3, 0)
    assert "A（甲公司）纳入到第 3 批".replace(" ", "") in page.replace(" ", "")
    assert "B（乙公司）纳入到第 0 批".replace(" ", "") in page.replace(" ", "")
    assert "已到但乱序" in page
    with db.open_db() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM arrivals").fetchone()["c"] == 3

    # B1 到达后，双方各纳入到 1（A2/A3 因连续纳入规则随 A 前缀一起可用）
    page = _text(_upload(client, "B", 1, 1))
    assert "B（乙公司）纳入到第 1 批".replace(" ", "") in page.replace(" ", "")


def test_idempotent_duplicate_accepted_once():
    client = _client()
    body = b"a,b\n1,2\n"
    _upload(client, "B", 1, 1, body=body)
    r = _upload(client, "B", 1, 1, body=body)
    assert "幂等忽略" in _text(r)
    with db.open_db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM arrivals WHERE company='B' AND batch_no=1"
        ).fetchone()["c"] == 1


def test_same_number_changed_content_or_ref_rejected():
    client = _client()
    _upload(client, "B", 1, 1, body=b"v1\n")
    # 改原档
    r = _upload(client, "B", 1, 1, body=b"v2-different\n")
    assert "退回" in _text(r)
    # 改引用
    r = _upload(client, "B", 1, 2, body=b"v1\n")
    assert "退回" in _text(r)
    with db.open_db() as conn:
        row = conn.execute(
            "SELECT * FROM arrivals WHERE company='B' AND batch_no=1"
        ).fetchone()
        assert row["max_other_ref"] == 1 and row["file_sha256"]


def test_bad_form_rejected():
    client = _client()
    r = _upload(client, "X", 1, 0)
    assert "退回" in _text(r)
    r = _upload(client, "A", 0, 0)
    assert "退回" in _text(r)


def _wait_status(client, run_id, want, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        client.get("/")
        with db.open_db() as conn:
            row = conn.execute(
                "SELECT status FROM package_runs WHERE id=?", (run_id,)
            ).fetchone()
        if row and row["status"] == want:
            return row
        if row and row["status"] != "running":
            return row
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach {want}")


def _latest_run_id():
    with db.open_db() as conn:
        return conn.execute("SELECT MAX(id) m FROM package_runs").fetchone()["m"]


def test_package_ready_then_download_only_when_ready():
    client = _client()
    _upload(client, "A", 1, 0, b"A1,data\n")
    _upload(client, "B", 1, 1, b"B1,data\n")
    client.post("/packages/start")
    run_id = _latest_run_id()
    row = _wait_status(client, run_id, "ready")
    assert row["status"] == "ready"
    resp = client.get(f"/packages/download/{run_id}")
    assert resp.status_code == 200
    assert resp.mimetype == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    originals = [n for n in zf.namelist() if n.startswith("company_")]
    assert "manifest.csv" in zf.namelist()
    assert len(originals) == 2


def test_running_package_blocks_second_start():
    client = _client()
    _upload(client, "A", 1, 0)
    _upload(client, "B", 1, 0)
    client.post("/packages/start")
    first = _latest_run_id()
    r = client.post("/packages/start", follow_redirects=True)
    assert "进行中" in _text(r)
    _wait_status(client, first, "ready")


def test_snapshot_freezes_arrivals_during_build():
    client = _client()
    _upload(client, "A", 1, 0)
    _upload(client, "B", 1, 0)
    client.post("/packages/start")
    run_id = _latest_run_id()
    # 打包进行期间继续到件（后台线程很快，这里即便已完成语义也成立：
    # 新件不会回写进已生成的包）
    _upload(client, "A", 2, 0)
    _upload(client, "B", 2, 0)
    _wait_status(client, run_id, "ready")
    with db.open_db() as conn:
        row = conn.execute("SELECT * FROM package_runs WHERE id=?", (run_id,)).fetchone()
    assert (row["pair_a"], row["pair_b"]) == (1, 1)
    resp = client.get(f"/packages/download/{run_id}")
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    originals = [n for n in zf.namelist() if n.startswith("company_")]
    assert len(originals) == 2
    # 再出一次包即纳入新到的第 2 批
    client.post("/packages/start")
    run2 = _latest_run_id()
    _wait_status(client, run2, "ready")
    with db.open_db() as conn:
        row2 = conn.execute("SELECT * FROM package_runs WHERE id=?", (run2,)).fetchone()
    assert (row2["pair_a"], row2["pair_b"]) == (2, 2)


def test_failure_then_retry_uses_current_arrivals(monkeypatch):
    client = _client()
    _upload(client, "A", 1, 0)
    # 只有 A1：可行对含不到 B1，出包失败
    client.post("/packages/start")
    run_id = _latest_run_id()
    row = _wait_status(client, run_id, "failed")
    assert row["status"] == "failed"
    # 未就绪禁止下载
    assert client.get(f"/packages/download/{run_id}").status_code == 409
    # 补齐 B1 后重试成功
    _upload(client, "B", 1, 0)
    client.post(f"/packages/retry/{run_id}")
    new_id = _latest_run_id()
    assert new_id != run_id
    row = _wait_status(client, new_id, "ready")
    assert row["status"] == "ready"


def test_fault_injection_then_retry_succeeds(monkeypatch):
    client = _client()
    _upload(client, "A", 1, 0)
    _upload(client, "B", 1, 0)
    monkeypatch.setenv("SETTLE_PACKAGE_FAULT", "1")
    client.post("/packages/start")
    run_id = _latest_run_id()
    _wait_status(client, run_id, "failed")
    monkeypatch.delenv("SETTLE_PACKAGE_FAULT")
    client.post(f"/packages/retry/{run_id}")
    new_id = _latest_run_id()
    row = _wait_status(client, new_id, "ready")
    assert row["status"] == "ready"
