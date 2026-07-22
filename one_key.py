"""
一键：邮箱窗口 GitHub 登录 → 选择临时域名并生成邮箱 → 注册 v0 → 总共尝试 10 次 → 保存成功的 API Key

用法:
  1. 打开【比特浏览器】客户端（本地 API 默认 54345）
  2. 打开本机代理软件，端口 7890（Clash / v2rayN 等）
  3. 运行后在邮箱窗口完成 GitHub 登录，程序会读取站点生成的真实邮箱
  4. 在本目录执行:

     python one_key.py

成功的密钥在:
  data/keys/all_keys.txt
  data/keys/*_时间戳.txt
"""
from __future__ import annotations

import sys

from src.main import main


if __name__ == "__main__":
    # 默认：bitbrowser + 总共尝试 10 次；可用 --count 覆盖
    argv = list(sys.argv[1:])
    if "--mode" not in argv:
        argv.extend(["--mode", "bitbrowser"])
    if "-n" not in argv and "--count" not in argv and "--attempts" not in argv:
        argv.extend(["-n", "10"])
    raise SystemExit(main(argv))
