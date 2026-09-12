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

    # ─────────────────────────────────────────────────────────────────────────
    # Public entry point
    # ─────────────────────────────────────────────────────────────────────────

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

        # metadata-only → plain stream copy, no re-encode needed
        cmd = await self._build_copy_command(input_path, output_path, metadata)
        await self._run(cmd, "Metadata stream copy")
        self._assert_output(output_path)
        return output_path

    async def split_for_size(
        self, input_path: str, output_dir: str, max_bytes: int
    ) -> list[str]:
        """Split media into ordered, independently playable FFmpeg parts.

        FFmpeg stream-copy splitting keeps the result as valid media files
        instead of raw byte fragments. Any generated segment still over the
        requested limit is recursively subdivided until every returned file is
        within the limit.
        """
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        input_path = os.path.abspath(input_path)
        output_dir = os.path.abspath(output_dir)
        os.makedirs(output_dir, exist_ok=True)

        if os.path.getsize(input_path) <= max_bytes:
            return [input_path]

        work_root = tempfile.mkdtemp(prefix="size_split_", dir=output_dir)
        serial = 0

        async def split_one(current_path: str) -> list[str]:
            nonlocal serial
            current_size = os.path.getsize(current_path)
            if current_size <= max_bytes:
                return [current_path]

            duration = await self._probe_duration(current_path)
            if duration <= 0:
                raise RuntimeError(
                    f"FFmpeg could not determine a usable duration for {os.path.basename(current_path)}."
                )

            estimated_count = max(2, int((current_size + max_bytes - 1) // max_bytes) + 1)
            segment_time = max(1.0, (duration / estimated_count) * 0.92)
            prefix = f"split_{serial:06d}_"
            serial += 1
            ext = os.path.splitext(current_path)[1] or ".mkv"
            out_pattern = os.path.join(work_root, f"{prefix}%04d{ext}")

            cmd = [
                self.ffmpeg_path, "-hide_banner", "-y",
                "-i", current_path,
                "-map", "0",
                "-c", "copy",
                "-f", "segment",
                "-segment_time", f"{segment_time:.6f}",
                "-break_non_keyframes", "1",
                "-reset_timestamps", "1",
                out_pattern,
            ]
            await self._run(
                cmd,
                f"FFmpeg size split ({os.path.basename(current_path)})",
            )

            generated = sorted(
                os.path.join(work_root, name)
                for name in os.listdir(work_root)
                if name.startswith(prefix) and os.path.isfile(os.path.join(work_root, name))
            )
            if len(generated) < 2:
                raise RuntimeError("FFmpeg size split produced fewer than two parts.")

            ordered: list[str] = []
            for part in generated:
                ordered.extend(await split_one(part))
            return ordered

        return await split_one(input_path)

    # ─────────────────────────────────────────────────────────────────────────
    # Watermark routing  (three modes)
    # ─────────────────────────────────────────────────────────────────────────

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
                # Invalid range → fall back to full encode
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

    # ─────────────────────────────────────────────────────────────────────────
    # Segment-split encode  (range + random modes)
    #
    # Strategy
    # --------
    # 1. Stream-copy the segments BEFORE each watermark window  →  no CPU cost
    # 2. Re-encode ONLY the watermark window with drawtext
    # 3. Stream-copy the segment AFTER the last window          →  no CPU cost
    # 4. Concat all pieces with the concat demuxer              →  no re-encode
    #
    # Intermediate segments are written as MKV so every stream type (video,
    # audio, subtitles, attachments) can be carried without remuxing errors.
    # The final concat step produces the target container (mp4/mkv/etc.).
    # ─────────────────────────────────────────────────────────────────────────

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
                # ── gap before this window (stream copy) ─────────────────────
                if seg_start > cursor:
                    gap_path = os.path.join(tmp_dir, f"gap_{idx}.mkv")
                    logger.info("[Media] Copying unchanged segment %.1fs to %.1fs.", cursor, seg_start)
                    await self._stream_copy_segment(input_path, gap_path, cursor, seg_start)
                    parts.append(gap_path)

                # ── watermark window (re-encode video only) ───────────────────
                wm_path = os.path.join(tmp_dir, f"wm_{idx}.mkv")
                logger.info("[Media] Encoding watermark segment %d/%d (%.1fs to %.1fs).", idx + 1, len(segments), seg_start, seg_end)
                await self._encode_watermark_segment(
                    input_path, wm_path, watermark, seg_start, seg_end
                )
                parts.append(wm_path)
                cursor = seg_end

            # ── tail after last window (stream copy) ─────────────────────────
            total = await self._probe_duration(input_path)
            if cursor < total - 0.1:
                tail_path = os.path.join(tmp_dir, "tail.mkv")
                logger.info("[Media] Copying unchanged tail from %.1fs to %.1fs.", cursor, total)
                await self._stream_copy_segment(input_path, tail_path, cursor, None)
                parts.append(tail_path)

            # ── concat all pieces ─────────────────────────────────────────────
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

    # ─────────────────────────────────────────────────────────────────────────
    # FFmpeg helpers
    # ─────────────────────────────────────────────────────────────────────────

    async def _stream_copy_segment(
        self,
        input_path: str,
        output_path: str,
        start: float,
        end: float | None,
    ) -> None:
        """Cut a segment from input using stream copy — zero re-encode cost."""
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
        """
        Re-encode ONLY the watermark window, preserving the source video codec
        and its native parameters.  All non-video streams are stream-copied so
        audio, subtitles, and attachments pass through untouched.

        The output is always MKV so every stream type is supported without
        remuxing errors (important for files with subtitle or attachment streams).
        """
        video_info = await self._probe_video_stream(input_path)
        codec_name = video_info.get("codec_name", "libx264")

        # Map codec name reported by ffprobe → encoder name for ffmpeg
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

        # Carry forward any quality/bitrate settings from the source stream
        # so we don't silently downgrade quality.
        cmd += self._source_quality_flags(video_info, encoder)

        cmd += [
            "-c:a", "copy",
            "-c:s", "copy",
            "-c:d", "copy",   # data streams (e.g. tmcd)
            "-avoid_negative_ts", "make_zero",
            output_path,      # always MKV — set by caller via .mkv extension
        ]
        await self._run(cmd, f"Watermark segment encode {start:.1f}s to {end:.1f}s")

    async def _concat_segments(
        self, list_file: str, output_path: str, metadata: dict, source_for_probe: str,
    ) -> None:
        """
        Concat-demuxer join — no re-encode, just muxes the pieces together.
        Metadata is applied here so it only needs one pass.
        """
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
        """
        Full-file watermark encode.  Probes the source codec so we re-encode
        with the same encoder and preserve quality instead of defaulting to
        hardcoded libx264 / crf 23.
        """
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
        # One ffprobe stream-listing call, shared by both helpers below,
        # instead of each of them separately re-probing the same file.
        streams = await self._streams_for_map_and_metadata(input_path, output_path, metadata)
        map_args, extra_codec_args = await self._safe_map_args(input_path, output_path, streams=streams)
        cmd = [self.ffmpeg_path, "-hide_banner", "-y", "-i", input_path]
        cmd += map_args
        self._append_metadata(cmd, metadata)
        cmd += await self._stream_metadata_args(input_path, output_path, metadata, streams=streams)
        cmd += ["-c", "copy"] + extra_codec_args + [output_path]
        return cmd

    # ─────────────────────────────────────────────────────────────────────────
    # Codec / quality helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _codec_to_encoder(codec_name: str) -> str:
        """
        Map ffprobe codec_name → ffmpeg encoder name.
        Falls back to libx264 for anything unrecognised.
        """
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
        """
        Return encoder flags that preserve source quality as closely as
        possible without hardcoding any values.

        Priority order:
          1. If the source has a bit_rate, target that bitrate (-b:v).
          2. Otherwise leave quality to the encoder's default (no flags).

        We deliberately do NOT forward CRF from the source because CRF is an
        encode-time setting not stored in the stream and is not recoverable via
        ffprobe.  Using the source bitrate as a target is the safest proxy.
        """
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

        # No bitrate available — use encoder defaults.
        # For libx264/libx265 the default CRF (23/28) is reasonable; for
        # others the encoder picks its own default.  This is intentional:
        # we never silently downgrade the source quality with a hardcoded CRF.
        return flags

    # ─────────────────────────────────────────────────────────────────────────
    # ffprobe helpers
    # ─────────────────────────────────────────────────────────────────────────

    async def _probe_duration(self, input_path: str) -> float:
        """Return video duration in seconds via ffprobe."""
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
        """Probe streams once, only if `_safe_map_args` and/or
        `_stream_metadata_args` will actually need them for this
        input/output/metadata combination -- mirrors each function's own
        "do I need to probe" check so we neither probe twice (the previous
        bug) nor probe when neither one would have needed to at all (e.g. an
        mkv output with no "Title All" set).
        """
        ext = os.path.splitext(output_path)[1].lower().lstrip(".")
        needs_map_probe = ext in ("mp4", "mov", "m4v")
        title_value = str(
            metadata.get("title_all") or metadata.get("movie_name") or ""
        ).strip()
        if not (needs_map_probe or title_value):
            return None
        return await self._probe_all_streams(input_path)

    async def _probe_all_streams(self, input_path: str) -> list[dict]:
        """Return codec_type/codec_name/index for every stream in the file."""
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

    # MP4/MOV cannot hold attachment streams (fonts etc.) or picture-based
    # subtitle codecs (PGS/DVD/VOBSUB); text subtitles can be transcoded to
    # mov_text. Blindly "-map 0 -c copy"-ing everything into an mp4 output
    # when the source is e.g. an MKV with ASS subtitles/attachments fails
    # ffmpeg's muxer with a "codec not currently supported in container"
    # error — this is why watermark/rename jobs failed for some files (the
    # ones with subtitle/attachment streams) and not others.
    _MP4_TEXT_SUBTITLE_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text"}

    async def _safe_map_args(
        self, input_path: str, output_path: str, streams: list[dict] | None = None
    ) -> tuple[list[str], list[str]]:
        """Build -map args (and any needed trailing codec override) that are
        safe for output_path's container.

        Returns (map_args, extra_codec_args). extra_codec_args (if any) MUST
        be appended AFTER any "-c copy"/"-c:v ..." block, since ffmpeg lets
        later options override earlier ones for the same stream type.
        For non-mp4-like containers (mkv, etc.) this is a no-op passthrough
        of ("-map", "0") since they support everything.

        Pass `streams` (from a prior `_probe_all_streams` call) when the
        caller already has it, so we don't re-run ffprobe on the same file
        for no reason -- that probe alone can take real time on large files.
        """
        ext = os.path.splitext(output_path)[1].lower().lstrip(".")
        if ext not in ("mp4", "mov", "m4v"):
            return ["-map", "0"], []

        if streams is None:
            streams = await self._probe_all_streams(input_path)
        if not streams:
            # Probe failed; keep prior behavior rather than guessing.
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
        """Whether `stream` (one ffprobe stream dict) survives into an
        output whose extension is `ext`. Mirrors the exact filtering rule
        used in `_safe_map_args` so stream-type-relative indices computed
        elsewhere (e.g. for per-stream metadata) line up with what actually
        gets muxed into the output.
        """
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
        """Tag EVERY video/audio/subtitle stream that survives into the
        output with the "Title All" value -- not just the first stream of
        each type.

        "Title All" is documented (see settings.py) as applying "to
        general, video, audio, and subtitle metadata", but a plain
        `-metadata title=...` only sets the container-level title; players
        showing per-track names (e.g. multiple audio languages or several
        subtitle tracks) read each stream's OWN `title` tag, which stayed
        untouched. This adds the missing `-metadata:s:TYPE:N title=...` for
        every video, audio, and subtitle stream (N is the type-relative
        output index, e.g. every audio track and every subtitle track in
        turn), instead of only ever landing on the first one.

        Pass `streams` (from a prior `_probe_all_streams` call) when the
        caller already has it, so we don't re-run ffprobe on the same file
        for no reason -- that probe alone can take real time on large files.
        """
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
        """
        Return a dict of the first video stream's properties via ffprobe.
        Keys include: codec_name, width, height, bit_rate, pix_fmt, etc.
        Returns an empty dict on any failure so callers can use .get() safely.
        """
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

    # ─────────────────────────────────────────────────────────────────────────
    # Random segment generator
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _random_segments(
        total_duration: float,
        seg_duration: int,
        repeat_count: int,
    ) -> list[tuple[int, int]]:
        """
        Generate `repeat_count` non-overlapping windows of `seg_duration`
        seconds, deterministically spread across the video.
        """
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

    # ─────────────────────────────────────────────────────────────────────────
    # drawtext filter builder
    # ─────────────────────────────────────────────────────────────────────────

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

        # Prefer a valid user-selected/custom font. If no valid custom font
        # is configured, use the project's default.ttf. If neither exists,
        # leave fontfile unset so FFmpeg can use its normal fallback.
        font_path = str(watermark.get("font_path") or "").strip()
        if not (font_path and os.path.isfile(font_path)):
            font_path = self._default_watermark_font_path()

        if font_path:
            parts.insert(0, f"fontfile='{self._escape_filter_value(font_path)}'")

        # For segment encodes the watermark is always ON for the whole segment,
        # so we skip the enable= expr.
        if time_offset == 0.0:
            enable_expr = self._enable_expr(watermark)
            if enable_expr:
                parts.append(f"enable='{enable_expr}'")

        return "drawtext=" + ":".join(parts)

    def _default_watermark_font_path(self) -> str:
        """Return the project's default watermark font when it is available."""
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

    # ─────────────────────────────────────────────────────────────────────────
    # Subprocess runner
    # ─────────────────────────────────────────────────────────────────────────

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
            # Log the full stderr (Heroku/host logs) so the real cause is
            # always recoverable, even though the user-facing message below
            # is short.
            logger.error(
                "[Media] %s failed after %.1fs. Full ffmpeg stderr:\n%s",
                stage, time.monotonic() - started_at, full_err or "(empty)",
            )
            raise Exception(f"FFmpeg error: {self._summarize_ffmpeg_error(full_err)}")
        logger.info("[Media] %s completed in %.1fs.", stage, time.monotonic() - started_at)

    @staticmethod
    def _summarize_ffmpeg_error(full_stderr: str) -> str:
        """Pick the single most useful line out of ffmpeg's stderr for a
        user-facing message, instead of blindly slicing raw characters
        (which used to cut error text off mid-word and, worse, sometimes
        showed only the "Stream mapping:" listing instead of the actual
        fatal error that followed it after a second [:200] truncation
        downstream). Prefers the last line containing a known fatal-error
        marker; falls back to the last non-empty line; falls back to
        "unknown error".
        """
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

    # ─────────────────────────────────────────────────────────────────────────
    # Static helpers
    # ─────────────────────────────────────────────────────────────────────────

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
        """Only used for full-file encodes."""
        mode = str(watermark.get("timing_mode", "range"))
        if mode == "full":
            return ""
        if mode == "random_duration":
            duration = self._clamp_int(watermark.get("duration", 30), 1, 3600)
            return f"lt(mod(t\\,{duration * 2})\\,{duration})"
        # range
        start = self._clamp_int(watermark.get("start", 0), 0, 86400)
        end   = self._clamp_int(watermark.get("end",   0), 0, 86400)
        if end > start:
            return f"between(t\\,{start}\\,{end})"
        return ""

    @staticmethod
    def _escape_filter_value(value: str) -> str:
        """
        Escape a value for use INSIDE an already single-quoted ffmpeg filter
        argument, e.g. text='{escaped}'.

        FFmpeg's filtergraph quoting does not support a plain backslash-escaped
        apostrophe inside a single-quoted section (text='It\\'s' is invalid and
        aborts the whole -vf argument). To embed a literal apostrophe you must
        close the quote, escape the apostrophe outside of it, and reopen the
        quote: It's -> It'\\''s . Only backslash and the outer quote need this
        treatment; colon/bracket escaping still applies normally within the
        quoted sections.
        """
        value = value.replace("\\", "\\\\").replace(":", "\\:")
        value = value.replace("[", "\\[").replace("]", "\\]")
        # Close quote, escaped apostrophe, reopen quote.
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
