import asyncio
import json
import logging
import os
import re
import tempfile
import shutil
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
        watermark: dict | None = None,
    ) -> str:
        metadata  = metadata  or {}
        watermark = watermark or {}

        has_metadata = self._has_metadata(metadata)
        has_watermark = self._watermark_enabled(watermark)
        logger.info(
            "[Media] Processing %s (metadata=%s, watermark=%s)",
            os.path.basename(input_path),
            has_metadata,
            has_watermark,
        )

        if not has_metadata and not has_watermark:
            logger.info("[Media] No processing requested; using the downloaded file.")
            return input_path

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        if has_watermark:
            return await self._process_with_watermark(input_path, output_path, metadata, watermark)

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


    async def _process_with_watermark(
        self, input_path: str, output_path: str, metadata: dict, watermark: dict
    ) -> str:
        mode = str(watermark.get("timing_mode", "range"))
        logger.info("[Media] Watermark pipeline started (mode=%s).", mode)

        if mode == "full":
            logger.info("[Media] Encoding the full video with the watermark.")
            cmd = await self._build_full_watermark_command(input_path, output_path, metadata, watermark)
            await self._run(cmd, "Full watermark encode")

        elif mode == "range":
            start = self._clamp_int(watermark.get("start", 0), 0, 86400)
            end   = self._clamp_int(watermark.get("end",   0), 0, 86400)
            if end > start:
                logger.info("[Media] Encoding watermark range %.1fs to %.1fs.", start, end)
                await self._process_segment_watermark(
                    input_path, output_path, metadata, watermark,
                    segments=[(start, end)],
                )
            else:
                logger.warning("[Media] Invalid watermark range; encoding the full video instead.")
                cmd = await self._build_full_watermark_command(input_path, output_path, metadata, watermark)
                await self._run(cmd, "Fallback full watermark encode")

        elif mode == "random_duration":
            duration     = self._clamp_int(watermark.get("duration",     30), 1, 3600)
            repeat_count = self._clamp_int(watermark.get("repeat_count",  1), 1,   20)
            total        = await self._probe_duration(input_path)
            segments     = self._random_segments(total, duration, repeat_count)
            if segments:
                logger.info("[Media] Encoding %d random watermark window(s): %s", len(segments), segments)
                await self._process_segment_watermark(
                    input_path, output_path, metadata, watermark, segments=segments
                )
            else:
                logger.warning("[Media] No valid random watermark windows; encoding the full video instead.")
                cmd = await self._build_full_watermark_command(input_path, output_path, metadata, watermark)
                await self._run(cmd, "Fallback full watermark encode")
        else:
            logger.warning("[Media] Unknown watermark mode '%s'; encoding the full video.", mode)
            cmd = await self._build_full_watermark_command(input_path, output_path, metadata, watermark)
            await self._run(cmd, "Fallback full watermark encode")

        self._assert_output(output_path)
        logger.info("[Media] Watermark pipeline completed: %s", os.path.basename(output_path))
        return output_path


    async def _process_segment_watermark(
        self,
        input_path: str,
        output_path: str,
        metadata: dict,
        watermark: dict,
        segments: list[tuple[int, int]],
    ) -> None:
        tmp_dir = tempfile.mkdtemp(prefix="wm_")
        logger.info("[Media] Preparing %d watermark segment(s).", len(segments))
        try:
            parts: list[str] = []
            cursor = 0.0

            for idx, (seg_start, seg_end) in enumerate(segments):
                if seg_start > cursor:
                    gap_path = os.path.join(tmp_dir, f"gap_{idx}.mkv")
                    logger.info("[Media] Copying unchanged segment %.1fs to %.1fs.", cursor, seg_start)
                    await self._stream_copy_segment(input_path, gap_path, cursor, seg_start)
                    parts.append(gap_path)

                wm_path = os.path.join(tmp_dir, f"wm_{idx}.mkv")
                logger.info("[Media] Encoding watermark segment %d/%d (%.1fs to %.1fs).", idx + 1, len(segments), seg_start, seg_end)
                await self._encode_watermark_segment(
                    input_path, wm_path, watermark, seg_start, seg_end
                )
                parts.append(wm_path)
                cursor = seg_end

            total = await self._probe_duration(input_path)
            if cursor < total - 0.1:
                tail_path = os.path.join(tmp_dir, "tail.mkv")
                logger.info("[Media] Copying unchanged tail from %.1fs to %.1fs.", cursor, total)
                await self._stream_copy_segment(input_path, tail_path, cursor, None)
                parts.append(tail_path)

            if len(parts) == 1:
                logger.info("[Media] One processed segment; moving it to the output.")
                shutil.move(parts[0], output_path)
            else:
                logger.info("[Media] Concatenating %d processed segment(s).", len(parts))
                list_file = os.path.join(tmp_dir, "concat.txt")
                with open(list_file, "w") as f:
                    for p in parts:
                        f.write(f"file '{p}'\n")
                await self._concat_segments(list_file, output_path, metadata, input_path)

        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            logger.info("[Media] Watermark temporary files cleaned up.")


    async def _stream_copy_segment(
        self,
        input_path: str,
        output_path: str,
        start: float,
        end: float | None,
    ) -> None:
        cmd = [
            self.ffmpeg_path, "-hide_banner", "-y",
            "-ss", str(start),
            "-i", input_path,
        ]
        if end is not None:
            cmd += ["-t", str(end - start)]
        cmd += [
            "-map", "0",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            output_path,
        ]
        await self._run(cmd, f"Stream-copy segment {start:.1f}s to {end if end is not None else 'end'}")

    async def _encode_watermark_segment(
        self,
        input_path: str,
        output_path: str,
        watermark: dict,
        start: float,
        end: float,
    ) -> None:
        video_info = await self._probe_video_stream(input_path)
        codec_name = video_info.get("codec_name", "libx264")

        encoder = self._codec_to_encoder(codec_name)
        logger.info("[Media] Source video codec=%s; selected encoder=%s.", codec_name, encoder)

        drawtext = self._drawtext_filter(watermark, time_offset=start)
        cmd = [
            self.ffmpeg_path, "-hide_banner", "-y",
            "-ss", str(start),
            "-i", input_path,
            "-t",  str(end - start),
            "-map", "0",
            "-vf", drawtext,
            "-c:v", encoder,
        ]

        cmd += self._source_quality_flags(video_info, encoder)

        cmd += [
            "-c:a", "copy",
            "-c:s", "copy",
            "-c:d", "copy",   
            "-avoid_negative_ts", "make_zero",
            output_path,      
        ]
        await self._run(cmd, f"Watermark segment encode {start:.1f}s to {end:.1f}s")

    async def _concat_segments(
        self, list_file: str, output_path: str, metadata: dict, source_for_probe: str,
    ) -> None:
        streams = await self._streams_for_map_and_metadata(source_for_probe, output_path, metadata)
        map_args, extra_codec_args = await self._safe_map_args(source_for_probe, output_path, streams=streams)
        cmd = [
            self.ffmpeg_path, "-hide_banner", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", list_file,
        ]
        cmd += map_args
        self._append_metadata(cmd, metadata)
        cmd += await self._stream_metadata_args(source_for_probe, output_path, metadata, streams=streams)
        cmd += ["-c", "copy"] + extra_codec_args + ["-movflags", "+faststart", output_path]
        await self._run(cmd, "Concatenate watermark segments")

    async def _build_full_watermark_command(
        self, input_path: str, output_path: str, metadata: dict, watermark: dict
    ) -> list[str]:
        video_info = await self._probe_video_stream(input_path)
        codec_name = video_info.get("codec_name", "libx264")
        encoder    = self._codec_to_encoder(codec_name)
        logger.info("[Media] Source video codec=%s; selected encoder=%s.", codec_name, encoder)

        streams = await self._streams_for_map_and_metadata(input_path, output_path, metadata)
        map_args, extra_codec_args = await self._safe_map_args(input_path, output_path, streams=streams)
        cmd = [
            self.ffmpeg_path, "-hide_banner", "-y",
            "-i", input_path,
        ]
        cmd += map_args
        self._append_metadata(cmd, metadata)
        cmd += await self._stream_metadata_args(input_path, output_path, metadata, streams=streams)
        cmd += [
            "-vf", self._drawtext_filter(watermark),
            "-c:v", encoder,
        ]
        cmd += self._source_quality_flags(video_info, encoder)
        cmd += [
            "-c:a", "copy",
            "-c:s", "copy",
            "-c:d", "copy",
        ]
        cmd += extra_codec_args
        cmd += [
            "-movflags", "+faststart",
            output_path,
        ]
        return cmd

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


    @staticmethod
    def _codec_to_encoder(codec_name: str) -> str:
        _map = {
            "h264":       "libx264",
            "hevc":       "libx265",
            "vp8":        "libvpx",
            "vp9":        "libvpx-vp9",
            "av1":        "libaom-av1",
            "mpeg2video": "mpeg2video",
            "mpeg4":      "mpeg4",
            "mjpeg":      "mjpeg",
            "prores":     "prores_ks",
        }
        return _map.get(codec_name.lower(), "libx264")

    @staticmethod
    def _source_quality_flags(video_info: dict, encoder: str) -> list[str]:
        flags: list[str] = []

        bit_rate = video_info.get("bit_rate")
        if bit_rate:
            try:
                br = int(bit_rate)
                if br > 0:
                    flags += ["-b:v", str(br)]
                    return flags
            except (TypeError, ValueError):
                pass

        return flags


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

    async def _probe_video_stream(self, input_path: str) -> dict:
        cmd = [
            self.ffprobe_path,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries",
            "stream=codec_name,width,height,bit_rate,pix_fmt,r_frame_rate,profile,level",
            "-of", "json",
            input_path,
        ]
        logger.info("[Media] Probing video stream with %s.", self.ffprobe_path)
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
            logger.warning("[Media] Video-stream probe failed: %s", stderr.decode(errors="ignore")[-500:].strip())
            return {}
        try:
            data    = json.loads(stdout.decode())
            streams = data.get("streams", [])
            stream = streams[0] if streams else {}
            logger.info("[Media] Video stream found: codec=%s, resolution=%sx%s.", stream.get("codec_name", "unknown"), stream.get("width", "?"), stream.get("height", "?"))
            return stream
        except Exception:
            logger.warning("[Media] ffprobe returned invalid video-stream data.")
            return {}


    @staticmethod
    def _random_segments(
        total_duration: float,
        seg_duration: int,
        repeat_count: int,
    ) -> list[tuple[int, int]]:
        if total_duration <= seg_duration:
            return []

        usable     = total_duration - seg_duration
        slot_width = usable / repeat_count
        segments: list[tuple[int, int]] = []

        for i in range(repeat_count):
            start = int(slot_width * i + slot_width * 0.5)
            end   = start + seg_duration
            if end <= total_duration:
                segments.append((start, end))

        return segments


    def _drawtext_filter(self, watermark: dict, time_offset: float = 0.0) -> str:
        text      = self._escape_filter_value(str(watermark.get("text", "")))
        color     = self._safe_color(str(watermark.get("color", "white")))
        font_size = self._clamp_int(watermark.get("font_size", 24), 8, 96)
        padding   = self._clamp_int(watermark.get("padding", 7),    0, 30)
        x_expr, y_expr = self._position_expr(
            str(watermark.get("position", "bot_right")), padding
        )

        parts = [
            f"text='{text}'",
            f"fontcolor={color}",
            f"fontsize={font_size}",
            f"x={x_expr}",
            f"y={y_expr}",
        ]

        font_path = str(watermark.get("font_path") or "").strip()
        if not (font_path and os.path.isfile(font_path)):
            font_path = self._default_watermark_font_path()

        if font_path:
            parts.insert(0, f"fontfile='{self._escape_filter_value(font_path)}'")

        if time_offset == 0.0:
            enable_expr = self._enable_expr(watermark)
            if enable_expr:
                parts.append(f"enable='{enable_expr}'")

        return "drawtext=" + ":".join(parts)

    def _default_watermark_font_path(self) -> str:
        module_dir = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(module_dir, "templates", "default.ttf"),
            os.path.join(module_dir, "src", "templates", "default.ttf"),
            os.path.join(os.getcwd(), "src", "templates", "default.ttf"),
        ]

        for candidate in candidates:
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)

        logger.warning(
            "[Media] Default watermark font not found; using FFmpeg font fallback."
        )
        return ""


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
            logger.error(
                "[Media] %s failed after %.1fs. Full ffmpeg stderr:\n%s",
                stage, time.monotonic() - started_at, full_err or "(empty)",
            )
            raise Exception(f"FFmpeg error: {self._summarize_ffmpeg_error(full_err)}")
        logger.info("[Media] %s completed in %.1fs.", stage, time.monotonic() - started_at)

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
    def _watermark_enabled(watermark: dict) -> bool:
        return bool(
            watermark.get("enabled") and str(watermark.get("text", "")).strip()
        )

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

    @staticmethod
    def _position_expr(position: str, padding: int) -> tuple[str, str]:
        pad_x = f"(w*{padding}/100)"
        pad_y = f"(h*{padding}/100)"
        positions = {
            "top_left":  (pad_x,               pad_y),
            "top_mid":   ("(w-text_w)/2",       pad_y),
            "top_right": (f"w-text_w-{pad_x}",  pad_y),
            "mid_left":  (pad_x,                "(h-text_h)/2"),
            "mid_right": (f"w-text_w-{pad_x}",  "(h-text_h)/2"),
            "bot_left":  (pad_x,                f"h-text_h-{pad_y}"),
            "bot_right": (f"w-text_w-{pad_x}",  f"h-text_h-{pad_y}"),
        }
        return positions.get(position, positions["bot_right"])

    def _enable_expr(self, watermark: dict) -> str:
        mode = str(watermark.get("timing_mode", "range"))
        if mode == "full":
            return ""
        if mode == "random_duration":
            duration = self._clamp_int(watermark.get("duration", 30), 1, 3600)
            return f"lt(mod(t\\,{duration * 2})\\,{duration})"
        start = self._clamp_int(watermark.get("start", 0), 0, 86400)
        end   = self._clamp_int(watermark.get("end",   0), 0, 86400)
        if end > start:
            return f"between(t\\,{start}\\,{end})"
        return ""

    @staticmethod
    def _escape_filter_value(value: str) -> str:
        value = value.replace("\\", "\\\\").replace(":", "\\:")
        value = value.replace("[", "\\[").replace("]", "\\]")
        value = value.replace("'", "'\\''")
        return value

    @staticmethod
    def _safe_color(value: str) -> str:
        value = value.strip().lower()
        if re.fullmatch(r"[a-z]+|#[0-9a-fA-F]{6}", value):
            return value
        return "white"

    @staticmethod
    def _clamp_int(value, low: int, high: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = low
        return max(low, min(high, number))
