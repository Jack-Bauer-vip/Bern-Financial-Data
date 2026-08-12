# -*- coding: utf-8 -*-
"""安装 Bern_Financial_Data 对外 SKILL.md 契约到 Claude Code 用户级技能目录。

复制 skills/bern-financial-data/SKILL.md → ~/.claude/skills/bern-financial-data/,
使任意 Claude Code 项目都能识别并激活该技能（查本机金融数据中台）。

幂等：目标已存在时询问是否覆盖（--force 直接覆盖）。--target 可注入自定义目录（测试用）。

用法：
    python scripts_gen/install_skill.py            # 装到 ~/.claude/skills
    python scripts_gen/install_skill.py --force    # 已存在时直接覆盖
    python scripts_gen/install_skill.py --target /tmp/x   # 装到指定目录
"""

import argparse
import shutil
import sys
from pathlib import Path

SKILL_NAME = "bern-financial-data"
SOURCE_MD = "SKILL.md"


def install(target: Path, force: bool = False) -> Path:
    """把 SKILL.md 复制到 {target}/{SKILL_NAME}/,返回目标文件路径。"""
    src_file = Path(__file__).resolve().parent.parent / "skills" / SKILL_NAME / SOURCE_MD
    if not src_file.exists():
        raise FileNotFoundError(f"未找到契约源文件: {src_file}")

    dest_dir = target / SKILL_NAME
    dest_file = dest_dir / SOURCE_MD
    dest_dir.mkdir(parents=True, exist_ok=True)

    if dest_file.exists():
        if dest_file.read_bytes() == src_file.read_bytes():
            print(f"已安装且内容一致，跳过: {dest_file}")
            return dest_file
        if not force:
            print(f"目标已存在但内容不同: {dest_file}")
            print("提示: 用 --force 覆盖为最新版本")
            return dest_file
        print(f"覆盖旧版本: {dest_file}")

    shutil.copy2(src_file, dest_file)
    print(f"已安装 → {dest_file}")
    return dest_file


def main() -> int:
    parser = argparse.ArgumentParser(description="安装 Bern_Financial_Data 对外 SKILL.md 契约")
    parser.add_argument("--target", type=Path, default=Path.home() / ".claude" / "skills",
                        help="目标技能根目录(默认 ~/.claude/skills)")
    parser.add_argument("--force", action="store_true", help="已存在时直接覆盖")
    args = parser.parse_args()

    dest = install(args.target, force=args.force)
    print()
    print("安装完成。验证：")
    print(f"   1. 确认文件存在: {dest}")
    print("   2. 新开一个 Claude Code 会话,输入「查询 CPI 最新同比/环比」,")
    print("      看是否自动激活 bern-financial-data 并走 /indicator/us.cpi")
    return 0


if __name__ == "__main__":
    sys.exit(main())
