from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NewsNowCatalogItem:
    id: str
    name: str
    category: str
    feed_type: str  # realtime | hottest
    default_strategy: str  # extract_web | newsnow_digest
    language: str = "zh-CN"
    description: str = ""


NEWSNOW_CATALOG: tuple[NewsNowCatalogItem, ...] = (
    # 科技与开发
    NewsNowCatalogItem(
        "hackernews",
        "Hacker News",
        "科技",
        "hottest",
        "newsnow_digest",
        "en",
        "全球极客与创业技术热议",
    ),
    NewsNowCatalogItem(
        "github-trending-today",
        "GitHub Trending",
        "科技",
        "hottest",
        "newsnow_digest",
        "en",
        "今日 GitHub 热门开源项目",
    ),
    NewsNowCatalogItem(
        "v2ex-share",
        "V2EX 最新分享",
        "科技",
        "realtime",
        "extract_web",
        "zh-CN",
        "创意工作者技术社区分享",
    ),
    NewsNowCatalogItem(
        "aihot",
        "AI 热点",
        "科技",
        "hottest",
        "newsnow_digest",
        "zh-CN",
        "人工智能与大模型热门动态",
    ),
    NewsNowCatalogItem(
        "sspai", "少数派", "科技", "hottest", "extract_web", "zh-CN", "数字生活与效率深度文章"
    ),
    NewsNowCatalogItem(
        "ithome", "IT之家", "科技", "realtime", "extract_web", "zh-CN", "数码科技与行业新闻"
    ),
    NewsNowCatalogItem(
        "36kr-quick",
        "36氪快讯",
        "科技",
        "realtime",
        "newsnow_digest",
        "zh-CN",
        "创投、AI 与商业快讯",
    ),
    NewsNowCatalogItem(
        "36kr-renqi", "36氪人气榜", "科技", "hottest", "extract_web", "zh-CN", "36氪深度商业报道"
    ),
    NewsNowCatalogItem(
        "solidot", "Solidot 奇客", "科技", "realtime", "extract_web", "zh-CN", "开源与科学资讯"
    ),
    NewsNowCatalogItem(
        "juejin", "稀土掘金", "科技", "hottest", "extract_web", "zh-CN", "中文软件工程热文"
    ),
    NewsNowCatalogItem(
        "producthunt", "Product Hunt", "科技", "hottest", "newsnow_digest", "en", "每日新产品发布榜"
    ),
    NewsNowCatalogItem(
        "freebuf", "FreeBuf 安全", "科技", "hottest", "extract_web", "zh-CN", "网络安全行业动态"
    ),
    NewsNowCatalogItem(
        "coolapk", "酷安热榜", "科技", "hottest", "newsnow_digest", "zh-CN", "数码社区热议"
    ),
    NewsNowCatalogItem(
        "nowcoder", "牛客热榜", "科技", "hottest", "newsnow_digest", "zh-CN", "开发者求职与技术讨论"
    ),
    NewsNowCatalogItem(
        "chongbuluo-hot",
        "虫部落热门",
        "科技",
        "hottest",
        "newsnow_digest",
        "zh-CN",
        "搜索技巧与效率工具社区热议",
    ),
    # 财经与商业
    NewsNowCatalogItem(
        "wallstreetcn-quick",
        "华尔街见闻快讯",
        "财经",
        "realtime",
        "newsnow_digest",
        "zh-CN",
        "全球宏观与市场实时快讯",
    ),
    NewsNowCatalogItem(
        "wallstreetcn-news",
        "华尔街见闻最新",
        "财经",
        "realtime",
        "extract_web",
        "zh-CN",
        "全球金融市场深度文章",
    ),
    NewsNowCatalogItem(
        "wallstreetcn-hot",
        "华尔街见闻最热",
        "财经",
        "hottest",
        "extract_web",
        "zh-CN",
        "高热度财经焦点",
    ),
    NewsNowCatalogItem(
        "cls-telegraph",
        "财联社电报",
        "财经",
        "realtime",
        "newsnow_digest",
        "zh-CN",
        "A 股与宏观实时电报",
    ),
    NewsNowCatalogItem(
        "cls-depth", "财联社深度", "财经", "realtime", "extract_web", "zh-CN", "资本市场深度调研"
    ),
    NewsNowCatalogItem(
        "cls-hot", "财联社热门", "财经", "hottest", "extract_web", "zh-CN", "财联社高赞热门文章"
    ),
    NewsNowCatalogItem(
        "xueqiu-hotstock",
        "雪球热门股票",
        "财经",
        "hottest",
        "newsnow_digest",
        "zh-CN",
        "投资社区热门标的",
    ),
    NewsNowCatalogItem(
        "gelonghui",
        "格隆汇事件",
        "财经",
        "realtime",
        "newsnow_digest",
        "zh-CN",
        "港美 A 股实时资讯",
    ),
    NewsNowCatalogItem(
        "jin10", "金十数据", "财经", "realtime", "newsnow_digest", "zh-CN", "外汇黄金宏观快讯"
    ),
    NewsNowCatalogItem(
        "fastbull-express",
        "法布财经快讯",
        "财经",
        "realtime",
        "newsnow_digest",
        "zh-CN",
        "全球外汇与大宗商品快讯",
    ),
    NewsNowCatalogItem(
        "fastbull-news",
        "法布财经新闻",
        "财经",
        "realtime",
        "extract_web",
        "zh-CN",
        "全球财经要闻与深度报道",
    ),
    NewsNowCatalogItem(
        "mktnews-flash",
        "MKTNews 快讯",
        "财经",
        "realtime",
        "newsnow_digest",
        "zh-CN",
        "宏观市场快讯",
    ),
    # 国际与时政
    NewsNowCatalogItem(
        "zaobao", "联合早报", "国际", "realtime", "extract_web", "zh-CN", "新加坡华文视角国际时政"
    ),
    NewsNowCatalogItem(
        "cankaoxiaoxi", "参考消息", "国际", "realtime", "extract_web", "zh-CN", "国际外媒编译资讯"
    ),
    NewsNowCatalogItem(
        "sputniknewscn",
        "卫星通讯社",
        "国际",
        "realtime",
        "extract_web",
        "zh-CN",
        "俄语视角国际新闻",
    ),
    # 国内热点与综合
    NewsNowCatalogItem(
        "thepaper", "澎湃新闻热榜", "综合", "hottest", "extract_web", "zh-CN", "深度时政与社会调查"
    ),
    NewsNowCatalogItem(
        "zhihu", "知乎热榜", "综合", "hottest", "newsnow_digest", "zh-CN", "知乎全站实时热点议题"
    ),
    NewsNowCatalogItem(
        "weibo", "微博实时热搜", "热点", "hottest", "newsnow_digest", "zh-CN", "社交媒体实时热搜榜"
    ),
    NewsNowCatalogItem(
        "toutiao", "今日头条热榜", "热点", "hottest", "newsnow_digest", "zh-CN", "综合新闻头条榜单"
    ),
    NewsNowCatalogItem(
        "baidu", "百度热搜", "热点", "hottest", "newsnow_digest", "zh-CN", "中文搜索引擎热点"
    ),
    NewsNowCatalogItem(
        "ifeng", "凤凰网热点", "综合", "hottest", "extract_web", "zh-CN", "综合时事热点报道"
    ),
    NewsNowCatalogItem(
        "tencent-hot",
        "腾讯新闻综合榜",
        "综合",
        "hottest",
        "extract_web",
        "zh-CN",
        "腾讯新闻深度与热点",
    ),
    NewsNowCatalogItem(
        "douyin", "抖音热点", "热点", "hottest", "newsnow_digest", "zh-CN", "短视频平台热议话题"
    ),
    NewsNowCatalogItem(
        "bilibili-hot-search",
        "B站热搜",
        "热点",
        "hottest",
        "newsnow_digest",
        "zh-CN",
        "B站热门搜索",
    ),
    NewsNowCatalogItem(
        "bilibili-hot-video",
        "B站热门视频",
        "热点",
        "hottest",
        "newsnow_digest",
        "zh-CN",
        "B站高播放热门内容",
    ),
    NewsNowCatalogItem(
        "bilibili-ranking",
        "B站排行榜",
        "热点",
        "hottest",
        "newsnow_digest",
        "zh-CN",
        "B站全站高人气内容排行",
    ),
    NewsNowCatalogItem(
        "douban", "豆瓣热门", "文化", "hottest", "newsnow_digest", "zh-CN", "书影音与文化社区热榜"
    ),
    NewsNowCatalogItem(
        "hupu", "虎扑热帖", "社区", "hottest", "newsnow_digest", "zh-CN", "体育与步行街热议"
    ),
    NewsNowCatalogItem(
        "tieba", "百度贴吧热议", "社区", "hottest", "newsnow_digest", "zh-CN", "贴吧全站实时热议话题"
    ),
    NewsNowCatalogItem(
        "dongqiudi", "懂球帝头条", "体育", "realtime", "extract_web", "zh-CN", "足球与体育新闻"
    ),
    NewsNowCatalogItem(
        "steam", "Steam 热门", "游戏", "hottest", "newsnow_digest", "zh-CN", "PC 游戏在线趋势"
    ),
)

NEWSNOW_BY_ID: dict[str, NewsNowCatalogItem] = {item.id: item for item in NEWSNOW_CATALOG}

CURATED_RSS_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "name": "喷嚏图卦",
        "kind": "rss",
        "identifier": "https://www.dapenti.com/blog/rss2.asp?name=xilei",
        "category": "综合",
        "fetch_strategy": "fulltext_rss",
        "use_proxy": "direct",
        "ai_options": {"title_include_regex": "喷嚏图卦"},
        "description": "经典每日图文资讯聚合（全文 RSS，默认过滤仅保留喷嚏图卦）",
    },
    {
        "name": "今日最新－天下雜誌",
        "kind": "webpage_list",
        "identifier": "https://www.cw.com.tw/today",
        "category": "财经",
        "fetch_strategy": "byparr_browser",
        "use_proxy": "proxy",
        "ai_options": {"link_selector": "a[href*='/article/']"},
        "description": "通过 Byparr 列表页抓取天下杂志每日精选",
    },
    {
        "name": "少数派 Matrix",
        "kind": "rss",
        "identifier": "https://sspai.com/feed",
        "category": "科技",
        "fetch_strategy": "fulltext_rss",
        "use_proxy": "direct",
        "description": "高效工作与数字生活深度全文 RSS",
    },
    {
        "name": "阮一峰的网络日志",
        "kind": "rss",
        "identifier": "https://www.ruanyifeng.com/blog/atom.xml",
        "category": "科技",
        "fetch_strategy": "fulltext_rss",
        "use_proxy": "direct",
        "description": "科技爱好者周刊与开发随笔",
    },
    {
        "name": "The Economist (RSSHub)",
        "kind": "rsshub",
        "identifier": "rsshub://economist/latest",
        "category": "国际",
        "fetch_strategy": "extract_web",
        "use_proxy": "proxy",
        "description": "经济学人最新国际政治与商业深度报道",
    },
    {
        "name": "MIT Technology Review",
        "kind": "rss",
        "identifier": "https://www.technologyreview.com/feed/",
        "category": "科技",
        "fetch_strategy": "extract_web",
        "use_proxy": "proxy",
        "description": "麻省理工科技评论前沿科技深度报道",
    },
    {
        "name": "Simon Willison's Weblog",
        "kind": "rss",
        "identifier": "https://simonwillison.net/atom/everything/",
        "category": "科技",
        "fetch_strategy": "fulltext_rss",
        "use_proxy": "proxy",
        "description": "AI 工程与开源工具一线观察",
    },
)
