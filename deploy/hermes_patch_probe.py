"""验证这个版本的飞书补丁装得上 —— 部署后的存活判据之一。

**它验证的是「新版本的补丁装载路径是否还成立」，不是「跑着的那个进程此刻如何」。**
这正是 v0190 的失效模式：懒加载让 15 个补丁装到了一个「克隆类」上 ——
和运行时真正在用的那个类长得一样但不是同一个对象，服务照常 active、
日志照常干净，静默失效了 5 天。装载路径一断，这里立刻数出 0。

在独立的短命进程里跑，不碰正在服务的 gateway。

为什么判据只能是 `co_filename`：
    `functools.wraps` 会把被包装函数的 `__module__` 原样抄到包装函数上，
    于是装在 hermes_multitenancy 里的补丁，`__module__` 看起来仍是上游核心的。
    2026-08-01 实测：`__module__` 判出 0/13，`co_filename` 判出 13/13。

为什么必须从 `load_feishu_module()` 取类，不能自己 import：
    只有它返回的才是运行时那一个类；自己 import 可能拿到克隆类，白验一场。

输出（stdout 最后一行）：
    PATCHPROBE <patched>/<total>      正常
    PATCHPROBE-ERR <类型>: <消息>     探针自己跑不起来
"""

from __future__ import annotations

import importlib
import os
import pkgutil


def install_all_patches(package) -> int:
    """把各 feishu_* 模块里的 install_*_patch 都调一遍，返回成功调用数。

    这些安装器本身是幂等的；即便某个失败也继续，让最终计数说话 ——
    探针的价值在于「数出来多少」，不在于中途抛错。
    """
    called = 0
    for mod_info in pkgutil.iter_modules(package.__path__):
        if not mod_info.name.startswith("feishu_"):
            continue
        try:
            mod = importlib.import_module(f"{package.__name__}.{mod_info.name}")
        except Exception:  # noqa: BLE001 — 单个模块导入失败不该让整个探针失败
            continue
        for attr in dir(mod):
            if attr.startswith("install_") and attr.endswith("_patch"):
                fn = getattr(mod, attr, None)
                if not callable(fn):
                    continue
                try:
                    fn()
                    called += 1
                except Exception:  # noqa: BLE001
                    pass
    return called


def main() -> None:
    os.environ.setdefault(
        "HERMES_HOME", os.path.expanduser("~/.hermes/profiles/multitenancy_router")
    )

    import hermes_multitenancy
    from hermes_multitenancy import feishu_adapter_compat as fac

    install_all_patches(hermes_multitenancy)

    adapter = getattr(fac.load_feishu_module(), "FeishuAdapter")
    mt_dir = os.path.dirname(os.path.abspath(hermes_multitenancy.__file__))

    patched = total = 0
    for member in vars(adapter).values():
        fn = getattr(member, "__func__", member)
        code = getattr(fn, "__code__", None)
        if code is None:
            continue
        total += 1
        if os.path.abspath(code.co_filename).startswith(mt_dir):
            patched += 1

    print(f"PATCHPROBE {patched}/{total}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 — 探针失败要说清楚，不能静默
        print(f"PATCHPROBE-ERR {type(exc).__name__}: {exc}")
