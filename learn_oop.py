"""
【W5 教学示例】函数写法 vs 类写法 对照
==========================================
目的：让你看懂"OOP 重构"到底在干什么。
一句话：**类就是把"要反复用的数据"和"操作这些数据的函数"打包在一起。**

不用背，不用自己写——先跑一遍，再改两行试试。
这个示例不调真实 API（不花钱、不需要 key），直接 python learn_oop.py 就能跑。
"""

# ==========================================================
# 写法 A：函数版（你现在的 main.py 基本就是这样）
# ==========================================================

def summarize_with_function(text, api_key, model="deepseek-chat"):
    """用函数实现：每次调用都要把 api_key、model 当参数传进来。"""
    # 假装这里发了一个请求（真实代码里是 requests.post）
    print(f"  [函数内部] 用 key={api_key[:6]}... model={model} 去请求")
    return f"《{text[:15]}...》的摘要"


# ==========================================================
# 写法 B：类版（W5 要把 main.py 改成这样）
# ==========================================================

class Summarizer:
    """
    一个类 = 把"配置"和"动作"打包。

    - __init__  ：构造函数，创建对象时自动跑一次，用来"存东西"
    - self.xxx  ：存在对象里的东西，后面的方法都能直接用
    - 方法      ：其实就是写在类里面的函数，第一个参数固定是 self
    """

    def __init__(self, api_key, model="deepseek-chat",temperature=44):
        """创建对象时把配置存起来——只存一次。"""
        self.api_key = api_key      # 存起来，以后不用再传
        self.model = model
        self.temperature=temperature

    def summarize(self, text):
        """注意：这里不用再传 api_key，直接用 self.api_key。"""
        print(f"  [类的方法] 用 key={self.api_key[:6]}... model={self.model} 去请求 温度={self.temperature} ")
        return f"《{text[:15]}...》的摘要"
    def count_words(self, text):
        return len(text)



# ==========================================================
# 对比：用起来到底有什么区别？
# ==========================================================

if __name__ == "__main__":
    text = "人工智能正在改变软件开发的方式，过去程序员需要逐行编写代码……"

    print("=" * 50)
    print("【函数版】每次调用都要把 api_key 再传一遍")
    print("=" * 50)
    # 用两次，就得传两次 key——麻烦，而且容易传错
    print(summarize_with_function(text, "sk-abc123"))
    print(summarize_with_function(text, "sk-abc123"))  # 又传了一遍

    print()
    print("=" * 50)
    print("【类版】创建时传一次，之后反复用")
    print("=" * 50)
    # 创建对象（这时 __init__ 把 key 存进去了）
    bot = Summarizer(api_key="sk-abc123")
    # 之后调用方法，不用再传 key
    print(bot.summarize(text))
    print(bot.summarize(text))  # 干净多了

    print()
    print("=" * 50)
    print("【再进一步】类和函数真正的威力：可以造多个对象")
    print("=" * 50)
    # 同一个类，可以造出配置不同的多个"实例"
    bot_a = Summarizer(api_key="sk-aaa111", model="deepseek-chat")
    bot_b = Summarizer(api_key="sk-bbb222", model="deepseek-reasoner")
    print("bot_a:", bot_a.summarize(text))
    print("bot_b:", bot_b.summarize(text))
    print()
    print("→ 这就是 W5 要干的事：把 API 调用包成 LLMClient，")
    print("  把业务逻辑包成 Summarizer，把配置包成 Config。")
    print("  代码没变多，但以后改起来不会牵一发动全身。")

    print()
    print("=" * 50)
    print("✏️  动手试试（改完再跑一次）：")
    print("   1. 给 Summarizer 加一个参数 temperature，在 summarize 里打印出来")
    print("   2. 给类加一个新方法 count_words(self, text)，返回字数")
    print("   3. 想想：你的 main.py 里，哪些东西适合'存进类里'？")
    print("=" * 50)
    bot = Summarizer(api_key="sk-test123", model="deepseek-chat")
    print("字数：", bot.count_words("你好,我是俊"))

