"""专业名单批量导入（§4.1 / §15.4）。

用法：python scripts/init_majors.py majors.csv
CSV 格式：name,code（如 "计算机科学与技术,080901"），首行表头可选。
幂等：按 name 去重，已存在的专业跳过。

在容器内执行：docker compose -f deploy/docker-compose.yml exec app python scripts/init_majors.py majors.csv
"""

import argparse
import asyncio
import csv
import sys
from pathlib import Path

from sqlalchemy import select

from app.storage.db import SessionLocal
from app.storage.models import Major


def parse_csv(path: Path) -> list[dict]:
    """解析专业名单 CSV：name,code；自动跳过表头行，忽略空行与 # 注释。"""
    items = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if not row or not row[0].strip() or row[0].strip().startswith("#"):
                continue
            name = row[0].strip()
            if name.lower() == "name":  # 表头
                continue
            code = row[1].strip() if len(row) > 1 and row[1].strip() else None
            items.append({"name": name, "code": code})
    return items


async def import_majors(items: list[dict]) -> tuple[int, int]:
    """写入 majors 表，返回 (新增数, 跳过数)。"""
    async with SessionLocal() as db:
        existing = set((await db.execute(select(Major.name))).scalars().all())
        created = 0
        for item in items:
            if item["name"] in existing:
                continue
            db.add(Major(name=item["name"], code=item["code"]))
            existing.add(item["name"])
            created += 1
        await db.commit()
    return created, len(items) - created


def main() -> None:
    parser = argparse.ArgumentParser(description="专业名单批量导入（name,code CSV，幂等）")
    parser.add_argument("csv_path", help="CSV 文件路径，如 scripts/majors.example.csv")
    args = parser.parse_args()

    path = Path(args.csv_path)
    if not path.is_file():
        sys.exit(f"文件不存在: {path}")
    items = parse_csv(path)
    if not items:
        sys.exit("CSV 中没有有效行（格式：name,code）")
    created, skipped = asyncio.run(import_majors(items))
    print(f"导入完成：新增 {created} 个专业，跳过 {skipped} 个已存在/重复项")


if __name__ == "__main__":
    main()
