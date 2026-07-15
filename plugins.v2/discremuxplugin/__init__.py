from datetime import datetime, timedelta
import json
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.core.config import settings
from app.chain.transfer import TransferChain
from app.core.event import eventmanager, Event
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import ChainEventType, EventType, MediaType

from .disc_remuxer import DiscRemuxer


class DiscRemuxPlugin(_PluginBase):
    plugin_name = "蓝光原盘重封装"
    plugin_desc = "基于整理历史或下载器拦截，将蓝光原盘重封装为 MKV。"
    plugin_icon = "https://raw.githubusercontent.com/the-bruz/MoviePilot-Plugins/main/icons/discremuxplugin.png"
    plugin_version = "2.0.1-alpha"

    plugin_author = "bruz"
    author_url = "https://github.com/the-bruz"

    plugin_config_prefix = "discremux_"
    plugin_order = 10
    auth_level = 1

    _DATA_KEY = "processed_histories"
    _history_enabled = False
    _intercept_enabled = False
    _message = "插件尚未初始化"
    _stop_event = threading.Event()
    _scheduler: Optional[BackgroundScheduler] = None
    _remuxer: Optional[DiscRemuxer] = None
    _remuxer_lock = threading.Lock()
    _remuxers = set()
    _intercept_lock = threading.Lock()
    _active_intercepts = set()

    def init_plugin(self, config: dict = None):
        """根据当前配置初始化插件。"""
        config = config or {}
        if "history_enabled" not in config and "enabled" in config:
            config["history_enabled"] = bool(config.get("enabled"))
            self.update_config(config)
        self._history_enabled = bool(config.get("history_enabled", config.get("enabled")))
        self._intercept_enabled = bool(config.get("intercept_enabled"))
        self._message = config.get("message") or "插件初始化完成，等待任务执行。"
        self._stop_event = threading.Event()
        self._active_intercepts = set()
        self._remuxers = set()
        self._remuxer = None

        if self._history_enabled and config.get("run_once"):
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info("蓝光原盘重封装服务启动，立即运行一次")
            self._scheduler.add_job(
                self.history_remux,
                "date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="蓝光原盘重封装",
            )
            self._scheduler.start()
            config["run_once"] = False
            self.update_config(config)

    def get_state(self) -> bool:
        """返回插件当前是否启用。"""
        return self._history_enabled or self._intercept_enabled

    def get_service(self) -> List[Dict[str, Any]]:
        """注册后台定时任务。"""
        if not self._history_enabled:
            return []

        cron_str = (self.get_config() or {}).get("cron_schedule") or "0 3 * * *"
        return [
            {
                "id": f"{self.__class__.__name__}.history_remux",
                "name": "定时重封装最近整理的蓝光原盘",
                "trigger": CronTrigger.from_crontab(cron_str),
                "func": self.history_remux,
                "kwargs": {},
            }
        ]

    def stop_service(self):
        """停止正在执行的重封装任务。"""
        self._stop_event.set()
        logger.info("收到停用信号，正在终止 FFmpeg 重封装任务...")
        if self._scheduler:
            try:
                self._scheduler.shutdown(wait=False)
                self._scheduler = None
            except Exception as e:
                logger.warning(f"关闭一次性调度器失败: {e}")
        with self._remuxer_lock:
            remuxers = list(self._remuxers)
        for remuxer in remuxers:
            try:
                remuxer.terminate()
            except Exception as e:
                logger.error(f"尝试终止 FFmpeg 进程时发生异常: {e}")

    def _register_remuxer(self, remuxer: DiscRemuxer) -> None:
        with self._remuxer_lock:
            self._remuxers.add(remuxer)
            self._remuxer = remuxer

    def _unregister_remuxer(self, remuxer: DiscRemuxer) -> None:
        with self._remuxer_lock:
            self._remuxers.discard(remuxer)
            self._remuxer = next(iter(self._remuxers), None)

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        json_path = Path(__file__).parent / "form_ui.json"
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                form_ui = json.load(f)
        except Exception as e:
            logger.error(f"加载表单配置失败: {json_path} | 错误详情: {e}")
            raise RuntimeError(f"插件 UI 配置加载失败: {e}") from e

        default_config = {
            "history_enabled": False,
            "run_once": False,
            "recent_days": 7,
            "min_mkv_size_gb": 5,
            "movies_only": True,
            "bdmv_action": "ignore",
            "delete_download_source": False,
            "refresh_media_server": True,
            "cron_schedule": "0 3 * * *",
            "intercept_enabled": False,
            "intercept_transfer_mkv": True,
        }
        return form_ui, default_config

    def get_page(self) -> List[dict]:
        """返回详情页 JSON。"""
        histories = self._get_processed_histories()[:20]
        headers = [
            {"title": "模式", "key": "mode", "sortable": True},
            {"title": "状态", "key": "status", "sortable": True},
            {"title": "标题", "key": "title", "sortable": True},
            {"title": "来源", "key": "source", "sortable": False},
            {"title": "输出", "key": "output", "sortable": False},
            {"title": "下载源", "key": "source_cleanup", "sortable": False},
            {"title": "后处理", "key": "post_action", "sortable": False},
            {"title": "时间", "key": "time", "sortable": True},
        ]
        items = [
            {
                "mode": self._format_history_mode(item),
                "status": self._format_history_status(item),
                "title": item.get("title") or "-",
                "source": self._format_history_source(item),
                "output": (item.get("remux") or {}).get("output") or item.get("output") or "-",
                "source_cleanup": self._format_source_cleanup(item),
                "post_action": self._format_history_post_action(item),
                "time": item.get("finished_at") or item.get("time") or "-",
            }
            for item in histories
        ]
        page = [
            {
                "component": "VRow",
                "props": {"style": {"overflow": "hidden"}},
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {"type": "info", "variant": "tonal", "text": self._message},
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "secondary",
                                    "variant": "tonal",
                                    "text": (
                                        f"插件数据目录：{self.get_data_path()}；如需重跑，可清空已处理历史。"
                                        "目标 MKV 已存在或旧 BDMV 有 .ignore 时仍会按配置跳过。插件不会修改 MP 整理记录状态。"
                                    ),
                                },
                            }
                        ],
                    },
                ],
            }
        ]
        if histories:
            page[0]["content"].append(
                {
                    "component": "VCol",
                    "props": {"cols": 12},
                    "content": [
                        {
                            "component": "VDataTableVirtual",
                            "props": {
                                "class": "text-sm",
                                "headers": headers,
                                "items": items,
                                "height": "30rem",
                                "density": "compact",
                                "fixed-header": True,
                                "hide-no-data": True,
                                "hover": True,
                            },
                        }
                    ],
                },
            )
        else:
            page[0]["content"].append(
                {
                    "component": "VCol",
                    "props": {"cols": 12},
                    "content": [
                        {
                            "component": "div",
                            "text": "暂无已处理历史记录。",
                            "props": {"class": "text-center"},
                        }
                    ],
                }
            )
        page[0]["content"].append(
            {
                "component": "VCol",
                "props": {"cols": 12},
                "content": [
                    {
                        "component": "VBtn",
                        "props": {
                            "color": "warning",
                            "variant": "tonal",
                        },
                        "content": [
                            {
                                "component": "span",
                                "text": "清空已处理历史",
                            }
                        ],
                        "events": {
                            "click": {
                                "api": "plugin/DiscRemuxPlugin/clear_processed",
                                "method": "post",
                            }
                        },
                    }
                ],
            }
        )
        return page

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/clear_processed",
                "endpoint": self.clear_processed_histories,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "清空已处理历史",
                "description": "清空插件记录的 processed history id，用于允许重新处理整理历史。",
            }
        ]

    def clear_processed_histories(self) -> schemas.Response:
        self.save_data(self._DATA_KEY, [])
        self._message = "已清空已处理历史，下次运行会重新评估整理记录。"
        logger.info(self._message)
        return schemas.Response(success=True, message=self._message)

    def _get_processed_histories(self) -> List[dict]:
        data = self.get_data(self._DATA_KEY)
        return data if isinstance(data, list) else []

    @staticmethod
    def _now_str() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _format_history_mode(item: dict) -> str:
        mode = item.get("mode")
        if mode == "intercept":
            return "截断整理"
        if mode == "post_transfer":
            return "整理后"
        return mode or "-"

    @staticmethod
    def _format_history_status(item: dict) -> str:
        status = item.get("status")
        return {
            "running": "运行中",
            "success": "成功",
            "failed": "失败",
            "skipped": "跳过",
        }.get(status, status or "-")

    @staticmethod
    def _format_history_source(item: dict) -> str:
        source = item.get("source") or {}
        if item.get("mode") == "intercept":
            return source.get("download_hash") or source.get("input") or "-"
        return str(source.get("transfer_history_id") or item.get("id") or "-")

    @staticmethod
    def _format_history_post_action(item: dict) -> str:
        post_action = item.get("post_action") or {}
        parts = []
        library_action = post_action.get("library_bdmv_action")
        if library_action and library_action != "none":
            parts.append(library_action)
        if post_action.get("triggered_transfer"):
            new_history_id = post_action.get("new_transfer_history_id")
            parts.append(f"整理MKV#{new_history_id}" if new_history_id else "整理MKV")
        return "；".join(parts) or "-"

    @staticmethod
    def _format_source_cleanup(item: dict) -> str:
        source_cleanup = (item.get("post_action") or {}).get("source_cleanup")
        return "已删除" if source_cleanup and source_cleanup != "none" else "保留"

    @staticmethod
    def _history_value(download_history, key: str, default=None):
        if isinstance(download_history, dict):
            return download_history.get(key, default)
        return getattr(download_history, key, default)

    @classmethod
    def _download_history_snapshot(cls, download_history) -> dict:
        keys = [
            "path",
            "type",
            "title",
            "year",
            "tmdbid",
            "doubanid",
            "downloader",
            "download_hash",
            "episode_group",
        ]
        return {key: cls._history_value(download_history, key) for key in keys}

    def _save_history_record(self, record: dict) -> None:
        histories = [
            item for item in self._get_processed_histories()
            if item.get("dedupe_key") != record.get("dedupe_key")
        ]
        histories.insert(0, record)
        self.save_data(self._DATA_KEY, histories[:200])

    def _update_history_record(self, dedupe_key: str, **updates) -> None:
        histories = self._get_processed_histories()
        for item in histories:
            if item.get("dedupe_key") != dedupe_key:
                continue
            for key, value in updates.items():
                if isinstance(value, dict) and isinstance(item.get(key), dict):
                    item[key].update(value)
                else:
                    item[key] = value
            self.save_data(self._DATA_KEY, histories[:200])
            return

    def _save_processed_history(
            self,
            history,
            output_file: Path,
            source_cleanup: str = "none",
            library_bdmv_action: str = "none",
    ) -> None:
        record = {
            "id": str(uuid.uuid4()),
            "dedupe_key": f"post_transfer:{history.id}",
            "mode": "post_transfer",
            "status": "success",
            "title": history.title or Path(str(history.dest or "")).name,
            "media_type": history.type,
            "tmdbid": history.tmdbid,
            "doubanid": history.doubanid,
            "source": {
                "transfer_history_id": history.id,
                "download_hash": history.download_hash,
                "downloader": history.downloader,
                "input": history.dest,
                "input_location": "library",
            },
            "remux": {
                "output": output_file.as_posix(),
            },
            "post_action": {
                "source_cleanup": source_cleanup,
                "transfer_history_cleanup": "none",
                "library_bdmv_action": library_bdmv_action,
                "triggered_transfer": False,
                "new_transfer_history_id": None,
            },
            "finished_at": self._now_str(),
            # 兼容旧详情页字段。
            "output": output_file.as_posix(),
            "time": self._now_str(),
        }
        self._save_history_record(record)

    def _is_processed(self, history_id: int) -> bool:
        return any(
            item.get("dedupe_key") == f"post_transfer:{history_id}"
            or str(item.get("id")) == str(history_id)
            for item in self._get_processed_histories()
        )

    @staticmethod
    def _is_valid_bdmv_dir(path: Optional[Path]) -> bool:
        if not path or not path.exists() or not path.is_dir():
            return False
        try:
            marker_files = {item.name.lower() for item in path.iterdir() if item.is_file()}
        except OSError:
            return False
        return "index.bdmv" in marker_files or "movieobject.bdmv" in marker_files

    @staticmethod
    def _resolve_movie_dir(dest: str) -> Path:
        dest_path = Path(dest)
        if dest_path.name.upper() == "BDMV":
            return dest_path.parent
        if dest_path.exists() and dest_path.is_file():
            return dest_path.parent
        if dest_path.suffix:
            return dest_path.parent
        return dest_path

    @classmethod
    def _resolve_old_bdmv_dir(cls, dest: str, movie_dir: Path) -> Optional[Path]:
        dest_path = Path(dest)
        parts = list(dest_path.parts)
        for index, part in enumerate(parts):
            if part.upper() == "BDMV":
                bdmv_dir = Path(*parts[: index + 1])
                return bdmv_dir if cls._is_valid_bdmv_dir(bdmv_dir) else None
        candidate = movie_dir / "BDMV"
        return candidate if cls._is_valid_bdmv_dir(candidate) else None

    @classmethod
    def _is_bdmv_history(cls, history) -> bool:
        if not history or not history.dest:
            return False
        movie_dir = cls._resolve_movie_dir(history.dest)
        old_bdmv_dir = cls._resolve_old_bdmv_dir(history.dest, movie_dir)
        return cls._is_valid_bdmv_dir(old_bdmv_dir)

    @staticmethod
    def _target_mkv_exists(output_file: Path, min_size_gb: float) -> bool:
        min_size = int(min_size_gb * 1024 * 1024 * 1024)
        return output_file.exists() and output_file.is_file() and output_file.stat().st_size > min_size

    @staticmethod
    def _has_ignore_file(old_bdmv_dir: Optional[Path]) -> bool:
        return bool(old_bdmv_dir and (old_bdmv_dir / ".ignore").exists())

    @staticmethod
    def _touch_ignore_file(old_bdmv_dir: Optional[Path]) -> None:
        if not old_bdmv_dir or not old_bdmv_dir.exists() or not old_bdmv_dir.is_dir():
            logger.warning(f"旧 BDMV 目录不存在，无法创建 .ignore: {old_bdmv_dir}")
            return
        (old_bdmv_dir / ".ignore").touch(exist_ok=True)

    @staticmethod
    def _delete_old_bdmv(movie_dir: Path, old_bdmv_dir: Optional[Path]) -> None:
        for target in [old_bdmv_dir, movie_dir / "CERTIFICATE"]:
            if target and target.exists() and target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
                logger.info(f"已删除旧媒体库原盘目录: {target}")

    @staticmethod
    def _media_type(history) -> Optional[MediaType]:
        if history.type == MediaType.MOVIE.value:
            return MediaType.MOVIE
        if history.type == MediaType.TV.value:
            return MediaType.TV
        return None

    def _cleanup_download_source(self, history, delete_source: bool) -> None:
        if delete_source and history.src_fileitem:
            src_fileitem = schemas.FileItem(**history.src_fileitem)
            self._delete_local_source_fileitem(src_fileitem)
            DownloadHistoryOper().delete_file_by_fullpath(Path(src_fileitem.path).as_posix())
            logger.info(f"已删除下载源: history_id={history.id}, src={history.src}")

    @staticmethod
    def _delete_local_source_fileitem(fileitem: schemas.FileItem) -> None:
        if fileitem.storage and fileitem.storage != "local":
            raise RuntimeError(f"仅支持静默删除本地下载源，不支持存储类型: {fileitem.storage}")

        source_path = Path(fileitem.path)
        if len(source_path.parts) <= 2:
            raise RuntimeError(f"拒绝删除根目录或一级目录: {source_path}")
        if not source_path.exists() and not source_path.is_symlink():
            logger.info(f"下载源已不存在，跳过删除: {source_path}")
            return

        if source_path.is_dir() and not source_path.is_symlink():
            shutil.rmtree(source_path)
            return
        source_path.unlink()

    def _refresh_media_server(self, history, output_file: Path) -> None:
        refresh_target = output_file.parent
        item = schemas.RefreshMediaItem(
            title=history.title,
            year=history.year,
            type=self._media_type(history),
            category=history.category,
            target_path=refresh_target,
        )
        services = MediaServerHelper().get_services()
        if not services:
            logger.info("未获取到媒体服务器实例，跳过媒体库刷新。")
            return

        for name, service in services.items():
            instance = service.instance
            if not instance:
                logger.warning(f"媒体服务器实例为空，跳过刷新: name={name}")
                continue
            if hasattr(instance, "is_inactive") and instance.is_inactive():
                logger.warning(f"媒体服务器未连接，跳过刷新: name={name}")
                continue

            try:
                if hasattr(instance, "refresh_library_by_items"):
                    result = instance.refresh_library_by_items([item])
                    logger.info(
                        f"已尝试刷新媒体服务器条目: name={name}, target_path={refresh_target}, "
                        f"output={output_file}, result={result}"
                    )
                elif hasattr(instance, "refresh_root_library"):
                    result = instance.refresh_root_library()
                    logger.info(
                        f"媒体服务器不支持按条目刷新，已尝试刷新根库: name={name}, "
                        f"target_path={refresh_target}, result={result}"
                    )
                else:
                    logger.warning(f"媒体服务器不支持刷新: name={name}")
            except Exception as e:
                logger.warning(
                    f"刷新媒体服务器失败: name={name}, target_path={refresh_target}, "
                    f"output={output_file}, error={e}"
                )

    def history_remux(self) -> bool:
        """从整理历史中查找 BDMV 记录并调度重封装。"""
        self._stop_event.clear()
        config = self.get_config() or {}
        recent_days = int(config.get("recent_days") or 7)
        min_mkv_size_gb = float(config.get("min_mkv_size_gb") or 5)
        movies_only = bool(config.get("movies_only", True))
        bdmv_action = config.get("bdmv_action") or "ignore"
        delete_download_source = bool(config.get("delete_download_source"))
        refresh_media_server = bool(config.get("refresh_media_server", True))

        since_time = (datetime.now() - timedelta(days=recent_days)).strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"开始查询最近 {recent_days} 天整理历史: since_time={since_time}")
        histories = TransferHistoryOper().list_by_date(since_time)
        candidates = [history for history in histories if self._is_bdmv_history(history)]
        logger.info(f"找到 BDMV 整理历史 {len(candidates)} 条。")

        remuxer = DiscRemuxer()
        self._register_remuxer(remuxer)
        try:
            remuxer.validate_environment()
        except Exception:
            self._unregister_remuxer(remuxer)
            raise

        processed_count = 0
        for history in candidates:
            if self._stop_event.is_set():
                logger.info("任务已被中止。")
                break

            history_id = history.id
            if movies_only and history.type != MediaType.MOVIE.value:
                logger.info(f"跳过非电影记录: history_id={history_id}, type={history.type}, dest={history.dest}")
                continue
            if self._is_processed(history_id):
                logger.info(f"跳过已记录处理的整理历史: history_id={history_id}")
                continue

            movie_dir = self._resolve_movie_dir(history.dest)
            old_bdmv_dir = self._resolve_old_bdmv_dir(history.dest, movie_dir)
            output_file = movie_dir / f"{movie_dir.name}.mkv"

            logger.info(
                "准备处理光盘源: "
                f"history_id={history_id}, src={history.src}, dest={history.dest}, "
                f"input={old_bdmv_dir.parent if old_bdmv_dir else None}, "
                f"output={output_file}, old_bdmv_action={bdmv_action}"
            )

            if self._target_mkv_exists(output_file, min_mkv_size_gb):
                logger.info(
                    f"目标 MKV 已存在且大于阈值，跳过: history_id={history_id}, output={output_file}, "
                    f"threshold={min_mkv_size_gb}GB"
                )
                continue
            if self._has_ignore_file(old_bdmv_dir):
                logger.info(f"旧 BDMV 已存在 .ignore，跳过: history_id={history_id}, old_bdmv={old_bdmv_dir}")
                continue
            if not self._is_valid_bdmv_dir(old_bdmv_dir):
                logger.warning(f"媒体库旧 BDMV 不存在，跳过: history_id={history_id}, old_bdmv={old_bdmv_dir}")
                continue
            media_source_root = old_bdmv_dir.parent

            try:
                remuxer.remux_to_mkv(
                    source_root_path=media_source_root.as_posix(),
                    output_file_path=output_file.as_posix(),
                )
                if self._stop_event.is_set():
                    raise InterruptedError("用户发送了停用信号。")

                if bdmv_action == "delete_bdmv":
                    self._delete_old_bdmv(movie_dir, old_bdmv_dir)
                else:
                    self._touch_ignore_file(old_bdmv_dir)
                    logger.info(f"已在旧 BDMV 内创建 .ignore: history_id={history_id}, old_bdmv={old_bdmv_dir}")

                source_cleanup = "none"
                if delete_download_source:
                    self._cleanup_download_source(history, delete_source=True)
                    source_cleanup = "delete_download_source"

                self._save_processed_history(
                    history,
                    output_file,
                    source_cleanup=source_cleanup,
                    library_bdmv_action=bdmv_action,
                )
                if refresh_media_server:
                    self._refresh_media_server(history, output_file)
                processed_count += 1
            except subprocess.CalledProcessError as e:
                logger.error(f"FFmpeg 重封装失败: history_id={history_id}, stderr={e.stderr}")
            except Exception as e:
                logger.error(f"处理整理历史失败: history_id={history_id}, error={e}", exc_info=True)

        self._message = f"最近一次执行完成：候选 {len(candidates)} 条，成功处理 {processed_count} 条。"
        self._unregister_remuxer(remuxer)
        logger.info(self._message)
        return True

    @eventmanager.register(ChainEventType.TransferIntercept)
    def intercept_transfer(self, event: Event):
        """拦截下载目录中的蓝光原盘整理，改由插件先重封装再整理 MKV。"""
        config = self.get_config() or {}
        if not bool(config.get("intercept_enabled")):
            return

        event_data = event.event_data
        if not event_data or getattr(event_data, "cancel", False):
            return

        fileitem = getattr(event_data, "fileitem", None)
        mediainfo = getattr(event_data, "mediainfo", None)
        if not fileitem or fileitem.type != "dir":
            return

        source_root = Path(fileitem.path)
        if not self._is_valid_bdmv_dir(source_root / "BDMV"):
            return

        download_history = DownloadHistoryOper().get_by_path(source_root.as_posix())
        if not download_history:
            logger.info(f"跳过非 MoviePilot 下载历史原盘: {source_root}")
            return
        if bool(config.get("movies_only", True)) and download_history.type != MediaType.MOVIE.value:
            logger.info(f"跳过非电影下载原盘: {source_root}, type={download_history.type}")
            return

        download_history_snapshot = self._download_history_snapshot(download_history)
        downloader = self._history_value(download_history_snapshot, "downloader") or ""
        download_hash = self._history_value(download_history_snapshot, "download_hash") or source_root.as_posix()
        dedupe_key = f"intercept:{downloader}:{download_hash}"
        with self._intercept_lock:
            if dedupe_key in self._active_intercepts:
                event_data.cancel = True
                event_data.source = self.plugin_name
                event_data.reason = "蓝光原盘重封装任务已在运行，跳过原整理"
                logger.info(
                    "下载器原盘整理已存在接管任务，取消重复整理: "
                    f"source={source_root}, downloader={downloader}, hash={download_hash}"
                )
                return
            self._active_intercepts.add(dedupe_key)

        output_file = source_root.parent / f"{source_root.name}.mkv"
        min_size_gb = float(config.get("min_mkv_size_gb") or 5)
        output_exists = self._target_mkv_exists(output_file, min_size_gb)
        logger.info(
            "接管下载器原盘整理: "
            f"source={source_root}, output={output_file}, downloader={downloader}, "
            f"hash={download_hash}, media={download_history.title} ({download_history.year or '-'}), "
            f"tmdbid={download_history.tmdbid}, output_exists={output_exists}"
        )
        record = self._build_intercept_record(
            dedupe_key=dedupe_key,
            source_root=source_root,
            output_file=output_file,
            download_history=download_history_snapshot,
            mediainfo=mediainfo,
            status="skipped" if output_exists else "running",
            remux_error="目标 MKV 已存在，跳过 FFmpeg 重封装" if output_exists else None,
        )
        self._save_history_record(record)

        event_data.cancel = True
        event_data.source = self.plugin_name
        event_data.reason = (
            "蓝光原盘整理已由插件接管：下载目录 MKV 已存在，跳过原盘整理"
            if output_exists
            else "蓝光原盘整理已由插件接管：先在下载目录重封装 MKV，再对 MKV 发起整理"
        )

        worker = threading.Thread(
            target=self._run_intercept_remux if not output_exists else self._run_existing_intercept_output,
            kwargs={
                "dedupe_key": dedupe_key,
                "source_root": source_root,
                "output_file": output_file,
                "download_history": download_history_snapshot,
                "config": config,
            },
            daemon=True,
        )
        worker.start()
        logger.info(f"已启动下载目录原盘重封装后台任务: source={source_root}, output={output_file}")

    def _build_intercept_record(
            self,
            dedupe_key: str,
            source_root: Path,
            output_file: Path,
            download_history,
            mediainfo,
            status: str = "running",
            remux_error: Optional[str] = None,
    ) -> dict:
        now = self._now_str()
        return {
            "id": str(uuid.uuid4()),
            "dedupe_key": dedupe_key,
            "mode": "intercept",
            "status": status,
            "title": getattr(mediainfo, "title_year", None) or self._history_value(download_history, "title") or source_root.name,
            "media_type": self._history_value(download_history, "type"),
            "tmdbid": self._history_value(download_history, "tmdbid"),
            "doubanid": self._history_value(download_history, "doubanid"),
            "source": {
                "transfer_history_id": None,
                "download_hash": self._history_value(download_history, "download_hash"),
                "downloader": self._history_value(download_history, "downloader"),
                "input": source_root.as_posix(),
                "input_location": "download",
            },
            "remux": {
                "output": output_file.as_posix(),
                "started_at": now,
                "finished_at": now if status == "skipped" else None,
                "duration_seconds": None,
                "error": remux_error,
            },
            "post_action": {
                "source_cleanup": "none",
                "transfer_history_cleanup": "none",
                "library_bdmv_action": "none",
                "triggered_transfer": False,
                "new_transfer_history_id": None,
            },
            "started_at": now,
            "finished_at": None,
        }

    def _run_existing_intercept_output(self, dedupe_key: str, source_root: Path, output_file: Path, download_history, config: dict) -> None:
        try:
            logger.info(f"下载目录 MKV 已存在，跳过 FFmpeg 重封装并进入后处理: output={output_file}")
            triggered_transfer, new_transfer_history_id = self._post_process_intercept_output(
                output_file=output_file,
                download_history=download_history,
                config=config,
            )
            self._update_history_record(
                dedupe_key,
                status="success",
                post_action={
                    "source_cleanup": self._cleanup_intercept_source(source_root, config),
                    "triggered_transfer": triggered_transfer,
                    "new_transfer_history_id": new_transfer_history_id,
                },
                finished_at=self._now_str(),
            )
            logger.info(
                "已存在 MKV 后处理完成: "
                f"output={output_file}, triggered_transfer={triggered_transfer}, "
                f"new_transfer_history_id={new_transfer_history_id}"
            )
        except Exception as e:
            self._update_history_record(
                dedupe_key,
                status="failed",
                remux={"error": str(e), "finished_at": self._now_str()},
                finished_at=self._now_str(),
            )
            logger.error(f"处理已存在拦截 MKV 失败: source={source_root}, output={output_file}, error={e}", exc_info=True)
        finally:
            with self._intercept_lock:
                self._active_intercepts.discard(dedupe_key)

    def _run_intercept_remux(self, dedupe_key: str, source_root: Path, output_file: Path, download_history, config: dict) -> None:
        started_at = time.time()
        try:
            logger.info(f"开始下载目录原盘重封装: source={source_root}, output={output_file}")
            remuxer = DiscRemuxer()
            self._register_remuxer(remuxer)
            remuxer.validate_environment()
            remuxer.remux_to_mkv(
                source_root_path=source_root.as_posix(),
                output_file_path=output_file.as_posix(),
            )
            finished_at = self._now_str()
            self._update_history_record(
                dedupe_key,
                remux={
                    "finished_at": finished_at,
                    "duration_seconds": int(time.time() - started_at),
                    "error": None,
                },
                finished_at=finished_at,
            )
            logger.info(
                "下载目录原盘重封装完成: "
                f"source={source_root}, output={output_file}, duration={int(time.time() - started_at)}s"
            )

            triggered_transfer, new_transfer_history_id = self._post_process_intercept_output(
                output_file=output_file,
                download_history=download_history,
                config=config,
            )

            self._update_history_record(
                dedupe_key,
                status="success",
                post_action={
                    "source_cleanup": self._cleanup_intercept_source(source_root, config),
                    "triggered_transfer": triggered_transfer,
                    "new_transfer_history_id": new_transfer_history_id,
                },
                finished_at=self._now_str(),
            )
            self._message = f"下载目录原盘重封装完成: {output_file}"
            logger.info(
                "下载器拦截重封装流程完成: "
                f"source={source_root}, output={output_file}, triggered_transfer={triggered_transfer}, "
                f"new_transfer_history_id={new_transfer_history_id}"
            )
        except subprocess.CalledProcessError as e:
            error = e.stderr or str(e)
            self._update_history_record(
                dedupe_key,
                status="failed",
                remux={"error": error, "finished_at": self._now_str()},
                finished_at=self._now_str(),
            )
            logger.error(f"拦截重封装失败: source={source_root}, error={error}")
        except Exception as e:
            self._update_history_record(
                dedupe_key,
                status="failed",
                remux={"error": str(e), "finished_at": self._now_str()},
                finished_at=self._now_str(),
            )
            logger.error(f"拦截重封装处理失败: source={source_root}, error={e}", exc_info=True)
        finally:
            with self._intercept_lock:
                self._active_intercepts.discard(dedupe_key)
            if "remuxer" in locals():
                self._unregister_remuxer(remuxer)

    def _post_process_intercept_output(self, output_file: Path, download_history, config: dict) -> Tuple[bool, Optional[int]]:
        if not bool(config.get("intercept_transfer_mkv", True)):
            logger.info(f"配置为不整理重封装 MKV，跳过后续整理: output={output_file}")
            return False, None

        logger.info(
            "开始整理重封装 MKV: "
            f"output={output_file}, downloader={self._history_value(download_history, 'downloader')}, "
            f"hash={self._history_value(download_history, 'download_hash')}, "
            f"tmdbid={self._history_value(download_history, 'tmdbid')}"
        )
        state, errmsg = self._transfer_remuxed_mkv(output_file, download_history)
        if not state:
            raise RuntimeError(f"重封装后整理失败: {errmsg}")
        transfer_history = TransferHistoryOper().get_by_src(output_file.as_posix(), storage="local")
        logger.info(
            "重封装 MKV 整理完成: "
            f"output={output_file}, transfer_history_id={transfer_history.id if transfer_history else None}"
        )
        if transfer_history and bool(config.get("refresh_media_server", True)):
            self._refresh_media_server(transfer_history, output_file)
        return True, transfer_history.id if transfer_history else None

    @staticmethod
    def _cleanup_intercept_source(source_root: Path, config: dict) -> str:
        if not bool(config.get("delete_download_source")):
            logger.info(f"配置为保留下载目录原盘: source={source_root}")
            return "none"
        shutil.rmtree(source_root, ignore_errors=True)
        logger.info(f"已删除下载目录原盘: {source_root}")
        return "delete_original_disc"

    @staticmethod
    def _media_type_from_download_history(download_history) -> Optional[MediaType]:
        try:
            return MediaType(DiscRemuxPlugin._history_value(download_history, "type"))
        except Exception:
            return None

    def _transfer_remuxed_mkv(self, output_file: Path, download_history) -> Tuple[bool, Any]:
        if not output_file.exists() or not output_file.is_file():
            return False, f"重封装 MKV 不存在: {output_file}"

        fileitem = schemas.FileItem(
            storage="local",
            path=output_file.as_posix(),
            type="file",
            name=output_file.name,
            basename=output_file.stem,
            extension=output_file.suffix.lstrip("."),
            size=output_file.stat().st_size,
        )
        return TransferChain().manual_transfer(
            fileitem=fileitem,
            tmdbid=self._history_value(download_history, "tmdbid"),
            doubanid=self._history_value(download_history, "doubanid"),
            mtype=self._media_type_from_download_history(download_history),
            episode_group=self._history_value(download_history, "episode_group"),
            background=False,
            downloader=self._history_value(download_history, "downloader"),
            download_hash=self._history_value(download_history, "download_hash"),
            sync_extra_files=False,
        )
