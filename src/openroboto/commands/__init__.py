"""一个子命令一个模块，模块名就是命令名。

每个模块只暴露两个东西：
- `add_parser(subparsers)` —— 注册参数，`set_defaults(handler=run)`；
- `run(args) -> int` —— 干活，返回退出码。
"""
