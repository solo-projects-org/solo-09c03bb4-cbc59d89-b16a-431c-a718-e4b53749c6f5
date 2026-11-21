"""出包：开始时快照已收原档 -> 求最靠后批号对 -> 组装 ZIP -> 原子落盘。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import List

from . import db
from .logic import (COMPANIES, COMPANY_LABEL, batches_by_company,
                    feasible_pair, gap_analysis, included_batches)

MANIFEST_NAME = "manifest.csv"
INFO_NAME = "package_info.json"


def packages_dir() -> str:
    return os.environ.get("SETTLE_PACKAGES", "/data/packages")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot_arrivals(conn) -> List[dict]:
    """开始打包的瞬间，把当时已收的原档整份读入内存（含 CSV 字节）。

    已收件在库里不可变（同号改原档直接退回），后续期间到的新件不会进入本快照。
    """
    rows = conn.execute(
        "SELECT company, batch_no, max_other_ref, file_name, file_size,"
        " file_sha256, content, received_at FROM arrivals ORDER BY company, batch_no"
    ).fetchall()
    return [dict(r) for r in rows]


def create_run(conn, snapshot: List[dict]) -> int:
    meta = [{k: v for k, v in row.items() if k != "content"} for row in snapshot]
    cur = conn.execute(
        "INSERT INTO package_runs (status, snapshot) VALUES ('running', ?)",
        (json.dumps(meta, ensure_ascii=False),),
    )
    return int(cur.lastrowid)


def _safe_name(name: str) -> str:
    name = os.path.basename(name).replace("\\", "_").replace("/", "_")
    name = re.sub(r"[^0-9A-Za-z._\-一-鿿]+", "_", name)
    return name or "unnamed.csv"


def build_package(snapshot: List[dict], out_dir: str):
    """纯函数式组装：返回 (pair, zip_path, zip_name, sha256, size, manifest_rows)。

    任何异常都留给上层记成 failed；不落半成品，重试时重新取快照即可。
    """
    # 测试/演练用的故障注入：SETTLE_PACKAGE_FAULT=1 时本次组装直接抛错
    if os.environ.get("SETTLE_PACKAGE_FAULT") == "1":
        raise RuntimeError("故障注入：模拟出包写入失败")

    batches = batches_by_company(snapshot)
    pair = feasible_pair(batches)
    a, b = pair
    if a < 1 or b < 1:
        raise RuntimeError(
            f"双方第 1 批原档尚未齐备（当前可行对仅为 A=1..{a}, B=1..{b}），不能出包"
        )

    chosen = included_batches(batches, pair)
    # 再校验一遍引用闭包，双保险
    for bx in chosen:
        need = bx.max_other_ref
        if need:
            other = "B" if bx.company == "A" else "A"
            limit = b if other == "B" else a
            if need > limit or need not in batches[other]:
                raise RuntimeError(
                    f"{bx.company} 第 {bx.batch_no} 批引用 {other} 第 {need} 批，原档未纳入"
                )

    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tmp_fd, tmp_path = tempfile.mkstemp(prefix=".tmp-pkg-", suffix=".zip", dir=out_dir)
    os.close(tmp_fd)
    manifest_rows = []
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            used = set()
            for bx in chosen:
                row = next(r for r in snapshot
                           if r["company"] == bx.company and r["batch_no"] == bx.batch_no)
                content = row["content"]
                if _sha256(content) != bx.file_sha256:
                    raise RuntimeError("快照原档校验和不一致，终止出包")
                base = _safe_name(bx.file_name)
                member = f"company_{bx.company}/batch_{bx.batch_no:04d}_{base}"
                # 极端重名兜底
                if member in used:
                    member = f"company_{bx.company}/batch_{bx.batch_no:04d}_{bx.file_sha256[:8]}_{base}"
                used.add(member)
                zf.writestr(member, content)
                manifest_rows.append({
                    "company": bx.company,
                    "batch_no": bx.batch_no,
                    "max_other_ref": bx.max_other_ref,
                    "original_file_name": bx.file_name,
                    "zip_member": member,
                    "file_size": bx.file_size,
                    "file_sha256": bx.file_sha256,
                    "received_at": bx.received_at,
                })

            import csv as _csv
            import io
            buf = io.StringIO()
            buf.write("﻿")
            writer = _csv.DictWriter(buf, fieldnames=list(manifest_rows[0].keys()))
            writer.writeheader()
            writer.writerows(manifest_rows)
            zf.writestr(MANIFEST_NAME, buf.getvalue().encode("utf-8"))

            info = {
                "generated_at_utc": stamp,
                "pair": {"A_last_batch": a, "B_last_batch": b},
                "companies": {
                    c: {"label": COMPANY_LABEL[c],
                        "included_batches": list(range(1, pair[i] + 1))}
                    for i, c in enumerate(COMPANIES)
                },
                "gaps_at_snapshot": gap_analysis(batches, pair),
                "batch_count": len(chosen),
            }
            zf.writestr(INFO_NAME, json.dumps(info, ensure_ascii=False, indent=2))

        with open(tmp_path, "rb") as fh:
            data = fh.read()
        zip_sha = _sha256(data)
        zip_size = len(data)
        zip_name = f"settlement_package_A{a}_B{b}_{stamp}.zip"
        final_path = os.path.join(out_dir, zip_name)
        os.replace(tmp_path, final_path)
        os.sync()
        return pair, final_path, zip_name, zip_sha, zip_size, manifest_rows
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def run_build(run_id: int, snapshot: List[dict]) -> None:
    """后台线程入口：独立连接，组装成功才置 ready，否则置 failed 并保留现场。"""
    conn = db.open_db()
    try:
        try:
            pair, path, name, sha, size, _ = build_package(snapshot, packages_dir())
            with db.write_lock(), conn:
                conn.execute(
                    "UPDATE package_runs SET status='ready', finished_at=datetime('now'),"
                    " pair_a=?, pair_b=?, file_path=?, file_name=?, zip_sha256=?,"
                    " zip_size=?, error=NULL WHERE id=?",
                    (pair[0], pair[1], path, name, sha, size, run_id),
                )
        except Exception as exc:  # noqa: BLE001 - 任何失败都要落库，供重试
            with db.write_lock(), conn:
                conn.execute(
                    "UPDATE package_runs SET status='failed', finished_at=datetime('now'),"
                    " error=? WHERE id=?",
                    (f"{type(exc).__name__}: {exc}", run_id),
                )
    finally:
        conn.close()
