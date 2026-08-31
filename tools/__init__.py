"""工具 handler 实现包。

每个子模块对应 config/tools/*.yaml 中 handler.module 引用的模块。
当前为占位 stub —— 函数签名与 yaml 一致，调用时 raise NotImplementedError，
明确指示该工具尚未实现（避免 ImportError，符合 P5 待办）。
"""
