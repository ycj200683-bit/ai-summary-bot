"""
装饰器对照教学示例（W6）
作者：俊 ｜ 直接运行：python learn_decorator.py

装饰器为什么难？因为它有四层嵌套，讲解听起来像绕口令。
但它的本质只有一句话：

    装饰器 = 一个"接收函数、返回新函数"的函数。
    它做的事 = 在不修改原函数代码的前提下，给原函数"套一层壳"。

下面分 5 步，每步都能跑，按顺序看。
"""

import time
import functools


# ============================================================
# 第 1 步：闭包 —— 装饰器的地基（看不懂装饰器，99% 是卡在这）
# ============================================================
print("=" * 60)
print("第 1 步：闭包 —— 函数里可以定义函数，还能把它返回出去")
print("=" * 60)


def outer(msg):
    """outer 接收一个变量 msg。"""

    def inner():
        # inner 里用到了外层的 msg —— 这叫"闭包"：内层函数记住了外层的变量
        print(f"  inner 说：{msg}")

    return inner        # 注意：返回的是函数本身（没加括号），不是调用结果


say_hi = outer("你好")     # 此时 outer 已经执行完了，但 msg 没丢
say_hi()                  # 还能打印出 "你好" —— 这就是闭包

# 关键理解：outer 跑完后，msg 本该被销毁，但因为 inner 还引用着它，
#          所以它被"关"在了 inner 这个函数里 —— closure（闭包）。


# ============================================================
# 第 2 步：最简装饰器（无参）—— 原来 @ 就干这个
# ============================================================
print()
print("=" * 60)
print("第 2 步：最简装饰器 —— 给函数套一层壳")
print("=" * 60)


def my_decorator(func):
    """装饰器：接收一个函数 func，返回一个新函数 wrapper。"""

    def wrapper():
        print("  【壳】调用前做的准备……")     # 原函数执行前加动作
        func()                                # 执行原函数
        print("  【壳】调用后做的收尾……")     # 原函数执行后加动作

    return wrapper


@my_decorator                 # 这一行等价于：say_hello = my_decorator(say_hello)
def say_hello():
    print("  【原函数】Hello！")


say_hello()
# 输出顺序：壳的准备 → 原函数 → 壳的收尾
# 这就是装饰器：原函数一个字的代码都没改，却多了前后两段行为。


# ============================================================
# 第 3 步：让壳"通用"起来 —— *args / **kwargs
# ============================================================
print()
print("=" * 60)
print("第 3 步：壳要能接住任意参数 —— *args / **kwargs")
print("=" * 60)


def log_calls(func):
    def wrapper(*args, **kwargs):
        # *args 接住所有位置参数，**kwargs 接住所有关键字参数
        print(f"  【日志】调用 {func.__name__}，参数 args={args} kwargs={kwargs}")
        return func(*args, **kwargs)      # 把参数原样转发给原函数
    return wrapper


@log_calls
def add(a, b):
    return a + b


@log_calls
def greet(name, title="同学"):
    return f"{title} {name}，你好"


print("  add(1, 2) =", add(1, 2))
print("  greet('俊') =", greet("俊"))
# 关键点：wrapper 必须 return func(...)，否则原函数的返回值会被丢掉！


# ============================================================
# 第 4 步：带参装饰器 —— @retry(max_tries=3) 这种"三层嵌套"
# ============================================================
print()
print("=" * 60)
print("第 4 步：带参装饰器 —— 多一层嵌套，只是为了「接收参数」")
print("=" * 60)


def repeat(times):
    """带参装饰器：最外层只负责收参数，返回真正的装饰器。"""
    def decorator(func):              # 这一层和之前的装饰器一样
        @functools.wraps(func)        # 保留原函数的名字/文档（见第 5 步）
        def wrapper(*args, **kwargs):
            results = []
            for i in range(times):
                print(f"  【重复】第 {i + 1} 次执行 {func.__name__}")
                results.append(func(*args, **kwargs))
            return results             # 执行 times 次，把结果收集成 list
        return wrapper
    return decorator


@repeat(times=3)
def ping():
    return "pong"


print("  ping() =", ping())
# 记忆口诀：不带参 = 两层（decorator / wrapper）
#          带参 = 三层（收参数 / decorator / wrapper）


# ============================================================
# 第 5 步：实战 @retry —— 这就是 W6 要写进 main.py 的东西
# ============================================================
print()
print("=" * 60)
print("第 5 步：实战 @retry(max_tries=3, backoff=0.5) 指数退避重试")
print("=" * 60)


def retry(max_tries=3, backoff=0.5):
    """
    失败自动重试的装饰器。

    参数：
      max_tries : 最多执行几次（含第一次）
      backoff   : 退避基数（秒）。第 1 次重试等 backoff，第 2 次等 backoff*2 ……

    只重试"可以重试"的异常（用 retry_on 指定异常类型），
    其它异常立刻抛出，不做无谓等待。
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            wait = backoff
            for attempt in range(1, max_tries + 1):
                try:
                    return func(*args, **kwargs)
                except TransientError as e:
                    # 还能再试吗？
                    if attempt == max_tries:
                        print(f"  【重试】第 {attempt} 次仍失败，放弃：{e}")
                        raise                                     # 次数用完了，抛出
                    print(f"  【重试】第 {attempt} 次失败（{e}），等 {wait:.1f} 秒后重试")
                    time.sleep(wait)
                    wait *= 2                                     # 指数退避：等待时间翻倍
        return wrapper
    return decorator


class TransientError(Exception):
    """模拟"网络抖动"这类可以重试的错误。"""


class PermanentError(Exception):
    """模拟"key 错了"这类不该重试的错误。"""


call_count = {"n": 0}          # 用一个字典当"计数器"（闭包里要改外部变量，用可变对象最省事）


@retry(max_tries=3, backoff=0.3)
def flaky_api():
    """模拟一个"前两次失败、第三次才成功"的接口。"""
    call_count["n"] += 1
    if call_count["n"] < 3:
        raise TransientError("网络抖动")
    return "✅ 第 3 次成功拿到数据"


print("  —— 测试一：抖动三次后成功 ——")
print("  结果：", flaky_api())


@retry(max_tries=3, backoff=0.3)
def broken_api():
    """模拟"key 失效"这类不该重试的错误。"""
    raise PermanentError("401 key 不对")


print()
print("  —— 测试二：不可重试的错误，应当立刻抛出、不做等待 ——")
try:
    broken_api()
except PermanentError as e:
    print(f"  结果：立刻抛出 {type(e).__name__}: {e}（没有浪费时间重试）")


# ============================================================
# 第 6 步：functools.wraps —— 顺手但重要的"卫生习惯"
# ============================================================
print()
print("=" * 60)
print("第 6 步：functools.wraps —— 别让壳把原函数的身份顶掉")
print("=" * 60)


def no_wraps(func):
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)
    return wrapper


def with_wraps(func):
    @functools.wraps(func)            # 就多这一行
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)
    return wrapper


@no_wraps
def func_a():
    """我是 func_a 的说明文档。"""


@with_wraps
def func_b():
    """我是 func_b 的说明文档。"""


print("  没加 wraps ：", func_a.__name__, "| 文档:", func_a.__doc__)
print("  加了 wraps ：", func_b.__name__, "| 文档:", func_b.__doc__)
# 没加 wraps 时，函数名和文档都被壳顶掉了（变成 wrapper / None）——
# 那些"自动生成文档"的工具就会抓瞎。所以每个标准装饰器都会写 @functools.wraps(func)。

print()
print("=" * 60)
print("看完就做这两个小练习：")
print("  1. 把 @retry 的 max_tries 改成 5，观察 flaky_api 还会不会重试")
print("  2. 写一个 @timer 装饰器，打印函数执行耗时（提示：time.time() 前后各取一次）")
print("=" * 60)
