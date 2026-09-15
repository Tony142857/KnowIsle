"""专业名单批量导入（§4.1 / §15.4）。

用法：python scripts/init_majors.py majors.csv
CSV 格式：name,code（如 "计算机科学与技术,080901"）
在容器内执行：docker compose -f deploy/docker-compose.yml exec app python scripts/init_majors.py majors.csv
"""

import sys


def main() -> None:
    # TODO(v0.2): 读取 CSV → 批量写入 majors 表（幂等：按 name 去重）
    sys.exit("init_majors 尚未实现（v0.2 迭代交付），CSV 格式：name,code")


if __name__ == "__main__":
    main()
