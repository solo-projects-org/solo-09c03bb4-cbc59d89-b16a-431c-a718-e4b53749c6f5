"""领域规则：公司、批号/引用校验、最靠后可行批号对、缺口分析。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Tuple

COMPANIES = ("A", "B")
COMPANY_LABEL = {"A": "甲公司", "B": "乙公司"}


@dataclass(frozen=True)
class Batch:
    company: str
    batch_no: int
    max_other_ref: int
    file_name: str
    file_size: int
    file_sha256: str
    received_at: str


def validate_meta(company: str, batch_no: str, max_other_ref: str) -> Tuple[str, int, int]:
    """校验上传表单里的公司与两个批号字段，返回规范化后的三元组。"""
    company = (company or "").strip().upper()
    if company not in COMPANIES:
        raise ValueError("公司必须是 A 或 B")
    try:
        bno = int(str(batch_no).strip())
        mref = int(str(max_other_ref).strip())
    except (TypeError, ValueError):
        raise ValueError("批号和对方最高批号都必须是整数")
    if bno < 1:
        raise ValueError("批号必须从 1 起编（>=1）")
    if mref < 0:
        raise ValueError("对方最高批号不能为负，没有引用请填 0")
    return company, bno, mref


def batches_by_company(rows) -> Dict[str, Dict[int, Batch]]:
    """把 arrivals 行整理成 {公司: {批号: Batch}}。"""
    out: Dict[str, Dict[int, Batch]] = {c: {} for c in COMPANIES}
    for r in rows:
        out[r["company"]][r["batch_no"]] = Batch(
            company=r["company"],
            batch_no=r["batch_no"],
            max_other_ref=r["max_other_ref"],
            file_name=r["file_name"],
            file_size=r["file_size"],
            file_sha256=r["file_sha256"],
            received_at=r["received_at"],
        )
    return out


def contiguous_prefix(have: Mapping[int, Batch]) -> int:
    """从第 1 批起连续收到的最大批号（一批都没有返回 0）。"""
    n = 0
    while n + 1 in have:
        n += 1
    return n


def feasible_pair(batches: Mapping[str, Mapping[int, Batch]]) -> Tuple[int, int]:
    """
    求能同时成立的最靠后一对批号 (a, b)：
      1) A 的 1..a、B 的 1..b 全部原档已收（连续纳入）；
      2) 包内任一批引用的对方最高批号，必须落在对方纳入范围内；
      3) 引用的对方批次自然要求其原档已收（由条件 1 连带保证）。
    可行域是下闭且对 max 封闭的，因此从连续前缀上界做不动点收缩，
    收敛到的就是唯一的分量最大可行对（最靠后的一对）。
    """
    a = contiguous_prefix(batches["A"])
    b = contiguous_prefix(batches["B"])
    while True:
        # 已纳入的 A 批次对 B 的最高引用、已纳入的 B 批次对 A 的最高引用
        a_need_b = max(
            (batches["A"][i].max_other_ref for i in range(1, a + 1)),
            default=0,
        )
        b_need_a = max(
            (batches["B"][i].max_other_ref for i in range(1, b + 1)),
            default=0,
        )
        na, nb = a, b
        # A 侧引用了超出 B 纳入范围的批次：把制造引用的 A 批排除（缩 a）
        if a_need_b > nb:
            na = min(na, _last_satisfying(batches["A"], a, nb))
        # 对称地处理 B 侧
        if b_need_a > na:
            nb = min(nb, _last_satisfying(batches["B"], b, na))
        if (na, nb) == (a, b):
            return na, nb
        a, b = na, nb


def _last_satisfying(have: Mapping[int, Batch], upper: int,
                     other_limit: int) -> int:
    """在 1..upper 中找最后一个“引用不超过对方纳入范围”的批号；
    一旦某个批号越界，它及之后的批次本轮都不能纳入，返回其前一号。"""
    for i in range(1, upper + 1):
        if have[i].max_other_ref > other_limit:
            return i - 1
    return upper


def gap_analysis(batches: Mapping[str, Mapping[int, Batch]],
                 pair: Tuple[int, int]) -> List[dict]:
    """列出双方还缺哪些原档：连续前缀的断点 + 已收到的乱序批次。"""
    a, b = pair
    items: List[dict] = []
    for company in COMPANIES:
        have = batches[company]
        prefix = contiguous_prefix(have)
        missing = prefix + 1  # 下一个本该到的批号
        out_of_order = sorted(k for k in have if k > prefix)
        items.append({
            "company": company,
            "label": COMPANY_LABEL[company],
            "included_to": pair[0] if company == "A" else pair[1],
            "contiguous_to": prefix,
            "missing_next": missing if missing not in have else None,
            "out_of_order": out_of_order,
            "total_received": len(have),
        })
    return items


def included_batches(batches: Mapping[str, Mapping[int, Batch]],
                     pair: Tuple[int, int]) -> List[Batch]:
    """按入包范围取出批次（A 侧 1..a，B 侧 1..b），按公司、批号排序。"""
    a, b = pair
    chosen: List[Batch] = []
    for i in range(1, a + 1):
        chosen.append(batches["A"][i])
    for i in range(1, b + 1):
        chosen.append(batches["B"][i])
    return chosen
