"""把 marts 导出发布到 S3 数据湖，头寸数据在代码层被挡住。

边界（设计决策，不是省事）：`portfolio.local.yaml` 与一切头寸数字留在本地，
云层只承载行情/新闻/仓库产物。所以这里做两件事：

1. `POSITION_FILES` 里的文件**永不上传**（fact_positions 含 shares/avg_cost/
   market_value/unrealized_pnl）；
2. `dim_symbol.is_currently_held` 会暴露持有哪些标的，云端副本一律置 False。

桶名不进仓库：从 `QUANTAI_S3_BUCKET` 环境变量读，或回落到本机
`.aws-bucket-name.local`（已 gitignore）。

用法：
    python scripts/s3_publish.py            # 发布 data/exports -> s3://<bucket>/exports/
    python scripts/s3_publish.py --dry-run  # 只做暂存与体检，不上传
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
EXPORTS = ROOT / "data" / "exports"
BUCKET_FILE = ROOT / ".aws-bucket-name.local"
S3_PREFIX = "exports"

# 头寸数字，锁死不上云
POSITION_FILES = {"fact_positions.csv"}
# 暴露持有标的的列 -> 云端副本置 False
HELD_FLAG = ("dim_symbol.csv", "is_currently_held")


def resolve_bucket() -> str:
    bucket = os.environ.get("QUANTAI_S3_BUCKET")
    if bucket:
        return bucket.strip()
    if BUCKET_FILE.exists():
        return BUCKET_FILE.read_text(encoding="utf-8").strip()
    raise SystemExit(
        "找不到桶名：设 QUANTAI_S3_BUCKET 环境变量，或建 .aws-bucket-name.local"
    )


def stage(src: Path, dest: Path) -> list[str]:
    """把 src 下的 CSV 过一遍边界规则后放进 dest，返回实际暂存的文件名。"""
    import pandas as pd

    dest.mkdir(parents=True, exist_ok=True)
    staged: list[str] = []
    for f in sorted(src.glob("*.csv")):
        if f.name in POSITION_FILES:
            print(f"[skip ] {f.name} —— 头寸数据，留在本地")
            continue
        held_file, held_col = HELD_FLAG
        if f.name == held_file:
            df = pd.read_csv(f)
            if held_col in df.columns:
                df[held_col] = False
                print(f"[xform] {f.name} —— {held_col} 全部置 False")
            df.to_csv(dest / f.name, index=False)
        else:
            shutil.copy2(f, dest / f.name)
            print(f"[copy ] {f.name}")
        staged.append(f.name)
    return staged


def audit(dest: Path) -> None:
    """发布前体检：暂存目录里绝不能出现头寸文件或未清零的持有标志。"""
    import pandas as pd

    leaked = sorted(p.name for p in dest.glob("*.csv") if p.name in POSITION_FILES)
    if leaked:
        raise SystemExit(f"ABORT: 暂存目录含头寸文件 {leaked}")
    held_file, held_col = HELD_FLAG
    f = dest / held_file
    if f.exists():
        df = pd.read_csv(f)
        if held_col in df.columns and bool(df[held_col].any()):
            raise SystemExit(f"ABORT: {held_file}.{held_col} 仍有 True")
    print("[audit] 边界体检通过：无头寸文件，无持有标志")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="只暂存与体检，不上传")
    args = p.parse_args()

    if not EXPORTS.exists():
        raise SystemExit(f"没有 {EXPORTS}——先跑 scripts/warehouse.py --export")

    with tempfile.TemporaryDirectory(prefix="quantai-s3-") as tmp:
        dest = Path(tmp)
        staged = stage(EXPORTS, dest)
        audit(dest)
        size_kb = sum(f.stat().st_size for f in dest.glob("*.csv")) // 1024
        print(f"\n{len(staged)} 个文件 / {size_kb} KB 待发布")

        if args.dry_run:
            print("[dry-run] 未上传")
            return 0

        bucket = resolve_bucket()
        target = f"s3://{bucket}/{S3_PREFIX}/"
        print(f"[upload] -> {target}")
        rc = subprocess.run(
            ["aws", "s3", "sync", str(dest), target, "--delete", "--only-show-errors"],
            cwd=str(ROOT),
        ).returncode
        print("[done ] 发布完成" if rc == 0 else f"[fail ] aws s3 sync rc={rc}")
        return rc


if __name__ == "__main__":
    raise SystemExit(main())
