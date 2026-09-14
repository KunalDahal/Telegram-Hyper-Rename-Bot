import asyncio
import json
import logging
import os
import tempfile
import time


logger = logging.getLogger(__name__)


class MediaProcessor:
    def __init__(self, ffmpeg_path: str = "ffmpeg", ffprobe_path: str = "ffprobe"):
        self.ffmpeg_path  = ffmpeg_path
        self.ffprobe_path = ffprobe_path


    async def process(
        self,
        input_path: str,
        output_path: str,
        metadata: dict | None = None,
    ) -> str:
        metadata = metadata or {}

        has_metadata = self._has_metadata(metadata)
        logger.info(
            "[Media] Processing %s (metadata=%s)",
            os.path.basename(input_path),
            has_metadata,
        )

        if not has_metadata:
            logger.info("[Media] No processing requested; using the downloaded file.")
            return input_path

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        cmd = await self._build_copy_command(input_path, output_path, metadata)
        await self._run(cmd, "Metadata stream copy")
        self._assert_output(output_path)
        return output_path

    async def split_for_size(
        self, input_path: str, output_dir: str, max_bytes: int
    ) -> list[str]:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        input_path = os.path.abspath(input_path)
        output_dir = os.path.abspath(output_dir)
        os.makedirs(output_dir, exist_ok=True)

        total_size = os.path.getsize(input_path)
        if total_size <= max_bytes:
            return [input_path]

        total_duration = await self._probe_duration(input_path)
        if total_duration <= 0:
            raise RuntimeError(
                f"FFmpeg could not determine a usable duration for {os.path.basename(input_path)}."
            )

        target_bytes = int(max_bytes * 0.97)

        work_root = tempfile.mkdtemp(prefix="size_split_", dir=output_dir)
        ext = os.path.splitext(input_path)[1] or ".mkv"

        parts: list[str] = []
        start = 0.0
        part_number = 0
        END_EPSILON = 0.5

        while True:
            part_number += 1
            out_path = os.path.join(work_root, f"part_{part_number:04d}{ext}")

            cmd = [self.ffmpeg_path, "-hide_banner", "-y"]
            if start > 0:
                cmd += ["-ss", f"{start:.3f}"]
            cmd += [
                "-i", input_path,
                "-map", "0",
                "-c", "copy",
                "-fs", str(target_bytes),
                "-avoid_negative_ts", "make_zero",
                out_path,
            ]
            await self._run(cmd, f"FFmpeg size split (part {part_number})")

            if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
                raise RuntimeError(
                    f"FFmpeg produced no output for part {part_number} starting at {start:.3f}s."
                )
            part_size = os.path.getsize(out_path)
            if part_size > max_bytes:
                raise RuntimeError(
                    f"FFmpeg produced an oversized part ({part_size} bytes > {max_bytes} bytes): {out_path}"
                )
            parts.append(out_path)

            part_duration = await self._probe_duration(out_path)
            if part_duration <= 0:
                raise RuntimeError(
                    f"Could not determine the duration of generated part {os.path.basename(out_path)}."
                )
            next_start = start + part_duration

            if part_size < target_bytes * 0.98 or next_start >= total_duration - END_EPSILON:
                break

            if next_start <= start + END_EPSILON:
                raise RuntimeError(
                    "A single keyframe interval exceeds max_bytes; cannot split "
                    f"without re-encoding ({os.path.basename(input_path)} at {start:.3f}s)."
                )
            start = next_start

        if len(parts) < 2:
            raise RuntimeError("Size split produced fewer than two parts.")

        return parts


    async def _build_copy_command(
        self, input_path: str, output_path: str, metadata: dict
    ) -> list[str]:
        streams = await self._streams_for_map_and_metadata(input_path, output_path, metadata)
        map_args, extra_codec_args = await self._safe_map_args(input_path, output_path, streams=streams)
        cmd = [self.ffmpeg_path, "-hide_banner", "-y", "-i", input_path]
        cmd += map_args
        self._append_metadata(cmd, metadata)
        cmd += await self._stream_metadata_args(input_path, output_path, metadata, streams=streams)
        cmd += ["-c", "copy"] + extra_codec_args + [output_path]
        return cmd


    async def _probe_duration(self, input_path: str) -> float:
        logger.info("[Media] Probing duration with %s.", self.ffprobe_path)
        cmd = [
            self.ffprobe_path,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            input_path,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            logger.error("[Media] ffprobe executable was not found: %s", self.ffprobe_path)
            raise RuntimeError("ffprobe is not installed or is not on PATH.") from exc
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("[Media] Duration probe failed: %s", stderr.decode(errors="ignore")[-500:].strip())
            return 0.0
        try:
            duration = float(stdout.decode().strip())
            logger.info("[Media] Duration: %.2fs.", duration)
            return duration
        except (ValueError, AttributeError):
            logger.warning("[Media] ffprobe did not return a usable duration.")
            return 0.0

    async def _streams_for_map_and_metadata(
        self, input_path: str, output_path: str, metadata: dict
    ) -> list[dict] | None:
        ext = os.path.splitext(output_path)[1].lower().lstrip(".")
        needs_map_probe = ext in ("mp4", "mov", "m4v")
        title_value = str(
            metadata.get("title_all") or metadata.get("movie_name") or ""
        ).strip()
        if not (needs_map_probe or title_value):
            return None
        return await self._probe_all_streams(input_path)

    async def _probe_all_streams(self, input_path: str) -> list[dict]:
        cmd = [
            self.ffprobe_path, "-v", "error",
            "-show_entries", "stream=index,codec_type,codec_name",
            "-of", "json", input_path,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.warning("[Media] Stream probe failed: %s", stderr.decode(errors="ignore")[-500:].strip())
                return []
            return json.loads(stdout.decode()).get("streams", [])
        except Exception:
            logger.warning("[Media] Stream probe errored.", exc_info=True)
            return []

    _MP4_TEXT_SUBTITLE_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text"}

    async def _safe_map_args(
        self, input_path: str, output_path: str, streams: list[dict] | None = None
    ) -> tuple[list[str], list[str]]:
        ext = os.path.splitext(output_path)[1].lower().lstrip(".")
        if ext not in ("mp4", "mov", "m4v"):
            return ["-map", "0"], []

        if streams is None:
            streams = await self._probe_all_streams(input_path)
        if not streams:
            return ["-map", "0"], []

        args: list[str] = []
        needs_mov_text = False
        dropped: list[str] = []
        for s in streams:
            idx = s.get("index")
            ctype = s.get("codec_type")
            cname = (s.get("codec_name") or "").lower()
            if not self._stream_kept_for_container(s, ext):
                dropped.append(f"#0:{idx} {ctype} ({cname})")
                continue
            if ctype == "subtitle" and cname != "mov_text":
                needs_mov_text = True
            args += ["-map", f"0:{idx}"]

        if dropped:
            logger.warning(
                "[Media] Dropping stream(s) incompatible with .%s output: %s",
                ext, ", ".join(dropped),
            )
        extra = ["-c:s", "mov_text"] if needs_mov_text else []
        return args, extra

    @classmethod
    def _stream_kept_for_container(cls, stream: dict, ext: str) -> bool:
        if ext not in ("mp4", "mov", "m4v"):
            return True
        ctype = stream.get("codec_type")
        cname = (stream.get("codec_name") or "").lower()
        if ctype == "attachment":
            return False
        if ctype == "subtitle" and cname not in cls._MP4_TEXT_SUBTITLE_CODECS:
            return False
        return True

    async def _stream_metadata_args(
        self,
        input_path: str,
        output_path: str,
        metadata: dict,
        streams: list[dict] | None = None,
    ) -> list[str]:
        title_value = str(
            metadata.get("title_all") or metadata.get("movie_name") or ""
        ).strip()
        if not title_value:
            return []

        if streams is None:
            streams = await self._probe_all_streams(input_path)
        if not streams:
            return []

        ext = os.path.splitext(output_path)[1].lower().lstrip(".")
        spec_for_type = {"video": "v", "audio": "a", "subtitle": "s"}
        counters = {"video": 0, "audio": 0, "subtitle": 0}
        args: list[str] = []
        for s in streams:
            ctype = s.get("codec_type")
            if ctype not in spec_for_type:
                continue
            if not self._stream_kept_for_container(s, ext):
                continue
            stream_idx = counters[ctype]
            counters[ctype] += 1
            args.extend([f"-metadata:s:{spec_for_type[ctype]}:{stream_idx}", f"title={title_value}"])
        return args

    async def _run(self, cmd: list[str], stage: str) -> None:
        executable = os.path.basename(str(cmd[0]))
        output = os.path.basename(str(cmd[-1]))
        logger.info("[Media] %s started (%s → %s).", stage, executable, output)
        started_at = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            logger.error("[Media] %s executable was not found: %s", executable, cmd[0])
            raise RuntimeError(f"{executable} is not installed or is not on PATH.") from exc
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            full_err = stderr.decode(errors="ignore").strip()
            code_desc = self._describe_returncode(proc.returncode)
            logger.error(
                "[Media] %s failed after %.1fs (%s). Full ffmpeg stderr:\n%s",
                stage, time.monotonic() - started_at, code_desc, full_err or "(empty)",
            )
            # A negative returncode means the process was killed by a signal
            # rather than exiting on its own - ffmpeg never gets a chance to
            # print anything useful in that case, so the summarized stderr
            # (often just the last progress line) is misleading on its own.
            # Surface the signal/OOM hint up front instead of hiding it.
            if proc.returncode < 0:
                raise Exception(
                    f"FFmpeg was killed ({code_desc}) after {time.monotonic() - started_at:.1f}s "
                    f"with no ffmpeg-reported error - this usually means the OS killed it "
                    f"(most commonly an out-of-memory kill). Last output: "
                    f"{self._summarize_ffmpeg_error(full_err)}"
                )
            raise Exception(f"FFmpeg error: {self._summarize_ffmpeg_error(full_err)}")
        logger.info("[Media] %s completed in %.1fs.", stage, time.monotonic() - started_at)

    @staticmethod
    def _describe_returncode(returncode: int) -> str:
        if returncode >= 0:
            return f"exit code {returncode}"
        signum = -returncode
        try:
            import signal
            name = signal.Signals(signum).name
        except (ValueError, ImportError):
            name = f"signal {signum}"
        if signum == 9:
            return f"killed by {name} (SIGKILL - almost always an OOM kill)"
        return f"killed by {name}"

    @staticmethod
    def _summarize_ffmpeg_error(full_stderr: str) -> str:
        if not full_stderr:
            return "unknown error"
        lines = [ln.strip() for ln in full_stderr.splitlines() if ln.strip()]
        if not lines:
            return "unknown error"
        markers = (
            "error", "invalid", "not supported", "could not", "unsupported",
            "no such", "failed", "unable to", "cannot",
        )
        for line in reversed(lines):
            if any(marker in line.lower() for marker in markers):
                return line[:300]
        return lines[-1][:300]

    @staticmethod
    def _assert_output(path: str) -> None:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            raise Exception("Media processing did not create a valid output file")


    @staticmethod
    def _has_metadata(metadata: dict) -> bool:
        return any(str(v).strip() for v in metadata.values())

    @staticmethod
    def _append_metadata(cmd: list[str], metadata: dict) -> None:
        mapping = {
            "movie_name": "title",
            "title_all":  "title",
            "artist":     "artist",
            "author":     "author",
            "encoder":    "encoder",
        }
        for key, ff_key in mapping.items():
            value = str(metadata.get(key) or "").strip()
            if value:
                cmd.extend(["-metadata", f"{ff_key}={value}"])


