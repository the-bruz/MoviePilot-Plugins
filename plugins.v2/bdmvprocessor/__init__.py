from typing import Any, Dict, List, Tuple

from app.plugins import _PluginBase


class BDMVProcessorPlugin(_PluginBase):
    plugin_name = "蓝光原盘重封装"
    plugin_desc = "读取整理列表，找到含BDMV文件夹的目标并重封装为mkv格式。"
    plugin_icon = "bdmvprocessor_icon.png"
    plugin_version = "1.0.0"
    
    plugin_author = "bruz"
    author_url = "https://github.com/the-bruz"
    
    plugin_config_prefix = "smartbdremuxer_"
    plugin_order = 10
    auth_level = 1

    _enabled = False
    _message = "插件尚未初始化"

    def init_plugin(self, config: dict = None):
        """根据当前配置初始化插件。"""
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._message = config.get("message") or "Hello MoviePilot"

    def get_state(self) -> bool:
        """返回插件当前是否启用。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """没有远程命令时直接返回空列表。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """没有插件 API 时直接返回空列表。"""
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回配置页 JSON 和默认配置模型。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "message",
                                            "label": "展示文本",
                                        },
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        ], {
            "enabled": False,
            "message": "Hello MoviePilot",
        }

    def get_page(self) -> List[dict]:
        """返回详情页 JSON。"""
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": self._message,
                },
            }
        ]

    def stop_service(self):
        """没有后台任务时可以留空。"""
        pass