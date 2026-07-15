import subprocess
from pathlib import Path
from typing import Optional

from app.log import logger


class DiscRemuxer:
    """使用 FFmpeg Blu-ray 协议重封装蓝光原盘。"""

    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None

    def terminate(self, timeout: int = 10) -> None:
        process = self._process
        if not process or process.poll() is not None:
            return
        logger.info(f"正在终止 FFmpeg 重封装进程: pid={process.pid}")
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning(f"FFmpeg 进程未在 {timeout} 秒内退出，强制终止: pid={process.pid}")
            process.kill()
            process.wait(timeout=5)

    def validate_environment(self) -> None:
        """检查 FFmpeg 可执行文件及其 Blu-ray 协议支持。"""
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-protocols"],
                capture_output=True,
                text=True,
                check=True,
            )
        except FileNotFoundError as e:
            raise RuntimeError("未检测到 ffmpeg，请在 MoviePilot 容器中安装 FFmpeg。") from e
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"无法检查 FFmpeg 协议支持: {e.stderr}") from e

        if "bluray" not in result.stdout.split():
            raise RuntimeError("当前 FFmpeg 未启用 bluray 协议，请使用包含 libbluray 支持的 FFmpeg。")
        try:
            subprocess.run(["ffprobe", "-version"], capture_output=True, check=True)
        except FileNotFoundError as e:
            raise RuntimeError("未检测到 ffprobe，请安装与 FFmpeg 同版本的 ffprobe。") from e
        except subprocess.CalledProcessError as e:
            raise RuntimeError("ffprobe 不可用，请检查 FFmpeg 安装。") from e
        logger.info("环境检查通过，FFmpeg Blu-ray 协议可用。")

    def _run_process(self, cmd: list[str]) -> str:
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=0,
        )
        try:
            output, _ = self._process.communicate()
            if self._process.returncode != 0:
                stderr = "\n".join((output or "").splitlines()[-80:])
                raise subprocess.CalledProcessError(self._process.returncode, cmd, stderr=stderr)
            return output or ""
        finally:
            self._process = None

    @staticmethod
    def _playlist_ids(source_root: Path) -> list[str]:
        playlist_dir = source_root / "BDMV" / "PLAYLIST"
        playlist_ids = sorted(
            playlist_file.stem
            for playlist_file in playlist_dir.glob("*.mpls")
            if playlist_file.stem.isdigit()
        )
        if not playlist_ids:
            raise RuntimeError(f"未在原盘中找到播放列表: {playlist_dir}")
        return playlist_ids

    @staticmethod
    def _bluray_url(source_root: Path) -> str:
        return f"bluray:{source_root.as_posix()}"

    def _playlist_duration(self, source_root: Path, playlist_id: str) -> float:
        cmd = [
            "ffprobe", "-v", "error", "-playlist", playlist_id,
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            "-i", self._bluray_url(source_root),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            logger.debug(f"无法读取播放列表时长，跳过: playlist={playlist_id}, error={result.stderr.strip()}")
            return 0
        try:
            return float(result.stdout.strip())
        except ValueError:
            return 0

    def _get_longest_playlist(self, source_root: Path) -> str:
        durations = {
            playlist_id: self._playlist_duration(source_root, playlist_id)
            for playlist_id in self._playlist_ids(source_root)
        }
        playlist_id, duration = max(durations.items(), key=lambda item: item[1])
        if duration <= 0:
            raise RuntimeError("无法从原盘播放列表读取有效时长。")
        logger.info(f"自动识别主正片播放列表: {playlist_id}, duration={duration:.0f}s")
        return playlist_id

    def remux_to_mkv(self, source_root_path: str, output_file_path: str) -> Path:
        """提取最长播放列表，成功后将 partial 文件改名为最终 MKV。"""
        source_root = Path(source_root_path)
        output_file = Path(output_file_path)
        partial_file = output_file.with_suffix(".partial.mkv")

        output_file.parent.mkdir(parents=True, exist_ok=True)
        if partial_file.exists():
            partial_file.unlink()

        playlist_id = self._get_longest_playlist(source_root)
        cmd = [
            "ffmpeg", "-y", "-nostdin", "-playlist", playlist_id,
            "-i", self._bluray_url(source_root),
            "-map", "0", "-map_metadata", "0", "-map_chapters", "0", "-c", "copy",
            partial_file.as_posix(),
        ]
        logger.info(
            f"开始执行 FFmpeg Blu-ray 重封装: source={source_root}, "
            f"playlist={playlist_id}, output={output_file}"
        )
        self._run_process(cmd)
        partial_file.rename(output_file)
        logger.info(f"重封装完成: {output_file}")
        return output_file
