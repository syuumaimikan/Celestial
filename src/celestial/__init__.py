"""Celestial: 小型AI群と管理AIによる、使うほど進化するローカルAIオーケストレーター。"""

from .config import Config, load_config
from .manager import Celestial, Result

__all__ = ["Celestial", "Config", "Result", "load_config"]
__version__ = "0.2.0"
