#!/usr/bin/env python3
"""网页试跑样例：生成样例 CSV，通过 HTTP 走一遍乱序到件、幂等/退回、互引入包、失败重试。

用法：
  1) 先启动服务：  docker compose up --build -d   （或 python wsgi.py）
  2) 再运行本脚本：python demo/run_demo.py
"""
from __future__ import annotations

import csv
import io
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from html.parser import HTMLParser

BASE = os.environ.get("SETTLE_BASE_URL", "http://127.0.0.1:8000")
OUT = os.environ.get("SETTLE_DEMO_DIR", os.path.join(os.path.dirname(__file__), "sample_csv"))

# Flask flash 消息走会话 cookie，需要跨 302 重定向保留 cookie
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())


class _FlashParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.flashes: list[str] = []
        self._depth = 0
        self._buf = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div" and "flash" in attrs.get("class", "").split():
            self._depth = 1
            self._buf = ""
        elif self._depth:
            self._depth += 1

    def handle_endtag(self, tag):
        if self._depth:
            self._depth -= 1
            if self._depth == 0:
                text = " ".join(self._buf.split())
                if text:
                    self.flashes.append(text)

    def handle_data(self, data):
        if self._depth:
            self._buf += data


class _RunParser(HTMLParser):
    """抓取任务表每行的 td 文本（嵌套 span 的文案会并入同一个单元格）。"""
    def __init__(self):
        super().__init__()
        self.runs: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag == "td" and self._row is not None:
            self._cell = []
    def handle_endtag(self, tag):
        if tag == "td" and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.runs.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def make_csv(company: str, batch: int) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["card_id", "tap_time", "route", "amount", "transfer_flag"])
    for i in range(5):
        w.writerow([
            f"{company}-{batch:02d}-{1000 + i}",
            f"2026-10-{(batch % 28) + 1:02d} 08:{10 + i:02d}:00",
            f"R{company}{batch}", 1.5 + i * 0.1, 1 if i % 2 else 0,
        ])
    return buf.getvalue().encode("utf-8")


def _multipart(company, bno, ref, content: bytes, filename: str) -> tuple[bytes, str]:
    boundary = "----settle-demo-" + uuid.uuid4().hex
    parts = []
    for name, value in (("company", company), ("batch_no", str(bno)),
                        ("max_other_ref", str(ref))):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    parts.append(
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"csv_file\"; "
         f"filename=\"{filename}\"\r\nContent-Type: text/csv\r\n\r\n").encode()
        + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def upload(company, bno, ref, content=None, filename=None):
    content = content if content is not None else make_csv(company, bno)
    filename = filename or f"{company}_batch_{bno:02d}.csv"
    body, ctype = _multipart(company, bno, ref, content, filename)
    req = urllib.request.Request(BASE + "/upload", data=body,
                                 headers={"Content-Type": ctype}, method="POST")
    try:
        with _OPENER.open(req) as resp:
            html = resp.read().decode()
    except urllib.error.HTTPError as e:
        html = e.read().decode()
    p = _FlashParser()
    p.feed(html)
    return p.flashes or ["(无提示)"]


def post(path):
    req = urllib.request.Request(BASE + path, data=b"", method="POST")
    with _OPENER.open(req) as resp:
        return resp.read().decode()


def get(path):
    with _OPENER.open(BASE + path) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


def wait_run(timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        html, _ = get("/")
        p = _RunParser()
        p.feed(html.decode())
        # 页面有多张表，任务表行的特征：首列是纯数字任务号，第 2 列是状态
        task_rows = [r for r in p.runs
                     if len(r) >= 5 and r[0].isdigit()
                     and r[1] in ("running", "ready", "failed")]
        if task_rows:
            cells = task_rows[0]  # 最新一行：# / 状态 / 开始 / 结束 / 批号对 / ZIP / 操作
            run_id, status, pair = cells[0], cells[1], cells[4]
            if status in ("ready", "failed"):
                return run_id, status, pair
        time.sleep(0.3)
    raise TimeoutError("打包任务超时未结束")


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    print(f"== 目标服务 {BASE}，样例 CSV 目录 {OUT} ==")
    try:
        urllib.request.urlopen(BASE + "/", timeout=5)
    except Exception as exc:
        print(f"服务不可达：{exc}\n请先 docker compose up --build -d")
        return 2

    # 1) 乱序到件：先到 A3/A2，再到 A1；B1/B2 穿插
    print("\n[1] 乱序到件")
    for c, b, ref in (("A", 3, 2), ("A", 2, 2), ("B", 1, 1), ("A", 1, 0), ("B", 2, 2)):
        data = make_csv(c, b)
        with open(os.path.join(OUT, f"{c}_batch_{b:02d}.csv"), "wb") as fh:
            fh.write(data)
        print(f"  上传 {c} 第{b}批 引用对方最高批{ref}: {upload(c, b, ref, data)}")

    # 2) 幂等重传 + 同号改原档/改引用退回
    print("\n[2] 重传规则")
    same = make_csv("A", 1)
    print("  原样重传 A1:", upload("A", 1, 0, same))
    print("  改原档重传 A1:", upload("A", 1, 0, b"tampered,content\n"))
    print("  改引用重传 A1:", upload("A", 1, 9, same))

    # 3) 第一次出包：A2<->B2 互引、A3 引 B2，原档齐备 -> 最靠后对 (3,2)；
    #    打包期间才到的 A4 存在连续断点（无 A4 之后件，此处 A4 仅不入本快照包）
    print("\n[3] 开始打包（快照隔离，期间继续到件不影响本包）")
    post("/packages/start")
    # 打包期间到的 A4 不会进这个包
    upload("A", 4, 0, make_csv("A", 4))
    run = wait_run()
    print(f"  任务结果: id={run[0]} status={run[1]} pair={run[2]}")
    assert run[1] == "ready" and "A=3" in run[2] and "B=2" in run[2], run

    data, ctype = get(f"/packages/download/{run[0]}")
    zpath = os.path.join(OUT, f"downloaded_{run[0]}.zip")
    with open(zpath, "wb") as fh:
        fh.write(data)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        print("  ZIP 内容:", zf.namelist())
        print("  批号清单前 3 行:\n   ",
              zf.read("manifest.csv").decode("utf-8-sig").splitlines()[:3])

    # 4) 失败重试：故障注入环境变量只在容器/进程内有效，这里演示业务级失败——
    #    手动制造一个“缺 B1 无法出包”的新场景不可行（B1 已在库），
    #    因此演示重试入口对非 failed 任务的保护即可；真实失败重试见自测用例。
    print("\n[4] 重试保护（仅 failed 任务可重试）")
    page = post(f"/packages/retry/{run[0]}")
    p = _FlashParser(); p.feed(page)
    print("  对 ready 任务点重试:", p.flashes)

    print("\n✅ 试跑完成。下载包保存在", zpath)
    print("   打开网页查看双方纳入到批、缺口与任务列表：", BASE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
