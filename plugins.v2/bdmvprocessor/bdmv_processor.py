import csv
from app.log import logger
import subprocess
from pathlib import Path
from typing import Dict, Optional

class BDMVProcessor:
    """蓝光 BDMV 原盘自动化重封装处理器。"""

    _TINFO_DURATION_INDEX: int = 8

    def __init__(self, bdmv_root_path: str, 
                 output_dir_path: Optional[str] = None, 
                 container_name: str = "makemkv") -> None:
        self.bdmv_root: str = bdmv_root_path
        self.movie_name: str = Path(self.bdmv_root).name
        
        if not output_dir_path:
            self.output_dir: str = f"{self.bdmv_root}_remuxed"
        else:
            self.output_dir: str = output_dir_path
            
        self.container_name: str = container_name
        self._validate_environment()

    def _validate_environment(self) -> None:
        try:
            subprocess.run(["docker", "--version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["docker", "inspect", self.container_name], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["docker", "exec", self.container_name, "makemkvcon", "--version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            raise RuntimeError(f"环境或容器检查失败，请确认 Docker 及容器状态。详细信息: {e}")

    def _extract_info(self) -> Dict[int, Dict[int, str]]:
        cmd = ["docker", "exec", self.container_name, "makemkvcon", "--robot", "info", f"file:{self.bdmv_root}"]
        logger.info("正在扫描原盘媒体信息，请稍候...")
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        
        titles: Dict[int, Dict[int, str]] = {}
        for line in result.stdout.splitlines():
            if line.startswith("TINFO:"):
                row = next(csv.reader([line[6:]]))
                titles.setdefault(int(row[0]), {})[int(row[1])] = row[3]
        return titles

    @staticmethod
    def parse_duration(duration_str: str) -> int:
        try:
            h, m, s = map(int, duration_str.split(":"))
            return h * 3600 + m * 60 + s
        except (ValueError, AttributeError):
            return 0

    def _get_longest_title(self, titles: Dict[int, Dict[int, str]]) -> str:
        if not titles:
            raise RuntimeError("未能在该原盘中找到任何可提取的 Title。")
        target_title, _ = max(
            titles.items(),
            key=lambda item: self.parse_duration(item[1].get(self._TINFO_DURATION_INDEX, "00:00:00"))
        )
        return str(target_title)

    def _prepare_output_directory(self) -> None:
        """清空输出目录中的历史 MKV 文件，为新任务腾出纯净空间"""
        out_path = Path(self.output_dir)
        
        if not out_path.exists():
            logger.info(f"输出目录不存在，正在创建: {self.output_dir}")
            out_path.mkdir(parents=True, exist_ok=True)
            return

        # 仅清理 MKV，防误删
        old_mkvs = list(out_path.glob("*.mkv"))
        if old_mkvs:
            logger.warning(f"发现输出目录中存在 {len(old_mkvs)} 个历史 MKV 文件，正在清空...")
            for f in old_mkvs:
                f.unlink()
                logger.debug(f"已删除旧文件: {f.name}")
            logger.info("清理完毕，输出目录已就绪。")

    def remux_to_mkv(self, extract_all: bool = False) -> None:
        logger.info(f"开始处理原盘: {self.bdmv_root}")
        
        try:
            # 1. 运行前确保目录纯净
            self._prepare_output_directory()

            # 2. 决定提取目标
            if not extract_all:
                titles = self._extract_info()
                target_title = self._get_longest_title(titles)
                logger.info(f"自动识别主正片 Title ID: {target_title}")
            else:
                target_title = "all"
                logger.info("配置为提取原盘中的全部 Title。")

            # 3. 封装
            cmd = [
                "docker", "exec", self.container_name,
                "makemkvcon", "mkv", f"file:{self.bdmv_root}", 
                target_title, self.output_dir
            ]
            logger.info("开始执行核心封装命令...")
            subprocess.run(cmd, 
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True, 
                check=True
            )

            # 4. 极简重命名逻辑（因为此前已清空，所有 mkv 均是刚生成的）
            logger.info("重封装完成，正在按序重命名...")
            mkv_files = sorted(Path(self.output_dir).glob("*.mkv"))
            
            for index, mkv_file in enumerate(mkv_files):
                new_file = mkv_file.with_name(f"{self.movie_name}_t{index:02d}.mkv")
                mkv_file.rename(new_file)
                logger.info(f"-> 成功生成: {new_file.name}")
            
            logger.info(f"全部任务圆满结束！输出目录: {self.output_dir}")
            
        except subprocess.CalledProcessError as e:
            logger.error("Docker/MakeMKV 执行失败:")
            logger.error(f"标准错误 (Stderr):\n{e.stderr}")
            raise e