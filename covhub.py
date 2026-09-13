#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""covhub 的命令行入口壳。

实现在 covhub/ 包里；这个文件只是让文档里所有 `python covhub.py xxx` 的用法
继续成立（同目录下包优先于同名模块，所以壳自己不会被当成 covhub 导入）。
"""

from covhub.cli import main

if __name__ == "__main__":
    main()
