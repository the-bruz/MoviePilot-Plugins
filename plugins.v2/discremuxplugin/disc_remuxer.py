import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from app.log import logger


class DiscRemuxer:
    """解析 Blu-ray 播放列表并使用 FFmpeg 重封装原始 M2TS。"""

    _MPLS_TIMEBASE = 45_000

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
        """检查 FFmpeg 可执行文件。"""
        try:
            subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        except FileNotFoundError as e:
            raise RuntimeError("未检测到 ffmpeg，请在 MoviePilot 容器中安装 FFmpeg。") from e
        except subprocess.CalledProcessError as e:
            raise RuntimeError("ffmpeg 不可用，请检查 FFmpeg 安装。") from e
        logger.info("环境检查通过，FFmpeg 可用。")

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
                stderr = "\n".join((output or "").splitlines()[-20:])
                raise subprocess.CalledProcessError(self._process.returncode, cmd, stderr=stderr)
            return output or ""
        finally:
            self._process = None

    @staticmethod
    def _read_uint16(data: bytes, offset: int) -> int:
        return struct.unpack_from(">H", data, offset)[0]

    @staticmethod
    def _read_uint32(data: bytes, offset: int) -> int:
        return struct.unpack_from(">I", data, offset)[0]

    def _parse_playlist(self, playlist_file: Path) -> list[tuple[str, float, float]]:
        """读取 MPLS PlayItem 的 M2TS 文件名与播放区间。"""
        data = playlist_file.read_bytes()
        if len(data) < 18 or data[:4] != b"MPLS":
            raise RuntimeError(f"无效的播放列表文件: {playlist_file}")

        playlist_offset = self._read_uint32(data, 8)
        if playlist_offset + 10 > len(data):
            raise RuntimeError(f"播放列表数据不完整: {playlist_file}")

        item_count = self._read_uint16(data, playlist_offset + 6)
        offset = playlist_offset + 10
        play_items = []
        for _ in range(item_count):
            if offset + 22 > len(data):
                raise RuntimeError(f"播放列表条目不完整: {playlist_file}")
            item_length = self._read_uint16(data, offset)
            item_end = offset + 2 + item_length
            if item_length < 20 or item_end > len(data):
                raise RuntimeError(f"播放列表条目长度异常: {playlist_file}")

            clip_id = data[offset + 2:offset + 7].decode("ascii", errors="strict")
            in_time = self._read_uint32(data, offset + 14) / self._MPLS_TIMEBASE
            out_time = self._read_uint32(data, offset + 18) / self._MPLS_TIMEBASE
            if not clip_id.isdigit() or out_time <= in_time:
                raise RuntimeError(f"播放列表条目无效: {playlist_file}")
            play_items.append((clip_id, in_time, out_time))
            offset = item_end

        if not play_items:
            raise RuntimeError(f"播放列表没有可用片段: {playlist_file}")
        return play_items

    def _playlist_entries(self, source_root: Path, playlist_id: str) -> list[tuple[Path, float, float]]:
        playlist_file = source_root / "BDMV" / "PLAYLIST" / f"{playlist_id}.mpls"
        stream_dir = source_root / "BDMV" / "STREAM"
        entries = []
        for clip_id, in_time, out_time in self._parse_playlist(playlist_file):
            stream_file = stream_dir / f"{clip_id}.m2ts"
            if not stream_file.is_file():
                raise RuntimeError(f"播放列表引用的 M2TS 不存在: {stream_file}")
            entries.append((stream_file, in_time, out_time))
        return entries

    def _get_longest_playlist(self, source_root: Path) -> tuple[str, list[tuple[Path, float, float]], float]:
        playlist_dir = source_root / "BDMV" / "PLAYLIST"
        candidates = []
        for playlist_file in playlist_dir.glob("*.mpls"):
            if not playlist_file.stem.isdigit():
                continue
            try:
                entries = self._playlist_entries(source_root, playlist_file.stem)
            except RuntimeError as e:
                logger.debug(f"无法读取播放列表，跳过: playlist={playlist_file.stem}, error={e}")
                continue
            duration = sum(out_time - in_time for _, in_time, out_time in entries)
            candidates.append((playlist_file.stem, entries, duration))
        if not candidates:
            raise RuntimeError(f"未在原盘中找到可用播放列表: {playlist_dir}")

        playlist_id, entries, duration = max(candidates, key=lambda item: item[2])
        logger.info(
            f"自动识别主正片播放列表: {playlist_id}, duration={duration:.0f}s, clips={len(entries)}"
        )
        return playlist_id, entries, duration

    @staticmethod
    def _escape_concat_path(path: Path) -> str:
        return path.as_posix().replace("'", "'\\''")

    def _create_concat_file(self, output_dir: Path, entries: list[tuple[Path, float, float]]) -> Path:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".discremux_",
            suffix=".ffconcat",
            dir=output_dir,
            delete=False,
        ) as concat_file:
            concat_file.write("ffconcat version 1.0\n")
            for stream_file, in_time, out_time in entries:
                concat_file.write(f"file '{self._escape_concat_path(stream_file)}'\n")
                concat_file.write(f"inpoint {in_time:.6f}\n")
                concat_file.write(f"outpoint {out_time:.6f}\n")
        return Path(concat_file.name)

    def remux_to_mkv(self, source_root_path: str, output_file_path: str) -> Path:
        """按最长播放列表拼接 M2TS，成功后将 partial 文件改名为最终 MKV。"""
        source_root = Path(source_root_path)
        output_file = Path(output_file_path)
        partial_file = output_file.with_suffix(".partial.mkv")

        output_file.parent.mkdir(parents=True, exist_ok=True)
        if partial_file.exists():
            partial_file.unlink()

        playlist_id, entries, _ = self._get_longest_playlist(source_root)
        concat_file = self._create_concat_file(output_file.parent, entries)
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
            "-f", "concat", "-safe", "0", "-i", concat_file.as_posix(),
            "-map", "0", "-map_metadata", "0", "-map_chapters", "0", "-c", "copy",
            partial_file.as_posix(),
        ]
        logger.info(
            f"开始执行 FFmpeg M2TS 重封装: source={source_root}, "
            f"playlist={playlist_id}, clips={len(entries)}, output={output_file}"
        )
        try:
            self._run_process(cmd)
        finally:
            concat_file.unlink(missing_ok=True)
        partial_file.rename(output_file)
        logger.info(f"重封装完成: {output_file}")
        return output_file
