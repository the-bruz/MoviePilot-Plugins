import os
import csv
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple
from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.log import logger
from app.helper.directory import DirectoryHelper
from .bdmv_processor import BDMVProcessor

class BDMVProcessorPlugin(_PluginBase):
    plugin_name = "蓝光原盘重封装"
    plugin_desc = "扫描媒体库，找到含 BDMV 文件夹的电影目录并重封装为 mkv 格式。依赖 makemkv 容器。"
    plugin_icon = "bdmvprocessor_icon.png"
    plugin_version = "1.0.0" # 升级个版本号纪念一下新架构
    
    plugin_author = "bruz"
    author_url = "https://github.com/the-bruz"
    
    plugin_config_prefix = "bdmvprocessor_"
    plugin_order = 10
    auth_level = 1

    _enabled = False
    _message = "插件尚未初始化"
    _stop_flag = False

    def init_plugin(self, config: dict = None):
        """根据当前配置初始化插件。"""
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._message = config.get("message") or "插件初始化完成，等待定时任务执行。"
        self._stop_flag = False

    def get_state(self) -> bool:
        """返回插件当前是否启用。"""
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        """注册后台定时任务。"""
        if not self.get_state():
            return []
            
        # 从配置中获取 cron 表达式，如果没有则回退到默认的每天凌晨 3 点
        cron_str = self.get_config().get('cron_schedule') or "0 3 * * *"
        
        return [
            {
                "id": f"{self.__class__.__name__}.remux",
                "name": "定时扫描并重封装蓝光原盘",
                "trigger": CronTrigger.from_crontab(cron_str),
                "func": self.remux,
                "kwargs": {},
            }
        ]
    
    def stop_service(self):
        """没有特定的常驻后台服务需要停止，留空。"""
        self._stop_flag = True
        logger.info("收到停用信号，正在终止所有的 BDMV 扫描与封装任务...")
        
        container_name = self.get_config().get("container_name", "makemkv")
        try:
            # -9 强制杀掉，这样正在执行的 subprocess.run 会因为远端断开而立刻报错退出
            subprocess.run(
                ["docker", "exec", container_name, "pkill", "-9", "makemkvcon"],
                capture_output=True, check=False
            )
            logger.info(f"已向容器 {container_name} 发送 makemkvcon 进程终结信号。")
        except Exception as e:
            logger.error(f"尝试终止 makemkvcon 进程时发生异常: {e}")
    
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
            current_dir = Path(__file__).parent
            json_path = current_dir / "form_ui.json"
            
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    form_ui = json.load(f)
            except Exception as e:
                logger.error(f"加载表单配置失败: {json_path} | 错误详情: {e}")
                raise RuntimeError(f"插件 UI 配置加载失败: {e}") from e
            
            default_config = {
                "container_name": "makemkv",
                "library_paths": "/volume1/media/movies",
                "delete_mode": "keep_all", 
                "cron_schedule": "0 3 * * *" 
            }
            
            return form_ui, default_config

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
    
    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """没有远程命令时直接返回空列表。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """没有插件 API 时直接返回空列表。"""
        return []

# ================= 辅助业务逻辑 =================

    def _is_already_remuxed(self, movie_dir: Path) -> bool:
        """启发式检查：目录下是否存在大于 10GB 的 mkv 文件"""
        # 10GB 字节大小
        THRESHOLD_BYTES = 10737418240 
        for mkv_file in movie_dir.glob("*.mkv"):
            if mkv_file.is_file() and mkv_file.stat().st_size > THRESHOLD_BYTES:
                return True
        return False

    def _cleanup_source(self, movie_dir: Path):
        """清理源文件：仅删除原盘文件夹结构 (BDMV / CERTIFICATE)"""
        for target in ["BDMV", "CERTIFICATE"]:
            target_path = movie_dir / target
            if target_path.exists() and target_path.is_dir():
                shutil.rmtree(target_path, ignore_errors=True)
                logger.info(f"已清理原盘源文件夹: {target_path}")


    # ================= 核心任务循环 =================

    def remux(self) -> bool:
        """扫描并调度 BDMVProcessor"""
        if self._stop_flag:
            return False
            
        logger.info("开始执行蓝光原盘扫描任务...")
        self._stop_flag = False
        
        config = self.get_config()
        container_name = config.get("container_name", "makemkv")
        delete_mode = config.get("delete_mode", "keep_all")
        library_paths_str = config.get("library_paths", "")
        
        library_dirs = []
        if not library_paths_str:
            logger.info("未配置媒体库根目录，扫描全部本地媒体库。")
            dir_confs = DirectoryHelper().get_local_library_dirs()
            for dir_conf in dir_confs:
                library_dirs.append(dir_conf.library_path)
        else:
            library_dirs = [Path(p.strip()) for p in library_paths_str.split(",") if p.strip()]
        
        for lib_dir in library_dirs:
            if self._stop_flag:
                logger.info("任务已被中止。")
                break
                
            if not lib_dir.exists() or not lib_dir.is_dir():
                logger.warning(f"目录不存在或无效，跳过: {lib_dir}")
                continue
                
            logger.info(f"正在扫描媒体库: {lib_dir}")
            
            for bdmv_dir in lib_dir.rglob("BDMV"):
                if self._stop_flag:
                    break
                    
                if not bdmv_dir.is_dir():
                    continue
                    
                movie_dir = bdmv_dir.parent
                
                # 1. 检查是否已被压制 (>10GB MKV 判定)
                if self._is_already_remuxed(movie_dir):
                    logger.debug(f"已存在 >10GB 的 MKV 文件，跳过: {movie_dir.name}")
                    continue
                    
                logger.info(f"发现待处理原盘: {movie_dir.name}")
                
                # 建立一个隔离的临时目录进行压制输出，防止污染源目录
                tmp_out_dir = movie_dir / "_remux_tmp"
                
                try:
                    # 2. 实例化 Processor 并执行
                    processor = BDMVProcessor(
                        bdmv_root_path=str(movie_dir), 
                        output_dir_path=str(tmp_out_dir), 
                        container_name=container_name
                    )
                    
                    # 默认提取最长正片
                    processor.remux_to_mkv(extract_all=False)
                    
                    if self._stop_flag:
                        raise InterruptedError("用户发送了停用信号。")

                    # 3. 成功后，将生成的 MKV 移动到外层
                    generated_mkvs = list(tmp_out_dir.glob("*.mkv"))
                    if not generated_mkvs:
                        logger.error(f"处理完成，但未能找到生成的 MKV 文件: {movie_dir.name}")
                        continue
                        
                    for mkv_file in generated_mkvs:
                        final_path = movie_dir / mkv_file.name
                        mkv_file.rename(final_path)
                        logger.info(f"重封装完成，最终文件已归位: {final_path.name}")
                    
                    # 4. 根据策略清理原盘
                    if delete_mode == "delete_bdmv":
                        self._cleanup_source(movie_dir)
                        
                except subprocess.CalledProcessError:
                    if self._stop_flag:
                        logger.warning(f"封装任务已被强制打断: {movie_dir.name}")
                    else:
                        logger.error(f"MakeMKV 处理 {movie_dir.name} 失败。")
                except Exception as e:
                    logger.error(f"处理 {movie_dir.name} 时发生严重错误: {e}")
                finally:
                    # 无论成功失败，只要用完了临时目录，都要把它删掉擦屁股
                    if tmp_out_dir.exists():
                        shutil.rmtree(tmp_out_dir, ignore_errors=True)
                    
        logger.info("蓝光原盘扫描任务完成！")
        return True