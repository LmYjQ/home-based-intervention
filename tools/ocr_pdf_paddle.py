"""用 PaddleOCR 云端 API 解析扫描版 PDF，按页存 Markdown。

    python tools/ocr_pdf_paddle.py 孤独症谱系障碍儿童干预康复指南2026.pdf --out data/ocr/guideline2026

要点：
- 末几页是横向表格（内容旋转 90°），必须开 useDocOrientationClassify，
  否则表格会被读成乱序的竖排文字。
- useChartRecognition 保持 False：指南里没有需要识图的图，反而会引入噪声。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
# **token 只从环境变量读**，不写死 —— 写死的话一提交就泄进版本库。
# 用法：``PADDLEOCR_TOKEN=xxx python tools/ocr_pdf_paddle.py <pdf> --out <dir>``
TOKEN = os.environ.get("PADDLEOCR_TOKEN", "")
MODEL = "PaddleOCR-VL-1.6"


def submit(file_path: str, orient: bool) -> str:
    headers = {"Authorization": f"bearer {TOKEN}"}
    optional_payload = {
        "useDocOrientationClassify": orient,
        "useDocUnwarping": False,
        "useChartRecognition": False,
    }
    data = {"model": MODEL, "optionalPayload": json.dumps(optional_payload)}
    with open(file_path, "rb") as f:
        resp = requests.post(JOB_URL, headers=headers, data=data, files={"file": f})
    resp.raise_for_status()
    return resp.json()["data"]["jobId"]


def wait(job_id: str) -> str:
    headers = {"Authorization": f"bearer {TOKEN}"}
    while True:
        resp = requests.get(f"{JOB_URL}/{job_id}", headers=headers)
        resp.raise_for_status()
        data = resp.json()["data"]
        state = data["state"]
        if state == "done":
            return data["resultUrl"]["jsonUrl"]
        if state == "failed":
            raise SystemExit(f"OCR job failed: {data.get('errorMsg')}")
        prog = data.get("extractProgress", {})
        print(
            f"  {state} {prog.get('extractedPages', 0)}/{prog.get('totalPages', '?')}",
            flush=True,
        )
        time.sleep(5)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-orient", action="store_true")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"submitting {args.pdf} ...", flush=True)
    job_id = submit(args.pdf, orient=not args.no_orient)
    print(f"job {job_id}", flush=True)
    jsonl_url = wait(job_id)

    lines = requests.get(jsonl_url).text.strip().split("\n")
    for line in lines:
        if not line.strip():
            continue
        result = json.loads(line)["result"]
        for res in result["layoutParsingResults"]:
            page_no = res.get("pageIndex", 0) + 1
            md = res["markdown"]["text"]
            (out / f"page_{page_no:03d}.md").write_text(md, encoding="utf-8")
            imgs = res["markdown"].get("images", {})
            for img_path, url in imgs.items():
                dest = out / img_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(requests.get(url).content)
            print(f"  page {page_no}: {len(md)} chars, {len(imgs)} images", flush=True)


if __name__ == "__main__":
    main()
