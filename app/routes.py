"""Flask 路由：收件上传、状态页、开始打包/重试、完整生成后下载。"""
from __future__ import annotations

import hashlib
import threading

from flask import (Blueprint, abort, flash, redirect, render_template,
                   request, send_file, url_for)

from . import db, packaging
from .logic import (COMPANIES, COMPANY_LABEL, batches_by_company,
                    feasible_pair, gap_analysis, validate_meta)

bp = Blueprint("main", __name__)


def _load_state(conn):
    rows = conn.execute(
        "SELECT company, batch_no, max_other_ref, file_name, file_size,"
        " file_sha256, received_at FROM arrivals ORDER BY company, batch_no"
    ).fetchall()
    batches = batches_by_company(rows)
    pair = feasible_pair(batches)
    gaps = gap_analysis(batches, pair)
    return rows, batches, pair, gaps


@bp.route("/")
def index():
    with db.open_db() as conn:
        rows, batches, pair, gaps = _load_state(conn)
        runs = conn.execute(
            "SELECT * FROM package_runs ORDER BY id DESC LIMIT 10"
        ).fetchall()
    return render_template(
        "index.html",
        rows=rows,
        batches=batches,
        pair=pair,
        gaps=gaps,
        runs=runs,
        companies=COMPANIES,
        labels=COMPANY_LABEL,
    )


@bp.route("/upload", methods=["POST"])
def upload():
    company = (request.form.get("company") or "").upper()
    batch_raw = request.form.get("batch_no")
    ref_raw = request.form.get("max_other_ref")
    f = request.files.get("csv_file")

    try:
        company, bno, mref = validate_meta(company, batch_raw, ref_raw)
        if f is None or not f.filename:
            raise ValueError("请选择要上传的 CSV 文件")
        if not f.filename.lower().endswith(".csv"):
            raise ValueError("只接收 .csv 文件")
        content = f.read()
        if not content:
            raise ValueError("CSV 文件内容为空")
        if len(content) > 20 * 1024 * 1024:
            raise ValueError("单文件超过 20MB 限制")
    except ValueError as exc:
        flash(f"退回：{exc}", "error")
        return redirect(url_for("main.index"))

    sha = hashlib.sha256(content).hexdigest()
    with db.write_lock(), db.open_db() as conn:
        existing = conn.execute(
            "SELECT file_sha256, max_other_ref FROM arrivals WHERE company=? AND batch_no=?",
            (company, bno),
        ).fetchone()
        if existing is not None:
            # 原样重传（同号、同引用、同内容）只收一次；改原档或改引用一律退回
            if existing["file_sha256"] == sha and existing["max_other_ref"] == mref:
                flash(
                    f"{COMPANY_LABEL[company]}第 {bno} 批与已收原档完全一致，幂等忽略（只收一次）",
                    "info",
                )
            else:
                flash(
                    f"退回：{COMPANY_LABEL[company]}第 {bno} 批已收原档，"
                    "同号改原档或改引用不允许，请使用新批号",
                    "error",
                )
            return redirect(url_for("main.index"))

        conn.execute(
            "INSERT INTO arrivals (company, batch_no, max_other_ref, file_name,"
            " file_size, file_sha256, content) VALUES (?,?,?,?,?,?,?)",
            (company, bno, mref, f.filename, len(content), sha, content),
        )
    flash(f"已收件：{COMPANY_LABEL[company]}第 {bno} 批（引用对方最高批 {mref}）", "ok")
    return redirect(url_for("main.index"))


@bp.route("/packages/start", methods=["POST"])
def start_package():
    with db.write_lock(), db.open_db() as conn:
        running = conn.execute(
            "SELECT id FROM package_runs WHERE status='running'"
        ).fetchone()
        if running is not None:
            flash(f"已有打包任务 #{running['id']} 进行中，请稍候", "error")
            return redirect(url_for("main.index"))
        snapshot = packaging.snapshot_arrivals(conn)
        run_id = packaging.create_run(conn, snapshot)
    # 快照已含原档字节；打包期间照常收件，不影响本次结果
    threading.Thread(target=packaging.run_build, args=(run_id, snapshot),
                     daemon=True).start()
    flash(f"打包任务 #{run_id} 已开始（以开始时已收原档为准）", "ok")
    return redirect(url_for("main.index"))


@bp.route("/packages/retry/<int:run_id>", methods=["POST"])
def retry_package(run_id: int):
    with db.write_lock(), db.open_db() as conn:
        old = conn.execute(
            "SELECT status FROM package_runs WHERE id=?", (run_id,)
        ).fetchone()
        if old is None:
            abort(404)
        if old["status"] != "failed":
            flash("只有失败的任务可以重试", "error")
            return redirect(url_for("main.index"))
        running = conn.execute(
            "SELECT id FROM package_runs WHERE status='running'"
        ).fetchone()
        if running is not None:
            flash(f"已有打包任务 #{running['id']} 进行中，请稍候", "error")
            return redirect(url_for("main.index"))
        # 重试：用当前已收原档重新取快照（失败期间到的新件这次会纳入）
        snapshot = packaging.snapshot_arrivals(conn)
        new_id = packaging.create_run(conn, snapshot)
    threading.Thread(target=packaging.run_build, args=(new_id, snapshot),
                     daemon=True).start()
    flash(f"已用当前已收原档重试，新任务 #{new_id}（原失败任务 #{run_id}）", "ok")
    return redirect(url_for("main.index"))


@bp.route("/packages/download/<int:run_id>")
def download(run_id: int):
    with db.open_db() as conn:
        run = conn.execute(
            "SELECT * FROM package_runs WHERE id=?", (run_id,)
        ).fetchone()
    if run is None:
        abort(404)
    if run["status"] != "ready":
        abort(409, description="包尚未完整生成，不能下载")
    if not run["file_path"]:
        abort(404)
    return send_file(
        run["file_path"],
        mimetype="application/zip",
        as_attachment=True,
        download_name=run["file_name"],
    )
