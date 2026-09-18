"""脚本单元测试：专业名单 CSV 解析（scripts/init_majors.py）。"""

from pathlib import Path

from scripts.init_majors import parse_csv


def test_parse_csv_basic(tmp_path: Path):
    csv_file = tmp_path / "majors.csv"
    csv_file.write_text(
        "name,code\n"
        "计算机科学与技术,080901\n"
        "\n"
        "# 注释行\n"
        "软件工程,\n",
        encoding="utf-8",
    )
    items = parse_csv(csv_file)
    assert items == [
        {"name": "计算机科学与技术", "code": "080901"},
        {"name": "软件工程", "code": None},
    ]


def test_parse_csv_bom_and_whitespace(tmp_path: Path):
    csv_file = tmp_path / "majors.csv"
    csv_file.write_text("﻿ 人工智能 , 080717 \n", encoding="utf-8")
    assert parse_csv(csv_file) == [{"name": "人工智能", "code": "080717"}]
