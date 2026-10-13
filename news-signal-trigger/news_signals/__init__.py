"""新闻信号发现与外部 Agent 触发接口。"""
from .discovery import NewsSignalDiscovery
from .trigger import NewsSignalTrigger, DiscoveryRunner

__all__ = ['NewsSignalDiscovery', 'NewsSignalTrigger', 'DiscoveryRunner']
