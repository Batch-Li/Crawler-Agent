from .base import AdapterResult, RawItem, SourceAdapter
from .registry import SourceRegistry
from .south_weekend import SouthWeekendAdapter
from .toutiao import ToutiaoAdapter
from .weibo import WeiboAdapter
from .weibo_authorized import WeiboAuthorizedAdapter
from .xiaohongshu import XiaohongshuAdapter
from .xiaohongshu_authorized import XiaohongshuAuthorizedAdapter
from .wechat_authorized import WechatAuthorizedListAdapter

__all__ = [
    "AdapterResult", "RawItem", "SourceAdapter", "SourceRegistry",
    "SouthWeekendAdapter", "ToutiaoAdapter", "WeiboAdapter", "WeiboAuthorizedAdapter", "WechatAuthorizedListAdapter", "XiaohongshuAdapter", "XiaohongshuAuthorizedAdapter",
]
