"""领域规则与打包纯函数测试。"""
import hashlib
import zipfile

import pytest

from app.logic import (batches_by_company, feasible_pair, gap_analysis,
                       validate_meta)
from app.packaging import build_package


def _row(company, bno, ref, content=None):
    if content is None:
        content = f"{company}-{bno}-csv-body".encode()
    elif isinstance(content, str):
        content = content.encode()
    return {
        "company": company, "batch_no": bno, "max_other_ref": ref,
        "file_name": f"{company}{bno}.csv", "file_size": len(content),
        "file_sha256": hashlib.sha256(content).hexdigest(),
        "received_at": "2026-11-01 00:00:00", "content": content,
    }


def _brute_force_feasible(snapshot):
    """穷举所有 (a,b) 求分量最大可行对，作为不动点算法的对照。"""
    batches = batches_by_company(snapshot)
    best = (0, 0)
    for a in range(0, max(batches["A"].keys(), default=0) + 2):
        for b in range(0, max(batches["B"].keys(), default=0) + 2):
            ok = all(i in batches["A"] for i in range(1, a + 1)) and \
                 all(j in batches["B"] for j in range(1, b + 1))
            if not ok:
                continue
            for i in range(1, a + 1):
                if batches["A"][i].max_other_ref > b:
                    ok = False
            for j in range(1, b + 1):
                if batches["B"][j].max_other_ref > a:
                    ok = False
            if ok and (a >= best[0] and b >= best[1]):
                best = (a, b)
    return best


def test_validate_meta():
    assert validate_meta("a", "3", "0") == ("A", 3, 0)
    with pytest.raises(ValueError):
        validate_meta("C", "1", "0")
    with pytest.raises(ValueError):
        validate_meta("A", "0", "0")
    with pytest.raises(ValueError):
        validate_meta("A", "1", "-1")
    with pytest.raises(ValueError):
        validate_meta("A", "x", "1")


def test_continuous_prefix_and_gap_out_of_order():
    snap = [_row("A", 1, 0), _row("A", 3, 0), _row("B", 1, 1)]
    batches = batches_by_company(snap)
    pair = feasible_pair(batches)
    # A 缺第 2 批 -> 连续前缀只到 1；B 前缀 1，B1 引用 A1 已满足
    assert pair == (1, 1)
    gaps = {g["company"]: g for g in gap_analysis(batches, pair)}
    assert gaps["A"]["missing_next"] == 2
    assert gaps["A"]["out_of_order"] == [3]
    assert gaps["B"]["contiguous_to"] == 1


def test_reference_holds_back_pair():
    # A1 引用 B2，但 B 只到 B1 -> 对被压回 (0,0) 之外的可行：A 不能纳入任何引用 B2 的批
    snap = [_row("A", 1, 2), _row("B", 1, 0)]
    assert feasible_pair(batches_by_company(snap)) == (0, 1)
    # 补齐 B2 后 (1,2) 成立
    snap.append(_row("B", 2, 0))
    assert feasible_pair(batches_by_company(snap)) == (1, 2)


def test_mutual_reference_enters_together():
    # A2 引 B2，B2 引 A2，互引闭环，原档齐备时一起入包
    snap = [
        _row("A", 1, 0), _row("A", 2, 2),
        _row("B", 1, 0), _row("B", 2, 2),
    ]
    assert feasible_pair(batches_by_company(snap)) == (2, 2)


def test_fixed_point_matches_brute_force():
    # 随机化穷举对照
    import random
    rng = random.Random(42)
    for trial in range(200):
        snap = []
        for company, max_n in (("A", rng.randint(0, 6)), ("B", rng.randint(0, 6))):
            present = set()
            for n in range(1, max_n + 1):
                if rng.random() < 0.85:  # 制造一些缺批
                    present.add(n)
            for n in sorted(present):
                ref_cap = rng.randint(0, 7)
                snap.append(_row(company, n, ref_cap))
        got = feasible_pair(batches_by_company(snap))
        want = _brute_force_feasible(snap)
        assert got == want, f"trial {trial}: {got} != {want}"


def test_build_package_zip_contents(tmp_path):
    snap = [
        _row("A", 1, 0, b"card,a1\n1,2\n"),
        _row("A", 2, 2, b"card,a2\n"),
        _row("B", 1, 0, b"card,b1\n"),
        _row("B", 2, 1, b"card,b2\n"),
    ]
    pair, path, name, sha, size, manifest = build_package(snap, str(tmp_path))
    assert pair == (2, 2)
    assert name.startswith("settlement_package_A2_B2_")
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        assert "manifest.csv" in names and "package_info.json" in names
        members = [n for n in names
                   if n.endswith(".csv") and n.startswith("company_")]
        assert len(members) == 4
        assert all(n.startswith(("company_A/", "company_B/")) for n in members)
        # 原档字节原样入包
        a1 = next(n for n in members if "batch_0001" in n and n.startswith("company_A"))
        assert zf.read(a1) == b"card,a1\n1,2\n"
        manifest_text = zf.read("manifest.csv").decode("utf-8-sig")
        assert "company,batch_no,max_other_ref" in manifest_text
    assert len(manifest) == 4


def test_build_package_refuses_without_both_first_batches(tmp_path):
    with pytest.raises(RuntimeError):
        build_package([_row("A", 1, 0)], str(tmp_path))


def test_build_package_failure_injection(tmp_path, monkeypatch):
    snap = [_row("A", 1, 0), _row("B", 1, 0)]
    monkeypatch.setenv("SETTLE_PACKAGE_FAULT", "1")
    with pytest.raises(RuntimeError):
        build_package(snap, str(tmp_path))
    # 没有半成品残留
    assert not list(tmp_path.glob("*.zip"))
    monkeypatch.delenv("SETTLE_PACKAGE_FAULT")
    pair, *_ = build_package(snap, str(tmp_path))
    assert pair == (1, 1)


def test_snapshot_isolation(tmp_path):
    # 快照之后到的新件不影响本次结果
    snap1 = [_row("A", 1, 0), _row("B", 1, 0)]
    pair1, *_ = build_package(snap1, str(tmp_path / "run1"))
    assert pair1 == (1, 1)
    snap2 = snap1 + [_row("A", 2, 0), _row("B", 2, 0)]
    pair2, *_ = build_package(snap2, str(tmp_path / "run2"))
    assert pair2 == (2, 2)
